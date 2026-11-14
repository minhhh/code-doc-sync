# Architecture

`code-graph` builds a structural graph of a repository and answers questions
about it offline. This document describes the pieces, how a build flows through
them, and how queries read the result.

## Layers

```text
┌──────────────────────────────────────────────────────────────┐
│ cli/ (Typer)                                                 │
│ one module per verb group: graph, lookup, change, health,    │
│ flows, arch, refactor; common.py holds options and emission  │
└───────────────┬──────────────────────────────────────────────┘
                │
┌───────────────▼──────────────────────────────────────────────┐
│ engine modules                                               │
│ parser, incremental, postprocessing, flows, communities,     │
│ analysis, changes, search, refactor                          │
│   graph/    store (SQLite), migrations                       │
│   lib/      constants, repo seam (open_store, root paths)    │
│   languages/  tsconfig, rescript, spring, temporal           │
└───────────────┬──────────────────────────────────────────────┘
                │
┌───────────────▼──────────────────────────────────────────────┐
│ SQLite (WAL)  <root>/.code-graph/graph.db                    │
└──────────────────────────────────────────────────────────────┘
```

The CLI is the only hand-written frontend; every command calls an engine
function directly. There is no adapter layer between them — each command opens
the store through `cli/common._store`, calls one engine function, and shapes the
payload it prints.

## Build flow

```text
code-graph build
  └─ build.build_or_update(full_rebuild=True)
       ├─ incremental.find_project_root()
       ├─ incremental.get_db_path()            # <root>/.code-graph/graph.db
       ├─ GraphStore()                         # schema + migrations
       ├─ incremental.collect_all_files()      # git ls-files / SVN / walk + ignore
       ├─ for each file:
       │    ├─ CodeParser.detect_language()
       │    ├─ CodeParser.parse_bytes()
       │    │    ├─ Tree-sitter parse (or notebook/Vue/Svelte/SQL/ReScript/
       │    │    │  Pascal declaration-oriented path)
       │    │    ├─ emit File/Class/Function/Type/Test nodes
       │    │    ├─ emit CALLS/IMPORTS_FROM/INHERITS/CONTAINS/TESTED_BY edges
       │    │    └─ resolve cross-file targets (imports, aliases, re-exports)
       │    │       and typed receivers (declared parameter/constructor types)
       │    └─ GraphStore.store_file_nodes_edges()  # one transaction per file
       ├─ resolvers (ReScript, Spring, Temporal, typed receivers)
       ├─ metadata (last_updated, VCS branch/revision)
       └─ postprocess
            ├─ signatures  (nodes.signature)
            ├─ FTS5 rebuild (nodes_fts)
            ├─ flows       (flows, flow_memberships)
            ├─ communities (communities, nodes.community_id)
            └─ summary tables (community_summaries, flow_snapshots, risk_index)
```

`postprocess` levels:

| Level | Runs |
| --- | --- |
| `none` | Parse and store only |
| `minimal` | Signatures + FTS |
| `full` | Signatures + FTS + flows + communities + summaries |

`build` is always a full rebuild. `update` re-parses files changed since
`--base` plus their dependents (up to two hops by default), then uses the
incremental flow/community paths.

## Parsing and resolution

`CodeParser` returns `(list[NodeInfo], list[EdgeInfo])`. A `File` node is always
first; symbols are nested by qualified name.

- Node kinds: `File`, `Class`, `Function`, `Type`, `Test`.
- Edge kinds: `CALLS`, `IMPORTS_FROM`, `INHERITS`, `IMPLEMENTS`, `CONTAINS`,
  `TESTED_BY`, `DEPENDS_ON`, `REFERENCES`.
- Qualified name: `<absolute file path>::<Outer.Inner.name>`. Files use the
  absolute path as the qualified name.

Resolution happens per file, using an import map and the set of locally defined
names:

1. Local definition wins.
2. Imported symbol: resolve the module to a file, then chase the export
   (including aliases and `export *` barrels for JS/TS).
3. Otherwise keep the bare name; a post-pass
   (`GraphStore.resolve_bare_call_targets`) links bare calls when the name is
   unambiguous across the graph.

Language-specific resolvers fill gaps that Tree-sitter cannot see:

- `languages/tsconfig.py` — TS path aliases and `baseUrl`.
- `languages/rescript.py` — ReScript module graph.
- `languages/spring.py` — Spring dependency injection wiring.
- `languages/temporal.py` — Temporal workflow/activity wiring.

## Storage

SQLite with WAL, `check_same_thread=False`, and `isolation_level=None`.
Writes use explicit `BEGIN IMMEDIATE` transactions so a failed file leaves the
previous rows intact.

### Tables

| Table | Purpose |
| --- | --- |
| `nodes` | Symbols and files; unique `qualified_name`; `community_id`, `signature` |
| `edges` | Directed relations with `kind`, `file_path`, `line`, `confidence_tier` |
| `metadata` | Build type, timestamps, VCS info, schema version |
| `flows` / `flow_memberships` | Execution paths and ordered membership |
| `communities` | Detected clusters (size, cohesion, parent) |
| `nodes_fts` | FTS5 external-content index over name/qualified name/file/signature |
| `community_summaries` | Pre-computed architecture text |
| `flow_snapshots` | Pre-computed flow summaries |
| `risk_index` | Per-node risk score and coverage |

Schema creation and evolution live in `graph/migrations.py`: one baseline `v1`
creates the final schema; future changes append `v2`, `v3`, …

## Query flow

```text
code-graph query callers_of helper
  └─ cli.query()
       └─ queries.query_graph("callers_of", "helper")
            ├─ cli.common._store(repo_root)          # validate root, open DB
            ├─ resolve target (qualified name, file, or name)
            ├─ SQL over edges/nodes (or BFS for impact)
            ├─ shape result dict (status, summary, results, edges, _hints)
            └─ store.close()
       └─ cli._emit(result)                         # JSON or text
```

Query paths:

| Family | Implementation |
| --- | --- |
| `search` | `search.hybrid_search`: FTS5 BM25 + kind/context boosts, LIKE fallback |
| `query` | `queries.query_graph`: eight SQL patterns over `edges`/`nodes` |
| `flows list` / `flows show` | `flows.get_flows` / `get_flow_by_id` |
| `arch communities` | `communities.get_communities` |
| `arch community` | `communities.find_community` (by id, else name substring) |
| `arch overview` | `communities.get_architecture_overview` |
| `arch hubs` / `arch bridges` | `analysis.find_hub_nodes`, `find_bridge_nodes` (NetworkX) |
| `arch surprises` | `analysis.find_surprising_connections` |
| `health` | `analysis.find_knowledge_gaps`, `generate_suggested_questions`, `find_large_functions`, `refactor.find_dead_code` |
| `change impact` | `GraphStore.get_impact_radius_sql` (BFS over incoming edges) |
| `change risk` | `changes.analyze_changes`: diff ranges → nodes → flows → risk |
| `change flows` | `flows.get_affected_flows`: diff ranges → flows |
| `refactor` | `refactor.rename_preview`, `suggest_refactorings`, `apply_refactor` |
| `health dead-code` | `refactor.find_dead_code` |
| `export` | `export.build_payload`: one projection per `--type` (`call-graph`, `community`, `flows`); aliases canonicalized first |

Heavy read paths use SQL first; NetworkX is only used for betweenness and the
fallback impact traversal. `GraphStore` caches the NetworkX graph and invalidates
it on writes.

## Output contract

Every CLI command returns a dict with at least `status` and usually `summary`.
The CLI serializes it as compact JSON (`--pretty` for indentation) or prints the
summary for `--format text`.

Two optional keys point at follow-up work, and both name *commands*, not
internal function names:

- `next_command_suggestions` — up to three `<command> -- <why>` strings from
  `cli/suggestions.py`. The program name is omitted.
- `_hints` — next steps, related files, and warnings derived from in-memory
  session state (`cli/hints.py`).

Both speak the CLI's own vocabulary: a suggestion is a command path, never an
internal function name. `tests/test_cli.py` resolves every suggestion against
the registered Typer commands, so renaming a verb fails the suite instead of
shipping a stale hint.

## Deliberate exclusions

Out of scope: vector search, wiki generation, a server/daemon, and an eval
harness. This project keeps the graph engine only:

- Search is FTS5/BM25 + LIKE. No vectors.
- No review-context shaping or wiki generation.
- No server or daemon; the CLI is the interface.
- Community detection uses igraph Leiden when installed, otherwise a
  deterministic file-based grouping fallback.
