# Development

How to change `code-graph` without breaking the build/query contract.

## Setup

```bash
cd code-graph
python -m pip install -e '.[dev]'
# or
uv pip install -e '.[dev]'
```

Run the checks:

```bash
python -m pytest
ruff check src tests
mypy src/codegraph
```

`mypy` is intentionally non-strict for the engine (it follows only the
CLI surface). Ruff checks everything.

## Ownership boundary

- `src/codegraph/cli/` — **ours**. Typer commands, flags, output, exit codes.
  One module per verb group (`graph.py`, `lookup.py`, `change.py`, `health.py`,
  `flows.py`, `arch.py`, `refactor.py`); `app.py` owns the root `Typer`,
  `options.py` the shared option aliases, `common.py` root resolution and
  `_emit`.
- `src/codegraph/graph/migrations.py` — migration framework lives here; the schema
  baseline is ours.
- Everything else under `src/codegraph/` — the graph engine. Keep changes small
  and local; do not reformat wholesale, so diffs stay reviewable.
- `tests/` — engine tests plus `tests/test_cli.py` for the CLI.

When you add behavior, put it in an engine module and keep the command thin.
There is no adapter layer: a new query belongs in `queries.py`, and
`cli/lookup.py` calls it.

## Add a CLI command

1. Implement or reuse a function in an engine module. It should return a dict
   with `status` and `summary`.
2. Add a Typer command to the group module it belongs to (e.g.
   `cli/health.py`), or create a new group module and register it in
   `cli/__init__.py`.

```python
@app.command("callers")   # in a group module: @<group>_app.command("...")
def callers(
    target: Annotated[str, typer.Argument(help="Symbol or qualified name.")],
    root: RootOption = None,
    output: FormatOption = "json",
    pretty: PrettyOption = False,
    no_color: NoColorOption = False,
) -> None:
    """List callers of a symbol."""
    repo_root = _root(root)
    _emit(
        query_graph("callers_of", target, repo_root=str(repo_root)),
        output_format=output,
        pretty=pretty,
        no_color=no_color,
    )
```

3. Add a test in `tests/test_cli.py` (and a unit test in the relevant module
   when the logic is new).
4. Document the command in `README.md`.

Rules:

- Resolve the repo root with `_root(root)` and open the store with
  `with _store(repo_root) as store:` from `cli/common.py`; engine functions
  take the store, they do not open databases.
- Emit through `_emit`; never print JSON directly.
- Return exit code 1 for missing graph/entity, 2 for bad flags.
- Do not add a second output format; `--format text` is the only alternative.

## Add a query pattern

1. Add the pattern to `_QUERY_PATTERNS` in `queries.py`.
2. Implement its branch in `query_graph`.
3. Reuse `GraphStore` helpers (`get_edges_by_target`, `get_nodes_by_file`,
   `_batch_get_nodes`, …) instead of raw SQL when possible; raw `store._conn`
   access is acceptable for batch/domain-table queries, as the module already
   documents.
4. Return `status`, `summary`, `results`, and `edges` (when relevant).
5. Add a test with synthetic nodes/edges; see `tests/test_changes.py` and
   `tests/test_flows.py` for the fixture pattern.

## Add a language

Built-in languages live in `parser.py`:

1. Add extensions to `EXTENSION_TO_LANGUAGE`.
2. Add node-type lists to `_CLASS_TYPES`, `_FUNCTION_TYPES`, `_IMPORT_TYPES`,
   `_CALL_TYPES`.
3. If the grammar needs special handling (Vue, Svelte, notebooks, SQL,
   ReScript), add a dedicated branch in `parse_bytes` and a `_parse_*` helper.
4. Add fixtures and tests (`tests/test_multilang.py`, `tests/test_parser.py`).
5. Update the language list in `README.md` if it is user-visible.

Projects can add languages without patching code via
`.code-graph/languages.toml`:

```toml
[languages.mylang]
extensions = [".ml"]
grammar = "ocaml"          # tree-sitter grammar packaged by tree-sitter-language-pack
class_node_types = ["module_definition"]
function_node_types = ["value_definition"]
import_node_types = ["open_statement"]
call_node_types = ["application_expression"]
```

See `languages/custom.py` for the full schema and validation rules.

## Add or change resolution

Cross-file resolution is in `parser.py`:

- `_collect_file_scope` — local names + import map.
- `_resolve_module_to_file` / `_do_resolve_module` — module path → file.
- `_resolve_call_target`, `_resolve_imported_symbol`,
  `_resolve_exported_symbol` — symbol → qualified name.
- `_resolve_call_targets` — post-pass for bare names.

For a new language, add a branch to `_do_resolve_module` (caller-relative,
walk-up, extension probing, etc.) and tests for aliases/re-exports. The TS and
Dart branches are good references. Resolver caches are per-`CodeParser`; keep
cache keys language- and caller-directory-aware.

Some grammars need a language dispatcher rather than the generic walker:
`_extract_pascal_constructs` is the reference for declaration-oriented
languages, where the class name sits on `declType` (not `declClass`), the same
method arrives twice (interface `declProc` then implementation `defProc`, merged
into one node), dotted names parse as `genericDot`, and `uses` refers to units
by name rather than by path (`_resolve_pascal_unit`).

Language-wide wiring that cannot be expressed in Tree-sitter belongs in a
resolver module (`languages/spring.py`, `languages/temporal.py`,
`languages/rescript.py`). They run after the parse loop in `full_build` and add
edges through `GraphStore`.

## Change the schema

Never edit the shipped `_migrate_v1` once released. Append a new migration:

```python
def _migrate_v2(conn: sqlite3.Connection) -> None:
    if not _has_column(conn, "nodes", "complexity"):
        conn.execute("ALTER TABLE nodes ADD COLUMN complexity REAL DEFAULT 0")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_nodes_complexity ON nodes(complexity)"
    )


MIGRATIONS = {1: _migrate_v1, 2: _migrate_v2}
```

`LATEST_VERSION` is derived automatically. `run_migrations` applies pending
versions in order, each in its own transaction, and updates
`metadata.schema_version` after each success.

Also update:

- `_KNOWN_TABLES` if you add a table.
- `GraphStore._SCHEMA_SQL` only for tables/columns needed before migrations run
  (currently `nodes`, `edges`, `metadata`).
- `GraphNode` / `GraphEdge` dataclasses and `_row_to_node` / `_row_to_edge`.
- `node_to_dict` / `edge_to_dict`.
- `tests/test_migrations.py` and a store round-trip test in `tests/test_graph.py`.

Rules:

- Migrations must be idempotent (`IF NOT EXISTS`, `_has_column` guards).
- Use `ALTER TABLE ... ADD COLUMN` with defaults for existing rows.
- Never drop user data silently.
- Bump `LATEST_VERSION` by adding the function to `MIGRATIONS`; do not hardcode.

## Testing

```bash
python -m pytest tests/test_cli.py -q          # CLI surface
python -m pytest tests/test_parser.py -q       # parsing/resolution
python -m pytest -q                            # everything
```

Useful environment variables in tests:

| Variable | Effect |
| --- | --- |
| `CODE_GRAPH_SERIAL_PARSE=1` | Parse serially instead of a process pool |
| `CODE_GRAPH_PARSE_WORKERS=N` | Worker count |
| `CODE_GRAPH_PARSE_EXECUTOR=thread\|process` | Executor choice |
| `CODE_GRAPH_REPO_ROOT=/path` | Force project root |
| `CODE_GRAPH_DATA_DIR=/path` | Force graph data directory |
| `CODE_GRAPH_GIT_TIMEOUT=SECONDS` | Git subprocess timeout |
| `CODE_GRAPH_RECURSE_SUBMODULES=1` | Include submodules |
| `CODE_GRAPH_LEIDEN_SEED=N` | Community detection seed |
| `CODE_GRAPH_STORE_BATCH_SIZE=N` | Files per write transaction (default 1) |

Tests use `.git` directories (or `.code-graph`) to satisfy
`_validate_repo_root`. Build small fixtures with `tmp_path`, as
`tests/test_cli.py` does.

For parser tests, construct `CodeParser()` directly and assert on
`NodeInfo`/`EdgeInfo`, or call `tests/support.py::parse_path(parser, path)`,
which reads the bytes and passes them to `parse_bytes` the way the build does.
For store tests, open `GraphStore(db_path)` and use
`store_file_nodes_edges` / `upsert_node` / `upsert_edge`.

## Debugging a build

```bash
# See progress and warnings
code-graph build --pretty --postprocess full

# Skip expensive phases while iterating
code-graph build --postprocess none

# Re-run the whole pipeline including post-processing (FTS, flows, communities).
# There is no post-processing-only verb: rebuild from scratch, which is the only
# way to guarantee derived tables match the nodes.
code-graph build --postprocess full

# Inspect the raw database
sqlite3 .code-graph/graph.db '.tables'
sqlite3 .code-graph/graph.db 'select kind, count(*) from nodes group by kind'
sqlite3 .code-graph/graph.db 'select kind, count(*) from edges group by kind'
```

If a file fails to parse, `build` reports it under `errors`; the rest of the
build continues. Incremental updates print the changed-file list to stdout
before the JSON payload — that is reference behavior, not a bug.

## CLI testing notes

- `typer.testing.CliRunner` captures stdout; compact JSON must contain exactly
  one newline.
- Do not assert on ANSI color; tests run without a TTY.
- Use `--root` with a temp repo containing `.git` or `.code-graph`.

## Exclusions policy

Do not reintroduce vector search, wiki, daemon, or eval modules without an
explicit product decision. Search must remain FTS5/BM25 + LIKE, and `data_dir`
naming must stay `.code-graph` with the `CODE_GRAPH_` env prefix.
