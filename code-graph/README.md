# code-graph

> **Attribution.** This project began as a derivative of
> [code-review-graph](https://github.com/tirth8205/code-review-graph) by Tirth
> Kanani, released under the MIT licence. The Tree-sitter parsing layer, the
> SQLite graph schema, and much of the query engine originate there; the MCP
> server, the AI-facing tool surface, and the embedded vector search have been
> removed. Upstream code remains under its original copyright — see
> [LICENSE](LICENSE).

Local-first structural code graph. Parses a repository with Tree-sitter, stores
symbols and edges in SQLite, and answers graph questions from the command line.
No network access, no background service.

The command surface is a Typer CLI over the graph engine in `src/codegraph/`.

## Graph model

The graph is a directed multigraph in SQLite. Node identity is
`qualified_name`, `<absolute file path>::<Outer.Inner.name>`; a `File` node uses
the absolute path as its qualified name.

| Element | Values |
| --- | --- |
| Node kinds | `File`, `Class`, `Function`, `Type`, `Test` |
| Edge kinds | `CALLS`, `IMPORTS_FROM`, `INHERITS`, `IMPLEMENTS`, `CONTAINS`, `TESTED_BY`, `DEPENDS_ON`, `REFERENCES` |
| Direction | Edges point source to target. `callers_of` walks them in reverse, `callees_of` walks them forward. |

A `CALLS` edge may store a bare target name (`helper`) when cross-file
resolution found no file for it. A post-pass links those edges when the name is
unique in the graph, and anything still bare stays bare so unresolved calls stay
visible instead of vanishing.

## Features

- Multi-language parsing (Python, JS/TS, Go, Rust, Java, C/C++, C#, Ruby, PHP,
  Kotlin, Scala, Swift, Dart, Pascal, bash, Nix, notebooks, and more).
- Full and incremental builds with git/SVN change detection
  mode for continuous updates.
- Cross-file resolution: calls, imports, inheritance, containment, tests.
- FTS5/BM25 symbol search with keyword fallback.
- Predefined graph navigation: callers, callees, imports, importers, children,
  tests, inheritors, file summaries.
- Impact radius, BFS/DFS traversal, and risk-scored change detection.
- Execution flow tracing and criticality ranking.
- Community detection, architecture overview (`arch`), hubs, bridges, knowledge gaps,
  surprising coupling, and suggested review questions.
- Dead-code detection, rename previews, community-driven refactor suggestions,
  and apply-with-diff.
- JSON export (`export --format json`) for ctx-sync and external tools.

## Install

```bash
cd code-graph
python -m pip install -e .
# or
uv pip install -e .
```

## Quick start

```bash
# Build the graph for the current repository
code-graph build

# Inspect it
code-graph status
code-graph search helper
code-graph query callers_of helper

# Keep it fresh
code-graph update --base HEAD~1
```

The graph lives in `<root>/.code-graph/graph.db` — always under the root you
point at, so `--root` is the only location knob. Root detection accepts
`--root`, `CODE_GRAPH_REPO_ROOT`, or the enclosing git/svn root. Library code
may still set `CODE_GRAPH_DATA_DIR` to place the database outside the worktree;
the CLI does not expose that as a flag.

## Output

All commands emit compact JSON by default (one line, pipe-friendly).

| Flag | Effect |
| --- | --- |
| `--pretty` | Indented JSON |
| `--format text` | Human-readable summary |
| `--no-color` | Disable ANSI color (text mode auto-detects TTY + `NO_COLOR`) |

Query commands open SQLite read-only. Errors go to stderr and exit non-zero.

## Commands

### Build and maintain

| Command | Description |
| --- | --- |
| `code-graph build [--postprocess full\|minimal\|none] [--recurse-submodules]` | Full rebuild |
| `code-graph update [--base REF] [--postprocess ...]` | Re-parse changed files and dependents |
| `code-graph status` | Node/edge/file/language counts |

### Search and navigate

| Command | Description |
| --- | --- |
| `code-graph search QUERY [--kind KIND] [--limit N]` | FTS5/BM25 search |
| `code-graph query PATTERN TARGET [--limit N] [--detail standard\|minimal]` | Predefined query (see below) |
| `code-graph search QUERY` | Find nodes by name or hybrid search |

`query` patterns:

| Pattern | Returns |
| --- | --- |
| `callers_of` | Functions/methods that call the target |
| `callees_of` | Functions/methods called by the target |
| `imports_of` | Modules imported by a file/module |
| `importers_of` | Files that import the target |
| `children_of` | Nodes contained in a file or class |
| `tests_for` | Tests covering the target |
| `inheritors_of` | Classes that inherit the target |
| `file_summary` | All nodes in a file |

Targets accept a name, a qualified name (`/path/file.py::Class.method`), or a
file path.

`--limit N` (default 20) caps returned rows. The envelope always reports
`result_count` (the untruncated total), `returned`, and `truncated`, so a capped
answer is never mistaken for a complete one; when truncated, `edges` is filtered
to edges incident to the returned rows.

`--detail minimal` keeps only `name`, `kind`, and `file_path` per row, drops
`edges`, and caps rows at five (or `--limit`, if smaller).

Every `query` call returns one object with `status`, `pattern`, `target`,
`summary`, `results`, and `edges`.

| `status` | Meaning |
| --- | --- |
| `ok` | Normal completion; `results` may be empty |
| `ambiguous` | Name search matched several nodes; `candidates` lists them and no traversal ran |
| `not_found` | The target did not resolve |
| `error` | Unknown pattern; `error` lists the valid ones |

`error` and `not_found` exit 1, other statuses exit 0. A rejected flag value
exits 2.

### Flows

| Command | Description |
| --- | --- |
| `code-graph flows list [--sort-by criticality\|depth\|node_count\|name] [--limit N]` | List execution flows |
| `code-graph flows show ID` | One flow with steps |

### Change analysis

All three take an optional file list (auto-detected from the diff otherwise)
and a `--base` ref, so they answer questions about the same change:

| Command | Description |
| --- | --- |
| `code-graph change risk [FILES...] [--base REF] [--max-depth N] [--include-source]` | Risk-scored change analysis |
| `code-graph change impact [FILES...] [--base REF] [--max-depth N]` | Blast radius of the change |
| `code-graph change flows [FILES...] [--base REF]` | Execution flows the change touches |

### Health

Four diagnostics over the whole graph: nothing is passed in, each returns a
list to triage.

| Command | Description |
| --- | --- |
| `code-graph health dead-code [--kind K] [--file-pattern TEXT]` | Unreferenced functions/classes |
| `code-graph health large-functions [--min-lines N] [--limit N]` | Oversized functions/classes |
| `code-graph health knowledge-gaps` | Isolated nodes, thin communities, untested hotspots |
| `code-graph health suggested-questions` | Review questions generated from the graph |

### Refactoring

| Command | Description |
| --- | --- |
| `code-graph refactor rename OLD NEW` | Preview a rename (writes nothing) |
| `code-graph refactor suggest` | Community-driven suggestions |
| `code-graph refactor apply ID [--dry-run]` | Apply or diff a rename preview |

### Architecture

Descriptive, not diagnostic: these describe how the graph is wired, whether or
not anything is wrong with it.

| Command | Description |
| --- | --- |
| `code-graph arch communities [--sort-by size\|cohesion\|name] [--min-size N]` | List communities |
| `code-graph arch community [--id N \| --name TEXT] [--members]` | One community |
| `code-graph arch overview` | Community-based architecture overview |
| `code-graph arch hubs [--top-n N]` | Most connected nodes |
| `code-graph arch bridges [--top-n N]` | Betweenness-centrality chokepoints |
| `code-graph arch surprises [--top-n N]` | Cross-community/language coupling |

### Export

| Command | Description |
| --- | --- |
| `code-graph export [--format json] [--out FILE] [--pretty]` | Full graph JSON (stdout if `--out` omitted) |
| `code-graph export --type call-graph\|community\|flows` | Typed projection: symbols+`CALLS`, communities+inter-community pairs, or execution flows |

`--type` also accepts the aliases `architecture`/`communities` and `flow`; an
unknown type exits 2 with the supported list. `--limit` caps flow rows.

The payload is one object:

| Field | Contents |
| --- | --- |
| `status` | `ok` |
| `root` | Absolute repo root |
| `type` | Projection name |
| `summary` | One-line counts |
| `nodes`, `edges` | Full graph, for `call-graph` |
| `metadata` | `last_updated`, `last_build_type`, `vcs_branch`, `vcs_revision` |

`community` replaces `nodes` and `edges` with `communities` and
`inter_community_edges`; `flows` replaces them with `flows`. Node fields are
`id`, `kind`, `name`, `qualified_name`, `file_path`, `line_start`, `line_end`,
`language`, `parent_name`, `is_test`. Edge fields are `id`, `kind`, `source`,
`target`, `file_path`, `line`, `confidence`, `confidence_tier`.

## Examples

```bash
# Who calls helper, with tests
code-graph query callers_of helper
code-graph query tests_for helper

# Review the last commit
code-graph change risk --base HEAD~1 --include-source

# Find decomposition targets
code-graph health large-functions --min-lines 120
code-graph arch hubs --top-n 20

# Export for an external tool
code-graph export --format json --out graph.json

# Export one projection (consumed by ctx-sync --type)
code-graph export --type community > architecture.json
code-graph export --type flows --limit 20 > flows.json
```

## Documentation

- [docs/architecture.md](docs/architecture.md) — overall flow, layers, data model,
  build/query pipelines, resolution and post-processing.
- [docs/development.md](docs/development.md) — how to modify the project: add a
  command, language, resolver, or migration; testing and linting.

## Intentional exclusions

Vector search, wiki generation, a server or daemon, an eval harness, and an
enrichment pipeline are not part of this project. Search is FTS5/BM25 with a
keyword fallback. Community detection uses Leiden when igraph is installed,
otherwise deterministic file-based grouping.

Only files tracked by version control are indexed (`git ls-files`, or the SVN
equivalent). Build output, scratch files, and anything else untracked stay out
of the graph, so a new file does not appear until it is added. `git add -N` on
the path is enough: it marks the file tracked without staging its content.
