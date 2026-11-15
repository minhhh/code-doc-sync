# Detailed Design

How `code-graph` computes the numbers it reports: the formulas, the constants,
and the data layouts they run on. [Architecture](architecture.md) covers the
layers and the build/query flow; this document covers the math inside them.

Every constant below exists in the source. Where a value is tunable, the
environment variable is named next to it.

## Data model

Nodes and edges live in SQLite (`.code-graph/graph.db`). A node has an integer
`id`, a `qualified_name` of the form `path/to/file.py::symbol`, a `kind`, a file
path, a line range, a language, and an optional signature. `File` nodes are real
rows; they anchor `CONTAINS` edges and are skipped by projections that expect
callable entities.

Edges carry `kind`, `line`, `confidence`, and `confidence_tier`. The tier
describes how the edge was established, not how much to trust it at query time:
`EXTRACTED` is the default, and resolvers mark weaker matches with the other
tiers. Nothing in the scoring code weights by tier.

Derived tables (`nodes_fts`, `flows`, `communities`, `community_summaries`) are
rebuilt from `nodes` and `edges` only. There is no partial refresh: a stale
derived table means the whole build must be re-run.

Two identity rules follow from that:

- Numeric community ids are reissued on every rebuild. Stable identity is the
exported `<name>#<digest>` key, where the digest is the first 12 hex characters
of a SHA-1 over the *sorted* member list (`ctx-sync` writes that key as
`community::<name>#<digest>`).
- Flow identity is the qualified name of its entry point, with no digest. One
  entry point yields one flow, so the name is already unique and stable across
  rebuilds. A digest over the traced path was removed because it changed the key
  whenever a call inside the flow moved, which made `ctx-sync` treat the flow as
  new, keep the old block and append a second copy.

## Call resolution

A `CALLS` target is qualified in three stages, and any stage may leave it bare:

1. **File scope.** A name defined in the same file becomes `path::Name`, or
   `path::Class.method` for a method of the enclosing class.
2. **Imports.** An imported name becomes the qualified name of its definition.
   This follows relative imports (`from .mod import x`), package re-exports
   (`pkg/__init__.py` holding `from .store import x`), and JS/TS barrels
   (`export { x } from './y'`), with a cycle guard in the chain. If the module
   doesn't define the name, the fallback is that module's file.
3. **Typed receivers.** `obj.method()` resolves when the file declares the
   receiver's class — a parameter annotation (`def f(store: GraphStore)`) or a
   constructor assignment (`s = Store()`, `const s = new Store()`) — the class
   declares that method, and the class name is unambiguous in the graph. A tie
   prefers a method in the caller's own file, then one in the same language;
   otherwise the target stays bare.

The bare targets left over are deliberate. Calls into libraries
(`cursor.execute`), container methods (`seen.add`), receivers reached through an
attribute (`self._conn.commit()`), and untyped parameters have no declaration to
resolve against, so any rewrite would be a guess. A wrong `CALLS` edge is worse
than a missing one: entry-point detection, dead-code detection, and impact
analysis all read the edge set as fact.

## Post-processing order

`run_post_processing` runs five phases in this order, and later phases read what
earlier ones wrote:

1. Compute signatures for nodes that lack one (`def name(params)` synthesized
   from parsed parameters).
2. Rebuild the FTS5 index.
3. Trace and store flows.
4. Detect and store communities.
5. Compute community summaries, flow snapshots, and the risk index.

`level="minimal"` stops after phase 2, which keeps search working while skipping
the expensive passes. `level="none"` skips the pipeline entirely.

Flows are traced before communities because `compute_risk_score` reads flow
criticalities, and community rows must exist before a node's community id can be
looked up.

## Community detection

### Graph construction

Leiden runs on an undirected multigraph. Node set is every node in the index;
edges are deduplicated by unordered pair (`(min, max)`), self-loops are dropped,
and parallel edges collapse to one with the weight of whichever edge was seen
first.

Edge weights feed the modularity objective:

| Edge kind | Weight |
| --- | --- |
| `CALLS` | 1.0 |
| `IMPORTS_FROM` | 0.5 |
| `INHERITS` | 0.8 |
| `IMPLEMENTS` | 0.7 |
| `CONTAINS` | 0.3 |
| `TESTED_BY` | 0.4 |
| `DEPENDS_ON` | 0.6 |
| anything else | 0.5 |

### Leiden parameters

```python
resolution = max(0.05, 1.0 / math.log10(max(n_nodes, 10)))
partition = g.community_leiden(
    objective_function="modularity",
    weights="weight",
    resolution=resolution,
    n_iterations=2,
)
```

Three choices worth knowing:

- **Resolution scales down as the graph grows.** At `n=1000` the factor is 0.33,
  at `n=30000` it is about 0.23. A fixed `resolution=1.0` produced thousands of
  two-node communities on 30k-node repos.
- **`n_iterations=2`** is enough for code dependency graphs; higher values cost
  minutes on large graphs for a partition that moves by a handful of nodes.
- **The recursive aggregation pass is skipped.** It blew up exponentially past
  100k nodes.

The RNG is seeded with `42` (`CODE_GRAPH_LEIDEN_SEED` overrides) so two builds of
the same graph produce the same partition. Without it, community ids, sizes, and
cohesion drift between runs and nothing is reproducible.

Partitions smaller than `min_size` (default `2`) are dropped.

### File-based fallback

When igraph is absent, or Leiden produces no edges, grouping falls back to
directories. The algorithm strips the longest common directory prefix across all
node paths, then increases the grouping depth from 1 until at least 10 groups
meet `min_size`. A node with no remaining directory segments is keyed by its file
stem, so shallow repos still split.

### Cohesion

```
cohesion(C) = internal(C) / (internal(C) + external(C))
```

`internal` counts edges with both endpoints in `C`; `external` counts edges with
exactly one endpoint in `C`. An edge crossing two communities increments
`external` for both sides. A community with no incident edges scores `0.0`.
Values are rounded to 4 decimals for storage.

Cohesion is computed in one pass over all edges for all communities
(`_compute_cohesion_batch`), building a `qualified_name -> community_index` map
first: `O(E + Σ|C|)` instead of `O(E × |C|)`. The per-community function is a
thin wrapper over the batch version so the two cannot diverge.

### Naming

1. Extract the most common directory component (second-to-last path segment) or
   file stem.
2. If a `Class` name covers more than 40% of members, use `prefix-class-slug`.
3. Otherwise use `prefix-keyword`, where the keyword is the most frequent word
   in member names after splitting on case and underscore, filtered against a
   stopword list (`get`, `set`, `init`, `test`, `main`, and similar).
4. Fallbacks: `prefix`, then the keyword, then `cluster`, then `empty`.

Names are not unique. Two communities can both be called `codegraph-find`; the
digest in the id separates them.

### Oversized splitting

Any community larger than 25% of all nodes is re-partitioned with Leiden on its
own subgraph, recursively, provided the result keeps at least 10 nodes per part.
Without igraph, oversized communities are left as they are. The point is to stop
one giant "everything" community from swallowing the architecture view.

## Execution flows

### Entry points

A node is an entry point if any of these hold:

1. No incoming `CALLS` edge. Edges sourced at `File` nodes are excluded from
   this test, so a function called only from module scope still counts.
2. A decorator matches a framework pattern: `app.<verb>`, `app.callback`,
   `app.result_callback`, `router.<verb>`, `blueprint.route`, `click.command`,
   `<group>.command`, `field_validator`, `celery.task`, and their siblings.
3. The name matches `main`, `__main__`, `test_*`, `Test[A-Z]…`, `on_*`,
   `handle_*`, `handler`, `lambda_handler`, `upgrade`, or the other migration
   entry names.

`Test` nodes and files matching `__tests__/`, `*.spec.ts(x)`, `*.test.ts(x)`,
`test_*.py` are excluded unless `include_tests` is set.

Condition 1 compares a node's `qualified_name` against the set of `CALLS`
targets, so it is only as good as resolution: a call that stayed bare
(`_emit` instead of `path/common.py::_emit`) does not mark its target as
called, and that target becomes a flow root. Python resolution covers absolute
imports, relative imports at any depth (`from .mod import x`, `from ..pkg.mod
import x`, `from . import x`), imports inside function bodies, and package
re-exports. Member calls such as `store.set_metadata(...)` resolve when the
receiver's class is declared in the calling file; calls whose receiver type is
unknown stay bare, and calls into libraries outside the graph never resolve at
all. Neither affects this test, since only real nodes are compared.

### Tracing

Forward BFS over `CALLS` edges, `max_depth=15`. Each node is expanded once; a
target reached again is emitted as a `ref` with no children, which records the
call without walking its subtree twice. A flow records `depth` (deepest level
reached), `node_count`, `file_count`, and the path.

### Criticality

```
file_spread  = min((file_count - 1) / 4, 1)        # 1 file → 0, 5+ → 1
external     = min(external_calls / 5, 1)
security     = security_hits / node_count
test_gap     = 1 - tested_nodes / node_count
depth_score  = min(depth / 10, 1)

criticality = 0.30 * file_spread
            + 0.20 * external
            + 0.25 * security
            + 0.15 * test_gap
            + 0.10 * depth_score
```

`external_calls` counts `CALLS` targets that are not present in the graph, which
is how stdlib and third-party calls show up. `security_hits` counts nodes whose
name or qualified name contains one of `SECURITY_KEYWORDS`, at most once per
node. Result is clamped to `[0, 1]` and rounded to 4 decimals.

Flows are stored sorted by criticality descending.

## Dead code

`find_dead_code` reports functions and classes with no incoming `CALLS`,
`TESTED_BY`, `IMPORTS_FROM`, `REFERENCES`, or `INHERITS` edge, after these
exclusions:

- **A decorator on the allowlist.** The node is invoked by a framework that no
  `CALLS` edge can represent: `@property` (attribute access), `@abstractmethod`
  (polymorphic dispatch), `@classmethod` and `@staticmethod` (called through the
  class), `@app.callback` (a Typer/Click root hook), `@app.result_callback`
  (the post-command hook), `@HostListener` (Angular events), and `@dataclass`
  on a class (instantiated as a type). The list is
  closed on purpose. A decorator only earns its place when the framework is
  known to call the node itself, so `@functools.lru_cache` on an uncalled
  helper, or a project-specific `@timed`, still reports as dead: precision here
  is what keeps the report usable, and widening the rule to "any decorator"
  trades that away for recall nobody asked for.
- Entry-point conventions: the name patterns and framework decorators used for
  flow roots.
- Framework base classes, and class names carrying a known CDK suffix.
- Methods overriding an `@abstractmethod` in a base class: they are reached
  through the base reference.
- Classes with at least one live member.

A node passed as a value counts as live. `REFERENCES` edges come from
positions where a function is named but never called with parentheses: object
literal values, array elements, property assignments, bare arguments, and
Python keyword arguments such as `Option(callback=_version_callback)`. A
callback registered through a keyword argument would otherwise look exactly
like a function nobody calls.

The parser stores decorators on the node as `extra.decorators`, for functions,
tests, and classes. Without that field the allowlist could never match, which is
why every decorated node used to look dead. The same field feeds
decorator-based entry-point detection.

## Change risk

Changed nodes are found by parsing `git diff` hunks into line ranges (SVN
equivalent supported) and keeping any node whose `[line_start, line_end]`
overlaps a changed range. When a path does not match exactly, file paths are
suffix-matched against the graph.

### Per-node score

| Component | Contribution | Cap |
| --- | --- | --- |
| Flow participation | sum of the flow criticalities the node belongs to | 0.25 |
| Flow membership (fallback, no criticality recorded) | 0.05 per flow | 0.25 |
| Community crossing | 0.05 per caller in a different community | 0.15 |
| Test coverage | `0.30 - min(tests / 5, 1) × 0.25` | — |
| Security sensitivity | 0.20 flat if the name or qualified name matches `SECURITY_KEYWORDS` | — |
| Caller count | `callers / 20` | 0.10 |

Test coverage runs from `0.30` for a node with no tests down to `0.05` at five or
more. The score is clamped to `[0, 1]` and rounded to 4 decimals, so the
theoretical maximum needs flows, cross-community callers, no tests, a sensitive
name, and 20+ callers at once.

`tests` here means transitive coverage: direct `TESTED_BY` edges plus `TESTED_BY`
edges on callees one `CALLS` hop out (`max_depth=1`), with the fan-out per hop
capped at 50 (`CODE_GRAPH_MAX_TRANSITIVE_FRONTIER`). Classes expand to their
methods before the lookup.

The change report's overall `risk_score` is the maximum over changed nodes, not
a sum or mean, and `review_priorities` is the top 10 nodes by score.

## Search ranking

Search uses FTS5 BM25 over `name`, `qualified_name`, `file_path`, and
`signature`, tokenized with `porter unicode61`, kept in sync with `nodes` via
`content='nodes'`.

Ordering is: fetch `limit × 3` rows by BM25, apply boosts, filter by kind, then
truncate to `limit`. Boosts multiply the BM25 score:

| Signal | Boost |
| --- | --- |
| PascalCase query | `Class`, `Type` ×1.5 |
| snake_case query | `Function` ×1.5 |
| Query contains `.` | qualified-name match ×2.0 |
| Query identifiers appear in `qualified_name` | ×2.0 |
| Node lives in a `context_files` path | ×1.5 |

Fallback is a `LIKE '%word%'` scan over `name` and `qualified_name`, used when
the FTS5 table is missing or returns nothing. Note that the schema migration
creates the FTS5 table unconditionally, so this fallback only helps on databases
predating it; an SQLite build without the FTS5 extension fails earlier, at
migration time.

## Impact radius

Reverse BFS over callers, bounded by:

- `max_depth = 2` (`CODE_GRAPH_MAX_IMPACT_DEPTH`)
- `max_nodes = 500` (`CODE_GRAPH_MAX_IMPACT_NODES`)

Two engines: a SQLite recursive CTE (`sql`, the default) or a Python-side
networkx BFS (`networkx`), selected with `CODE_GRAPH_BFS_ENGINE`. The SQL engine
hands the work to the database; the networkx engine materializes the graph in
memory and is faster on graphs that fit comfortably in RAM.

## Tunables

| Setting | Default | Env var |
| --- | --- | --- |
| Leiden seed | `42` | `CODE_GRAPH_LEIDEN_SEED` |
| Impact depth | `2` | `CODE_GRAPH_MAX_IMPACT_DEPTH` |
| Impact node cap | `500` | `CODE_GRAPH_MAX_IMPACT_NODES` |
| Transitive test fan-out | `50` | `CODE_GRAPH_MAX_TRANSITIVE_FRONTIER` |
| BFS engine | `sql` | `CODE_GRAPH_BFS_ENGINE` |
| Git subprocess timeout | `30s` | `CODE_GRAPH_GIT_TIMEOUT` |
| Files per write transaction | `1` | `CODE_GRAPH_STORE_BATCH_SIZE` |

`CODE_GRAPH_STORE_BATCH_SIZE=1` writes each file in its own transaction.
Values above 1 buffer the parsed nodes and edges for that many files and commit
them together, which cuts commits on large repos at the cost of holding those
graphs in memory and widening a failed transaction's rollback to the batch.

Hardcoded, not tunable: Leiden `n_iterations=2`, resolution floor `0.05`,
`min_size=2` for communities, `max_depth=15` for flow tracing, the 25%/10 split
thresholds, all scoring weights above, `_SQL_BATCH=450` for batched queries,
`fetch_limit = limit × 3` in search.

## Complexity and resource notes

- Cohesion: one pass over all edges for all communities. The naive per-community
  loop was `O(E × |C|)` and effectively hung on large repos.
- Batched `IN (...)` queries use at most 450 parameters per statement to stay
  under SQLite's default 999-variable limit.
- SQLite runs in WAL mode with `busy_timeout=5000ms` and a `30s` connection
  timeout, so a concurrent reader does not block a build indefinitely.
- The FTS rebuild drops and recreates the table inside one `BEGIN IMMEDIATE`
  transaction; a crash mid-rebuild cannot leave the database without an index.
- A full rebuild is the only supported way to refresh derived tables.

## Invariants

- Scoring reads only `nodes`, `edges`, and the derived tables built from them.
- Provenance in a score always exists: every component above is computed from
  stored fields, never from parsed source at query time.
- Community and flow identity survives rebuilds only through name/digest keys,
  never through numeric ids.
- Test nodes never become entry points by default, so flow criticality describes
  production paths.

## Worked example: the tool's own flows

The section below is generated from this repository's own graph. Each entry is a
`CTX-SYNC:FLOW` block, the same shape `ctx-sync` writes into `FLOWS.md`, so the
whole section can be refreshed in place:

```bash
code-graph build --root .
code-graph export --type flows --root . --limit 100 \
  | ctx-sync --type flows --root . --target code-graph/docs/detailed-design.md
```

The graph is built over the repository root, and the `.code-graphignore` file
there drops `tests/**`, so what follows is product code only. Reading it:
`criticality` is the score from the formula above, `nodes` is distinct nodes over
total tree nodes, four spaces of indentation is one level of depth, and a target
listed twice is a re-encountered call whose subtree was not walked again. All
40 traced flows are listed.

<!-- CTX-SYNC:FLOW-START -->
* flow=code-graph/src/codegraph/cli/arch.py::arch_bridges
    * entry_point=code-graph/src/codegraph/cli/arch.py::arch_bridges
    * criticality=0.7092
    * nodes=26/27
* code-graph/src/codegraph/cli/arch.py::arch_bridges
    * → code-graph/src/codegraph/cli/common.py::_emit
        * → code-graph/src/codegraph/cli/common.py::_color_enabled
        * → code-graph/src/codegraph/cli/common.py::_json_text
    * → code-graph/src/codegraph/cli/common.py::_root
        * → code-graph/src/codegraph/pipeline/incremental.py::find_project_root
            * → code-graph/src/codegraph/pipeline/incremental.py::find_repo_root
                * → code-graph/src/codegraph/pipeline/incremental.py::find_svn_root
    * → code-graph/src/codegraph/cli/common.py::_store
        * → code-graph/src/codegraph/graph/graph_store.py::GraphStore
        * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.close
            * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.close
        * → code-graph/src/codegraph/pipeline/incremental.py::get_db_path
            * → code-graph/src/codegraph/pipeline/incremental.py::get_data_dir
                * → code-graph/src/codegraph/pipeline/incremental.py::_write_data_dir_gitignore
    * → code-graph/src/codegraph/cli/common.py::_with_hints
        * → code-graph/src/codegraph/cli/hints.py::generate_hints
            * → code-graph/src/codegraph/cli/hints.py::_build_next_steps
            * → code-graph/src/codegraph/cli/hints.py::_build_related
            * → code-graph/src/codegraph/cli/hints.py::_extract_warnings
    * → code-graph/src/codegraph/query/analysis.py::find_bridge_nodes
        * → code-graph/src/codegraph/graph/graph_store.py::GraphStore._build_networkx_graph
        * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.get_all_community_ids
        * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.get_all_nodes
            * → code-graph/src/codegraph/graph/graph_store.py::GraphStore._row_to_node
                * → code-graph/src/codegraph/graph/graph_store.py::GraphNode
        * → code-graph/src/codegraph/graph/graph_store.py::_sanitize_name
<!-- CTX-SYNC:FLOW-END -->

<!-- CTX-SYNC:FLOW-START -->
* flow=code-graph/src/codegraph/cli/arch.py::arch_communities
    * entry_point=code-graph/src/codegraph/cli/arch.py::arch_communities
    * criticality=0.7117
    * nodes=23/25
* code-graph/src/codegraph/cli/arch.py::arch_communities
    * → code-graph/src/codegraph/cli/arch.py::_require_graph
        * → code-graph/src/codegraph/pipeline/incremental.py::get_db_path
            * → code-graph/src/codegraph/pipeline/incremental.py::get_data_dir
                * → code-graph/src/codegraph/pipeline/incremental.py::_write_data_dir_gitignore
    * → code-graph/src/codegraph/cli/common.py::_emit
        * → code-graph/src/codegraph/cli/common.py::_color_enabled
        * → code-graph/src/codegraph/cli/common.py::_json_text
    * → code-graph/src/codegraph/cli/common.py::_root
        * → code-graph/src/codegraph/pipeline/incremental.py::find_project_root
            * → code-graph/src/codegraph/pipeline/incremental.py::find_repo_root
                * → code-graph/src/codegraph/pipeline/incremental.py::find_svn_root
    * → code-graph/src/codegraph/cli/common.py::_store
        * → code-graph/src/codegraph/graph/graph_store.py::GraphStore
        * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.close
            * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.close
        * → code-graph/src/codegraph/pipeline/incremental.py::get_db_path
    * → code-graph/src/codegraph/cli/common.py::_with_hints
        * → code-graph/src/codegraph/cli/hints.py::generate_hints
            * → code-graph/src/codegraph/cli/hints.py::_build_next_steps
            * → code-graph/src/codegraph/cli/hints.py::_build_related
            * → code-graph/src/codegraph/cli/hints.py::_extract_warnings
    * → code-graph/src/codegraph/query/communities.py::get_communities
        * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.get_community_member_qns
        * → code-graph/src/codegraph/graph/graph_store.py::_sanitize_name
<!-- CTX-SYNC:FLOW-END -->

<!-- CTX-SYNC:FLOW-START -->
* flow=code-graph/src/codegraph/cli/arch.py::arch_community
    * entry_point=code-graph/src/codegraph/cli/arch.py::arch_community
    * criticality=0.7168
    * nodes=28/31
* code-graph/src/codegraph/cli/arch.py::arch_community
    * → code-graph/src/codegraph/cli/arch.py::_require_graph
        * → code-graph/src/codegraph/pipeline/incremental.py::get_db_path
            * → code-graph/src/codegraph/pipeline/incremental.py::get_data_dir
                * → code-graph/src/codegraph/pipeline/incremental.py::_write_data_dir_gitignore
    * → code-graph/src/codegraph/cli/common.py::_emit
        * → code-graph/src/codegraph/cli/common.py::_color_enabled
        * → code-graph/src/codegraph/cli/common.py::_json_text
    * → code-graph/src/codegraph/cli/common.py::_root
        * → code-graph/src/codegraph/pipeline/incremental.py::find_project_root
            * → code-graph/src/codegraph/pipeline/incremental.py::find_repo_root
                * → code-graph/src/codegraph/pipeline/incremental.py::find_svn_root
    * → code-graph/src/codegraph/cli/common.py::_store
        * → code-graph/src/codegraph/graph/graph_store.py::GraphStore
        * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.close
            * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.close
        * → code-graph/src/codegraph/pipeline/incremental.py::get_db_path
    * → code-graph/src/codegraph/cli/common.py::_with_hints
        * → code-graph/src/codegraph/cli/hints.py::generate_hints
            * → code-graph/src/codegraph/cli/hints.py::_build_next_steps
            * → code-graph/src/codegraph/cli/hints.py::_build_related
            * → code-graph/src/codegraph/cli/hints.py::_extract_warnings
    * → code-graph/src/codegraph/query/communities.py::find_community
        * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.get_nodes_by_community_id
            * → code-graph/src/codegraph/graph/graph_store.py::GraphStore._row_to_node
                * → code-graph/src/codegraph/graph/graph_store.py::GraphNode
        * → code-graph/src/codegraph/graph/graph_store.py::node_to_dict
            * → code-graph/src/codegraph/graph/graph_store.py::_sanitize_name
        * → code-graph/src/codegraph/query/communities.py::get_communities
            * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.get_community_member_qns
            * → code-graph/src/codegraph/graph/graph_store.py::_sanitize_name
<!-- CTX-SYNC:FLOW-END -->

<!-- CTX-SYNC:FLOW-START -->
* flow=code-graph/src/codegraph/cli/arch.py::arch_hubs
    * entry_point=code-graph/src/codegraph/cli/arch.py::arch_hubs
    * criticality=0.7079
    * nodes=28/29
* code-graph/src/codegraph/cli/arch.py::arch_hubs
    * → code-graph/src/codegraph/cli/common.py::_emit
        * → code-graph/src/codegraph/cli/common.py::_color_enabled
        * → code-graph/src/codegraph/cli/common.py::_json_text
    * → code-graph/src/codegraph/cli/common.py::_root
        * → code-graph/src/codegraph/pipeline/incremental.py::find_project_root
            * → code-graph/src/codegraph/pipeline/incremental.py::find_repo_root
                * → code-graph/src/codegraph/pipeline/incremental.py::find_svn_root
    * → code-graph/src/codegraph/cli/common.py::_store
        * → code-graph/src/codegraph/graph/graph_store.py::GraphStore
        * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.close
            * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.close
        * → code-graph/src/codegraph/pipeline/incremental.py::get_db_path
            * → code-graph/src/codegraph/pipeline/incremental.py::get_data_dir
                * → code-graph/src/codegraph/pipeline/incremental.py::_write_data_dir_gitignore
    * → code-graph/src/codegraph/cli/common.py::_with_hints
        * → code-graph/src/codegraph/cli/hints.py::generate_hints
            * → code-graph/src/codegraph/cli/hints.py::_build_next_steps
            * → code-graph/src/codegraph/cli/hints.py::_build_related
            * → code-graph/src/codegraph/cli/hints.py::_extract_warnings
    * → code-graph/src/codegraph/query/analysis.py::find_hub_nodes
        * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.get_all_community_ids
        * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.get_all_edges
            * → code-graph/src/codegraph/graph/graph_store.py::GraphStore._row_to_edge
                * → code-graph/src/codegraph/graph/graph_store.py::GraphEdge
        * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.get_all_nodes
            * → code-graph/src/codegraph/graph/graph_store.py::GraphStore._row_to_node
                * → code-graph/src/codegraph/graph/graph_store.py::GraphNode
        * → code-graph/src/codegraph/graph/graph_store.py::_sanitize_name
<!-- CTX-SYNC:FLOW-END -->

<!-- CTX-SYNC:FLOW-START -->
* flow=code-graph/src/codegraph/cli/arch.py::arch_overview
    * entry_point=code-graph/src/codegraph/cli/arch.py::arch_overview
    * criticality=0.7257
    * nodes=28/31
* code-graph/src/codegraph/cli/arch.py::arch_overview
    * → code-graph/src/codegraph/cli/arch.py::_require_graph
        * → code-graph/src/codegraph/pipeline/incremental.py::get_db_path
            * → code-graph/src/codegraph/pipeline/incremental.py::get_data_dir
                * → code-graph/src/codegraph/pipeline/incremental.py::_write_data_dir_gitignore
    * → code-graph/src/codegraph/cli/common.py::_emit
        * → code-graph/src/codegraph/cli/common.py::_color_enabled
        * → code-graph/src/codegraph/cli/common.py::_json_text
    * → code-graph/src/codegraph/cli/common.py::_root
        * → code-graph/src/codegraph/pipeline/incremental.py::find_project_root
            * → code-graph/src/codegraph/pipeline/incremental.py::find_repo_root
                * → code-graph/src/codegraph/pipeline/incremental.py::find_svn_root
    * → code-graph/src/codegraph/cli/common.py::_store
        * → code-graph/src/codegraph/graph/graph_store.py::GraphStore
        * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.close
            * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.close
        * → code-graph/src/codegraph/pipeline/incremental.py::get_db_path
    * → code-graph/src/codegraph/cli/common.py::_with_hints
        * → code-graph/src/codegraph/cli/hints.py::generate_hints
            * → code-graph/src/codegraph/cli/hints.py::_build_next_steps
            * → code-graph/src/codegraph/cli/hints.py::_build_related
            * → code-graph/src/codegraph/cli/hints.py::_extract_warnings
    * → code-graph/src/codegraph/query/communities.py::get_architecture_overview
        * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.get_all_edges
            * → code-graph/src/codegraph/graph/graph_store.py::GraphStore._row_to_edge
                * → code-graph/src/codegraph/graph/graph_store.py::GraphEdge
        * → code-graph/src/codegraph/graph/graph_store.py::_sanitize_name
        * → code-graph/src/codegraph/query/communities.py::_is_test_community
        * → code-graph/src/codegraph/query/communities.py::get_communities
            * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.get_community_member_qns
            * → code-graph/src/codegraph/graph/graph_store.py::_sanitize_name
<!-- CTX-SYNC:FLOW-END -->

<!-- CTX-SYNC:FLOW-START -->
* flow=code-graph/src/codegraph/cli/arch.py::arch_surprises
    * entry_point=code-graph/src/codegraph/cli/arch.py::arch_surprises
    * criticality=0.7079
    * nodes=28/29
* code-graph/src/codegraph/cli/arch.py::arch_surprises
    * → code-graph/src/codegraph/cli/common.py::_emit
        * → code-graph/src/codegraph/cli/common.py::_color_enabled
        * → code-graph/src/codegraph/cli/common.py::_json_text
    * → code-graph/src/codegraph/cli/common.py::_root
        * → code-graph/src/codegraph/pipeline/incremental.py::find_project_root
            * → code-graph/src/codegraph/pipeline/incremental.py::find_repo_root
                * → code-graph/src/codegraph/pipeline/incremental.py::find_svn_root
    * → code-graph/src/codegraph/cli/common.py::_store
        * → code-graph/src/codegraph/graph/graph_store.py::GraphStore
        * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.close
            * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.close
        * → code-graph/src/codegraph/pipeline/incremental.py::get_db_path
            * → code-graph/src/codegraph/pipeline/incremental.py::get_data_dir
                * → code-graph/src/codegraph/pipeline/incremental.py::_write_data_dir_gitignore
    * → code-graph/src/codegraph/cli/common.py::_with_hints
        * → code-graph/src/codegraph/cli/hints.py::generate_hints
            * → code-graph/src/codegraph/cli/hints.py::_build_next_steps
            * → code-graph/src/codegraph/cli/hints.py::_build_related
            * → code-graph/src/codegraph/cli/hints.py::_extract_warnings
    * → code-graph/src/codegraph/query/analysis.py::find_surprising_connections
        * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.get_all_community_ids
        * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.get_all_edges
            * → code-graph/src/codegraph/graph/graph_store.py::GraphStore._row_to_edge
                * → code-graph/src/codegraph/graph/graph_store.py::GraphEdge
        * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.get_all_nodes
            * → code-graph/src/codegraph/graph/graph_store.py::GraphStore._row_to_node
                * → code-graph/src/codegraph/graph/graph_store.py::GraphNode
        * → code-graph/src/codegraph/graph/graph_store.py::_sanitize_name
<!-- CTX-SYNC:FLOW-END -->

<!-- CTX-SYNC:FLOW-START -->
* flow=code-graph/src/codegraph/cli/change.py::change_flows
    * entry_point=code-graph/src/codegraph/cli/change.py::change_flows
    * criticality=0.7412
    * nodes=32/35
* code-graph/src/codegraph/cli/change.py::change_flows
    * → code-graph/src/codegraph/cli/common.py::_emit
        * → code-graph/src/codegraph/cli/common.py::_color_enabled
        * → code-graph/src/codegraph/cli/common.py::_json_text
    * → code-graph/src/codegraph/cli/common.py::_root
        * → code-graph/src/codegraph/pipeline/incremental.py::find_project_root
            * → code-graph/src/codegraph/pipeline/incremental.py::find_repo_root
                * → code-graph/src/codegraph/pipeline/incremental.py::find_svn_root
    * → code-graph/src/codegraph/cli/common.py::_store
        * → code-graph/src/codegraph/graph/graph_store.py::GraphStore
        * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.close
            * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.close
        * → code-graph/src/codegraph/pipeline/incremental.py::get_db_path
            * → code-graph/src/codegraph/pipeline/incremental.py::get_data_dir
                * → code-graph/src/codegraph/pipeline/incremental.py::_write_data_dir_gitignore
    * → code-graph/src/codegraph/cli/common.py::_with_hints
        * → code-graph/src/codegraph/cli/hints.py::generate_hints
            * → code-graph/src/codegraph/cli/hints.py::_build_next_steps
            * → code-graph/src/codegraph/cli/hints.py::_build_related
            * → code-graph/src/codegraph/cli/hints.py::_extract_warnings
    * → code-graph/src/codegraph/query/flows.py::affected_flows
        * → code-graph/src/codegraph/pipeline/incremental.py::get_changed_files
            * → code-graph/src/codegraph/pipeline/incremental.py::_get_svn_changed_files
            * → code-graph/src/codegraph/pipeline/incremental.py::detect_vcs
        * → code-graph/src/codegraph/pipeline/incremental.py::get_staged_and_unstaged
            * → code-graph/src/codegraph/pipeline/incremental.py::_get_svn_changed_files
            * → code-graph/src/codegraph/pipeline/incremental.py::detect_vcs
        * → code-graph/src/codegraph/query/flows.py::get_affected_flows
            * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.get_flow_ids_by_node_ids
            * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.get_node_ids_by_files
            * → code-graph/src/codegraph/query/flows.py::get_flow_by_id
                * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.get_node_by_id
                    * → code-graph/src/codegraph/graph/graph_store.py::GraphStore._row_to_node
                        * → code-graph/src/codegraph/graph/graph_store.py::GraphNode
                * → code-graph/src/codegraph/graph/graph_store.py::_sanitize_name
<!-- CTX-SYNC:FLOW-END -->

<!-- CTX-SYNC:FLOW-START -->
* flow=code-graph/src/codegraph/cli/change.py::change_impact
    * entry_point=code-graph/src/codegraph/cli/change.py::change_impact
    * criticality=0.725
    * nodes=30/37
* code-graph/src/codegraph/cli/change.py::change_impact
    * → code-graph/src/codegraph/cli/common.py::_emit
        * → code-graph/src/codegraph/cli/common.py::_color_enabled
        * → code-graph/src/codegraph/cli/common.py::_json_text
    * → code-graph/src/codegraph/cli/common.py::_root
        * → code-graph/src/codegraph/pipeline/incremental.py::find_project_root
            * → code-graph/src/codegraph/pipeline/incremental.py::find_repo_root
                * → code-graph/src/codegraph/pipeline/incremental.py::find_svn_root
    * → code-graph/src/codegraph/query/analysis.py::get_impact_radius
        * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.close
            * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.close
        * → code-graph/src/codegraph/graph/graph_store.py::edge_to_dict
            * → code-graph/src/codegraph/graph/graph_store.py::_sanitize_name
        * → code-graph/src/codegraph/graph/graph_store.py::node_to_dict
            * → code-graph/src/codegraph/graph/graph_store.py::_sanitize_name
        * → code-graph/src/codegraph/lib/repo.py::open_store
            * → code-graph/src/codegraph/graph/graph_store.py::GraphStore
            * → code-graph/src/codegraph/lib/repo.py::resolve_root
                * → code-graph/src/codegraph/lib/repo.py::validate_repo_root
                * → code-graph/src/codegraph/pipeline/incremental.py::find_project_root
            * → code-graph/src/codegraph/pipeline/incremental.py::get_db_path
                * → code-graph/src/codegraph/pipeline/incremental.py::get_data_dir
                    * → code-graph/src/codegraph/pipeline/incremental.py::_write_data_dir_gitignore
        * → code-graph/src/codegraph/lib/repo.py::resolve_graph_file_paths
            * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.get_files_matching
            * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.get_nodes_by_file
                * → code-graph/src/codegraph/graph/graph_store.py::GraphStore._row_to_node
                    * → code-graph/src/codegraph/graph/graph_store.py::GraphNode
            * → code-graph/src/codegraph/lib/repo.py::add
                * → code-graph/src/codegraph/lib/repo.py::add
        * → code-graph/src/codegraph/pipeline/incremental.py::get_changed_files
            * → code-graph/src/codegraph/pipeline/incremental.py::_get_svn_changed_files
            * → code-graph/src/codegraph/pipeline/incremental.py::detect_vcs
        * → code-graph/src/codegraph/pipeline/incremental.py::get_staged_and_unstaged
            * → code-graph/src/codegraph/pipeline/incremental.py::_get_svn_changed_files
            * → code-graph/src/codegraph/pipeline/incremental.py::detect_vcs
        * → code-graph/src/codegraph/query/analysis.py::get_impact_radius
<!-- CTX-SYNC:FLOW-END -->

<!-- CTX-SYNC:FLOW-START -->
* flow=code-graph/src/codegraph/cli/change.py::change_risk
    * entry_point=code-graph/src/codegraph/cli/change.py::change_risk
    * criticality=0.7147
    * nodes=51/61
* code-graph/src/codegraph/cli/change.py::change_risk
    * → code-graph/src/codegraph/changes.py::detect_changes
        * → code-graph/src/codegraph/changes.py::analyze_changes
            * → code-graph/src/codegraph/changes.py::compute_risk_score
                * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.count_flow_memberships
                * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.get_community_ids_by_qualified_names
                * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.get_edges_by_target
                * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.get_flow_criticalities_for_node
                * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.get_node_community_id
                * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.get_transitive_tests
                    * → code-graph/src/codegraph/graph/graph_store.py::GraphStore._node_dict
            * → code-graph/src/codegraph/changes.py::map_changes_to_nodes
                * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.get_files_matching
                * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.get_nodes_by_file
            * → code-graph/src/codegraph/changes.py::parse_diff_ranges
            * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.get_edges_by_target
                * → code-graph/src/codegraph/graph/graph_store.py::GraphStore._row_to_edge
                    * → code-graph/src/codegraph/graph/graph_store.py::GraphEdge
            * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.get_nodes_by_file
                * → code-graph/src/codegraph/graph/graph_store.py::GraphStore._row_to_node
                    * → code-graph/src/codegraph/graph/graph_store.py::GraphNode
            * → code-graph/src/codegraph/graph/graph_store.py::_sanitize_name
            * → code-graph/src/codegraph/graph/graph_store.py::node_to_dict
                * → code-graph/src/codegraph/graph/graph_store.py::_sanitize_name
            * → code-graph/src/codegraph/query/flows.py::get_affected_flows
                * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.get_flow_ids_by_node_ids
                * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.get_node_ids_by_files
                * → code-graph/src/codegraph/query/flows.py::get_flow_by_id
                    * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.get_node_by_id
                        * → code-graph/src/codegraph/graph/graph_store.py::GraphStore._row_to_node
                    * → code-graph/src/codegraph/graph/graph_store.py::_sanitize_name
        * → code-graph/src/codegraph/changes.py::parse_diff_ranges
            * → code-graph/src/codegraph/changes.py::parse_git_diff_ranges
                * → code-graph/src/codegraph/changes.py::_parse_unified_diff
            * → code-graph/src/codegraph/changes.py::parse_svn_diff_ranges
                * → code-graph/src/codegraph/changes.py::_parse_unified_diff
        * → code-graph/src/codegraph/pipeline/incremental.py::get_changed_files
            * → code-graph/src/codegraph/pipeline/incremental.py::_get_svn_changed_files
            * → code-graph/src/codegraph/pipeline/incremental.py::detect_vcs
        * → code-graph/src/codegraph/pipeline/incremental.py::get_staged_and_unstaged
            * → code-graph/src/codegraph/pipeline/incremental.py::_get_svn_changed_files
            * → code-graph/src/codegraph/pipeline/incremental.py::detect_vcs
    * → code-graph/src/codegraph/cli/common.py::_emit
        * → code-graph/src/codegraph/cli/common.py::_color_enabled
        * → code-graph/src/codegraph/cli/common.py::_json_text
    * → code-graph/src/codegraph/cli/common.py::_root
        * → code-graph/src/codegraph/pipeline/incremental.py::find_project_root
            * → code-graph/src/codegraph/pipeline/incremental.py::find_repo_root
                * → code-graph/src/codegraph/pipeline/incremental.py::find_svn_root
    * → code-graph/src/codegraph/cli/common.py::_store
        * → code-graph/src/codegraph/graph/graph_store.py::GraphStore
        * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.close
            * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.close
        * → code-graph/src/codegraph/pipeline/incremental.py::get_db_path
            * → code-graph/src/codegraph/pipeline/incremental.py::get_data_dir
                * → code-graph/src/codegraph/pipeline/incremental.py::_write_data_dir_gitignore
    * → code-graph/src/codegraph/cli/common.py::_with_hints
        * → code-graph/src/codegraph/cli/hints.py::generate_hints
            * → code-graph/src/codegraph/cli/hints.py::_build_next_steps
            * → code-graph/src/codegraph/cli/hints.py::_build_related
            * → code-graph/src/codegraph/cli/hints.py::_extract_warnings
<!-- CTX-SYNC:FLOW-END -->

<!-- CTX-SYNC:FLOW-START -->
* flow=code-graph/src/codegraph/cli/flows.py::flows_list
    * entry_point=code-graph/src/codegraph/cli/flows.py::flows_list
    * criticality=0.7163
    * nodes=19/19
* code-graph/src/codegraph/cli/flows.py::flows_list
    * → code-graph/src/codegraph/cli/common.py::_emit
        * → code-graph/src/codegraph/cli/common.py::_color_enabled
        * → code-graph/src/codegraph/cli/common.py::_json_text
    * → code-graph/src/codegraph/cli/common.py::_root
        * → code-graph/src/codegraph/pipeline/incremental.py::find_project_root
            * → code-graph/src/codegraph/pipeline/incremental.py::find_repo_root
                * → code-graph/src/codegraph/pipeline/incremental.py::find_svn_root
    * → code-graph/src/codegraph/cli/common.py::_with_hints
        * → code-graph/src/codegraph/cli/hints.py::generate_hints
            * → code-graph/src/codegraph/cli/hints.py::_build_next_steps
            * → code-graph/src/codegraph/cli/hints.py::_build_related
            * → code-graph/src/codegraph/cli/hints.py::_extract_warnings
    * → code-graph/src/codegraph/graph/graph_store.py::GraphStore
    * → code-graph/src/codegraph/pipeline/incremental.py::get_db_path
        * → code-graph/src/codegraph/pipeline/incremental.py::get_data_dir
            * → code-graph/src/codegraph/pipeline/incremental.py::_write_data_dir_gitignore
    * → code-graph/src/codegraph/query/flows.py::get_flows
        * → code-graph/src/codegraph/graph/graph_store.py::_sanitize_name
<!-- CTX-SYNC:FLOW-END -->

<!-- CTX-SYNC:FLOW-START -->
* flow=code-graph/src/codegraph/cli/flows.py::flows_show
    * entry_point=code-graph/src/codegraph/cli/flows.py::flows_show
    * criticality=0.7127
    * nodes=22/22
* code-graph/src/codegraph/cli/flows.py::flows_show
    * → code-graph/src/codegraph/cli/common.py::_emit
        * → code-graph/src/codegraph/cli/common.py::_color_enabled
        * → code-graph/src/codegraph/cli/common.py::_json_text
    * → code-graph/src/codegraph/cli/common.py::_root
        * → code-graph/src/codegraph/pipeline/incremental.py::find_project_root
            * → code-graph/src/codegraph/pipeline/incremental.py::find_repo_root
                * → code-graph/src/codegraph/pipeline/incremental.py::find_svn_root
    * → code-graph/src/codegraph/cli/common.py::_with_hints
        * → code-graph/src/codegraph/cli/hints.py::generate_hints
            * → code-graph/src/codegraph/cli/hints.py::_build_next_steps
            * → code-graph/src/codegraph/cli/hints.py::_build_related
            * → code-graph/src/codegraph/cli/hints.py::_extract_warnings
    * → code-graph/src/codegraph/graph/graph_store.py::GraphStore
    * → code-graph/src/codegraph/pipeline/incremental.py::get_db_path
        * → code-graph/src/codegraph/pipeline/incremental.py::get_data_dir
            * → code-graph/src/codegraph/pipeline/incremental.py::_write_data_dir_gitignore
    * → code-graph/src/codegraph/query/flows.py::get_flow_by_id
        * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.get_node_by_id
            * → code-graph/src/codegraph/graph/graph_store.py::GraphStore._row_to_node
                * → code-graph/src/codegraph/graph/graph_store.py::GraphNode
        * → code-graph/src/codegraph/graph/graph_store.py::_sanitize_name
<!-- CTX-SYNC:FLOW-END -->

<!-- CTX-SYNC:FLOW-START -->
* flow=code-graph/src/codegraph/cli/health.py::dead_code
    * entry_point=code-graph/src/codegraph/cli/health.py::dead_code
    * criticality=0.7103
    * nodes=37/44
* code-graph/src/codegraph/cli/health.py::dead_code
    * → code-graph/src/codegraph/cli/common.py::_emit
        * → code-graph/src/codegraph/cli/common.py::_color_enabled
        * → code-graph/src/codegraph/cli/common.py::_json_text
    * → code-graph/src/codegraph/cli/common.py::_root
        * → code-graph/src/codegraph/pipeline/incremental.py::find_project_root
            * → code-graph/src/codegraph/pipeline/incremental.py::find_repo_root
                * → code-graph/src/codegraph/pipeline/incremental.py::find_svn_root
    * → code-graph/src/codegraph/cli/common.py::_store
        * → code-graph/src/codegraph/graph/graph_store.py::GraphStore
        * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.close
            * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.close
        * → code-graph/src/codegraph/pipeline/incremental.py::get_db_path
            * → code-graph/src/codegraph/pipeline/incremental.py::get_data_dir
                * → code-graph/src/codegraph/pipeline/incremental.py::_write_data_dir_gitignore
    * → code-graph/src/codegraph/cli/common.py::_with_hints
        * → code-graph/src/codegraph/cli/hints.py::generate_hints
            * → code-graph/src/codegraph/cli/hints.py::_build_next_steps
            * → code-graph/src/codegraph/cli/hints.py::_build_related
            * → code-graph/src/codegraph/cli/hints.py::_extract_warnings
    * → code-graph/src/codegraph/refactor.py::find_dead_code
        * → code-graph/src/codegraph/graph/graph_store.py::GraphStore._row_to_edge
            * → code-graph/src/codegraph/graph/graph_store.py::GraphEdge
        * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.get_edges_by_source
            * → code-graph/src/codegraph/graph/graph_store.py::GraphStore._row_to_edge
        * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.get_edges_by_target
            * → code-graph/src/codegraph/graph/graph_store.py::GraphStore._row_to_edge
        * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.get_node
            * → code-graph/src/codegraph/graph/graph_store.py::GraphStore._row_to_node
                * → code-graph/src/codegraph/graph/graph_store.py::GraphNode
        * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.get_nodes_by_kind
            * → code-graph/src/codegraph/graph/graph_store.py::GraphStore._row_to_node
        * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.search_edges_by_target_name
            * → code-graph/src/codegraph/graph/graph_store.py::GraphStore._row_to_edge
        * → code-graph/src/codegraph/graph/graph_store.py::_sanitize_name
        * → code-graph/src/codegraph/query/flows.py::_has_framework_decorator
        * → code-graph/src/codegraph/refactor.py::_collect_type_referenced_names
            * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.get_nodes_by_kind
        * → code-graph/src/codegraph/refactor.py::_is_entry_point
            * → code-graph/src/codegraph/query/flows.py::_has_framework_decorator
            * → code-graph/src/codegraph/query/flows.py::_matches_entry_name
        * → code-graph/src/codegraph/refactor.py::_is_plausible_caller
            * → code-graph/src/codegraph/refactor.py::_path_segments
        * → code-graph/src/codegraph/refactor.py::_is_test_file
<!-- CTX-SYNC:FLOW-END -->

<!-- CTX-SYNC:FLOW-START -->
* flow=code-graph/src/codegraph/cli/health.py::knowledge_gaps
    * entry_point=code-graph/src/codegraph/cli/health.py::knowledge_gaps
    * criticality=0.7072
    * nodes=29/30
* code-graph/src/codegraph/cli/health.py::knowledge_gaps
    * → code-graph/src/codegraph/cli/common.py::_emit
        * → code-graph/src/codegraph/cli/common.py::_color_enabled
        * → code-graph/src/codegraph/cli/common.py::_json_text
    * → code-graph/src/codegraph/cli/common.py::_root
        * → code-graph/src/codegraph/pipeline/incremental.py::find_project_root
            * → code-graph/src/codegraph/pipeline/incremental.py::find_repo_root
                * → code-graph/src/codegraph/pipeline/incremental.py::find_svn_root
    * → code-graph/src/codegraph/cli/common.py::_store
        * → code-graph/src/codegraph/graph/graph_store.py::GraphStore
        * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.close
            * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.close
        * → code-graph/src/codegraph/pipeline/incremental.py::get_db_path
            * → code-graph/src/codegraph/pipeline/incremental.py::get_data_dir
                * → code-graph/src/codegraph/pipeline/incremental.py::_write_data_dir_gitignore
    * → code-graph/src/codegraph/cli/common.py::_with_hints
        * → code-graph/src/codegraph/cli/hints.py::generate_hints
            * → code-graph/src/codegraph/cli/hints.py::_build_next_steps
            * → code-graph/src/codegraph/cli/hints.py::_build_related
            * → code-graph/src/codegraph/cli/hints.py::_extract_warnings
    * → code-graph/src/codegraph/query/analysis.py::find_knowledge_gaps
        * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.get_all_community_ids
        * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.get_all_edges
            * → code-graph/src/codegraph/graph/graph_store.py::GraphStore._row_to_edge
                * → code-graph/src/codegraph/graph/graph_store.py::GraphEdge
        * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.get_all_nodes
            * → code-graph/src/codegraph/graph/graph_store.py::GraphStore._row_to_node
                * → code-graph/src/codegraph/graph/graph_store.py::GraphNode
        * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.get_communities_list
        * → code-graph/src/codegraph/graph/graph_store.py::_sanitize_name
<!-- CTX-SYNC:FLOW-END -->

<!-- CTX-SYNC:FLOW-START -->
* flow=code-graph/src/codegraph/cli/health.py::large_functions
    * entry_point=code-graph/src/codegraph/cli/health.py::large_functions
    * criticality=0.7341
    * nodes=22/24
* code-graph/src/codegraph/cli/health.py::large_functions
    * → code-graph/src/codegraph/cli/common.py::_emit
        * → code-graph/src/codegraph/cli/common.py::_color_enabled
        * → code-graph/src/codegraph/cli/common.py::_json_text
    * → code-graph/src/codegraph/cli/common.py::_root
        * → code-graph/src/codegraph/pipeline/incremental.py::find_project_root
            * → code-graph/src/codegraph/pipeline/incremental.py::find_repo_root
                * → code-graph/src/codegraph/pipeline/incremental.py::find_svn_root
    * → code-graph/src/codegraph/query/analysis.py::find_large_functions
        * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.close
            * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.close
        * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.get_nodes_by_size
            * → code-graph/src/codegraph/graph/graph_store.py::GraphStore._row_to_node
                * → code-graph/src/codegraph/graph/graph_store.py::GraphNode
        * → code-graph/src/codegraph/graph/graph_store.py::node_to_dict
            * → code-graph/src/codegraph/graph/graph_store.py::_sanitize_name
        * → code-graph/src/codegraph/lib/repo.py::open_store
            * → code-graph/src/codegraph/graph/graph_store.py::GraphStore
            * → code-graph/src/codegraph/lib/repo.py::resolve_root
                * → code-graph/src/codegraph/lib/repo.py::validate_repo_root
                * → code-graph/src/codegraph/pipeline/incremental.py::find_project_root
            * → code-graph/src/codegraph/pipeline/incremental.py::get_db_path
                * → code-graph/src/codegraph/pipeline/incremental.py::get_data_dir
                    * → code-graph/src/codegraph/pipeline/incremental.py::_write_data_dir_gitignore
<!-- CTX-SYNC:FLOW-END -->

<!-- CTX-SYNC:FLOW-START -->
* flow=code-graph/src/codegraph/cli/health.py::suggested_questions
    * entry_point=code-graph/src/codegraph/cli/health.py::suggested_questions
    * criticality=0.7441
    * nodes=34/47
* code-graph/src/codegraph/cli/health.py::suggested_questions
    * → code-graph/src/codegraph/cli/common.py::_emit
        * → code-graph/src/codegraph/cli/common.py::_color_enabled
        * → code-graph/src/codegraph/cli/common.py::_json_text
    * → code-graph/src/codegraph/cli/common.py::_root
        * → code-graph/src/codegraph/pipeline/incremental.py::find_project_root
            * → code-graph/src/codegraph/pipeline/incremental.py::find_repo_root
                * → code-graph/src/codegraph/pipeline/incremental.py::find_svn_root
    * → code-graph/src/codegraph/cli/common.py::_store
        * → code-graph/src/codegraph/graph/graph_store.py::GraphStore
        * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.close
            * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.close
        * → code-graph/src/codegraph/pipeline/incremental.py::get_db_path
            * → code-graph/src/codegraph/pipeline/incremental.py::get_data_dir
                * → code-graph/src/codegraph/pipeline/incremental.py::_write_data_dir_gitignore
    * → code-graph/src/codegraph/cli/common.py::_with_hints
        * → code-graph/src/codegraph/cli/hints.py::generate_hints
            * → code-graph/src/codegraph/cli/hints.py::_build_next_steps
            * → code-graph/src/codegraph/cli/hints.py::_build_related
            * → code-graph/src/codegraph/cli/hints.py::_extract_warnings
    * → code-graph/src/codegraph/query/analysis.py::generate_suggested_questions
        * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.get_all_edges
            * → code-graph/src/codegraph/graph/graph_store.py::GraphStore._row_to_edge
                * → code-graph/src/codegraph/graph/graph_store.py::GraphEdge
        * → code-graph/src/codegraph/query/analysis.py::find_bridge_nodes
            * → code-graph/src/codegraph/graph/graph_store.py::GraphStore._build_networkx_graph
            * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.get_all_community_ids
            * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.get_all_nodes
                * → code-graph/src/codegraph/graph/graph_store.py::GraphStore._row_to_node
                    * → code-graph/src/codegraph/graph/graph_store.py::GraphNode
            * → code-graph/src/codegraph/graph/graph_store.py::_sanitize_name
        * → code-graph/src/codegraph/query/analysis.py::find_hub_nodes
            * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.get_all_community_ids
            * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.get_all_edges
            * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.get_all_nodes
            * → code-graph/src/codegraph/graph/graph_store.py::_sanitize_name
        * → code-graph/src/codegraph/query/analysis.py::find_knowledge_gaps
            * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.get_all_community_ids
            * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.get_all_edges
            * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.get_all_nodes
            * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.get_communities_list
            * → code-graph/src/codegraph/graph/graph_store.py::_sanitize_name
        * → code-graph/src/codegraph/query/analysis.py::find_surprising_connections
            * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.get_all_community_ids
            * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.get_all_edges
            * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.get_all_nodes
            * → code-graph/src/codegraph/graph/graph_store.py::_sanitize_name
<!-- CTX-SYNC:FLOW-END -->

<!-- CTX-SYNC:FLOW-START -->
* flow=code-graph/src/codegraph/cli/lifecycle.py::build
    * entry_point=code-graph/src/codegraph/cli/lifecycle.py::build
    * criticality=0.7668
    * nodes=224/458
* code-graph/src/codegraph/cli/lifecycle.py::build
    * → code-graph/src/codegraph/cli/common.py::_emit
        * → code-graph/src/codegraph/cli/common.py::_color_enabled
        * → code-graph/src/codegraph/cli/common.py::_json_text
    * → code-graph/src/codegraph/cli/common.py::_root
        * → code-graph/src/codegraph/pipeline/incremental.py::find_project_root
            * → code-graph/src/codegraph/pipeline/incremental.py::find_repo_root
                * → code-graph/src/codegraph/pipeline/incremental.py::find_svn_root
    * → code-graph/src/codegraph/cli/common.py::_store
        * → code-graph/src/codegraph/graph/graph_store.py::GraphStore
        * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.close
            * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.close
        * → code-graph/src/codegraph/pipeline/incremental.py::get_db_path
            * → code-graph/src/codegraph/pipeline/incremental.py::get_data_dir
                * → code-graph/src/codegraph/pipeline/incremental.py::_write_data_dir_gitignore
    * → code-graph/src/codegraph/pipeline/build.py::build_or_update
        * → code-graph/src/codegraph/pipeline/incremental.py::full_build
            * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.commit
                * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.commit
            * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.get_all_files
            * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.remove_file_data
                * → code-graph/src/codegraph/graph/graph_store.py::GraphStore._invalidate_cache
            * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.set_metadata
                * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.commit
            * → code-graph/src/codegraph/parsing/parser.py::CodeParser
            * → code-graph/src/codegraph/parsing/parser.py::CodeParser.parse_bytes
                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._collect_file_scope
                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._collect_import_names
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._collect_js_import_names
                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._collect_js_exported_local_names
                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._get_name
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._get_name
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._leaf_name
                            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._leaf_name
                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._collect_type_bindings
                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._simple_type_name
                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser.bind
                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._extract_from_tree
                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._extract_bash_source_command
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._resolve_module_to_file
                            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._do_resolve_module
                                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._find_dart_pubspec_root
                        * → code-graph/src/codegraph/parsing/parser.py::EdgeInfo
                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._extract_calls
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._extract_from_tree
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._get_base_call_name
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._get_call_name
                            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._get_custom_call_name
                            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._normalize_php_name
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._get_call_receiver
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._get_java_method_and_receiver
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._get_test_description
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._qualify
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._resolve_call_target
                            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._qualify
                            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._resolve_imported_symbol
                                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._qualify
                                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._resolve_exported_symbol
                                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._collect_file_scope
                                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._get_parser
                                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._qualify
                                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._resolve_exported_symbol
                                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._resolve_imported_symbol
                                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._resolve_module_to_file
                                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser.detect_language
                                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._resolve_module_to_file
                        * → code-graph/src/codegraph/parsing/parser.py::EdgeInfo
                        * → code-graph/src/codegraph/parsing/parser.py::NodeInfo
                        * → code-graph/src/codegraph/parsing/parser.py::_is_test_file
                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._extract_classes
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._emit_kafka_edges_from_class
                            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._qualify
                            * → code-graph/src/codegraph/parsing/parser.py::EdgeInfo
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._emit_spring_injections
                            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._emit_spring_constructor_injection
                                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._get_java_annotations
                                * → code-graph/src/codegraph/parsing/parser.py::EdgeInfo
                            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._emit_spring_field_injection
                                * → code-graph/src/codegraph/parsing/parser.py::EdgeInfo
                            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._qualify
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._emit_temporal_stub_fields
                            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._qualify
                            * → code-graph/src/codegraph/parsing/parser.py::EdgeInfo
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._extract_from_tree
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._get_bases
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._get_class_decorators
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._get_java_annotations
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._get_name
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._qualify
                        * → code-graph/src/codegraph/parsing/parser.py::EdgeInfo
                        * → code-graph/src/codegraph/parsing/parser.py::NodeInfo
                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._extract_dart_calls_from_children
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._qualify
                        * → code-graph/src/codegraph/parsing/parser.py::EdgeInfo
                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._extract_elixir_constructs
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._elixir_call_identifier
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._elixir_function_name_and_params
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._elixir_module_name
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._extract_from_tree
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._qualify
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._resolve_call_target
                        * → code-graph/src/codegraph/parsing/parser.py::EdgeInfo
                        * → code-graph/src/codegraph/parsing/parser.py::NodeInfo
                        * → code-graph/src/codegraph/parsing/parser.py::_is_test_function
                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._extract_from_tree
                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._extract_functions
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._emit_kafka_edges_from_method
                            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._get_kafka_annotation_topics
                            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._qualify
                            * → code-graph/src/codegraph/parsing/parser.py::EdgeInfo
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._extract_from_tree
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._get_go_receiver_type
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._get_name
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._get_params
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._get_return_type
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._qualify
                        * → code-graph/src/codegraph/parsing/parser.py::EdgeInfo
                        * → code-graph/src/codegraph/parsing/parser.py::NodeInfo
                        * → code-graph/src/codegraph/parsing/parser.py::_is_test_function
                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._extract_imports
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._extract_import
                            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._find_string_literal
                                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._find_string_literal
                            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._import_path_text
                            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._r_call_func_name
                            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._r_first_string_arg
                            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._r_iter_args
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._resolve_module_to_file
                        * → code-graph/src/codegraph/parsing/parser.py::EdgeInfo
                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._extract_js_field_function
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._extract_from_tree
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._get_params
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._qualify
                        * → code-graph/src/codegraph/parsing/parser.py::EdgeInfo
                        * → code-graph/src/codegraph/parsing/parser.py::NodeInfo
                        * → code-graph/src/codegraph/parsing/parser.py::_is_test_function
                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._extract_js_var_functions
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._extract_from_tree
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._get_params
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._get_return_type
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._qualify
                        * → code-graph/src/codegraph/parsing/parser.py::EdgeInfo
                        * → code-graph/src/codegraph/parsing/parser.py::NodeInfo
                        * → code-graph/src/codegraph/parsing/parser.py::_is_test_function
                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._extract_jsx_component_call
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._qualify
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._resolve_jsx_component_target
                            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._get_jsx_component_reference
                                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._get_member_expression_root_name
                                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._get_member_expression_root_name
                                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._looks_like_component_name
                            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._resolve_call_target
                            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._resolve_imported_symbol
                        * → code-graph/src/codegraph/parsing/parser.py::EdgeInfo
                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._extract_julia_constructs
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._extract_from_tree
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._julia_call_first_identifier
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._julia_short_func_name
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._julia_string_arg
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._qualify
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._resolve_module_to_file
                        * → code-graph/src/codegraph/parsing/parser.py::EdgeInfo
                        * → code-graph/src/codegraph/parsing/parser.py::NodeInfo
                        * → code-graph/src/codegraph/parsing/parser.py::_is_test_function
                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._extract_lua_constructs
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._handle_lua_table_function
                            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._extract_from_tree
                            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._get_params
                            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._qualify
                            * → code-graph/src/codegraph/parsing/parser.py::EdgeInfo
                            * → code-graph/src/codegraph/parsing/parser.py::NodeInfo
                            * → code-graph/src/codegraph/parsing/parser.py::_is_test_function
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._handle_lua_variable_declaration
                            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._extract_from_tree
                            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._get_params
                            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._lua_get_require_target
                            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._qualify
                            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._resolve_module_to_file
                            * → code-graph/src/codegraph/parsing/parser.py::EdgeInfo
                            * → code-graph/src/codegraph/parsing/parser.py::NodeInfo
                            * → code-graph/src/codegraph/parsing/parser.py::_is_test_function
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._lua_get_require_target
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._resolve_module_to_file
                        * → code-graph/src/codegraph/parsing/parser.py::EdgeInfo
                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._extract_nix_constructs
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._extract_from_tree
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._extract_nix_flake_input_urls
                            * → code-graph/src/codegraph/parsing/parser.py::CodeParser.visit
                                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._nix_attrpath_parts
                                * → code-graph/src/codegraph/parsing/parser.py::CodeParser.first_path_arg
                                * → code-graph/src/codegraph/parsing/parser.py::CodeParser.head_call_name
                                * → code-graph/src/codegraph/parsing/parser.py::CodeParser.visit
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._extract_nix_import_targets
                            * → code-graph/src/codegraph/parsing/parser.py::CodeParser.visit
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._is_nix_flake_file
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._nix_attrpath_parts
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._qualify
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._resolve_module_to_file
                        * → code-graph/src/codegraph/parsing/parser.py::EdgeInfo
                        * → code-graph/src/codegraph/parsing/parser.py::NodeInfo
                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._extract_pascal_constructs
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._extract_from_tree
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._pascal_call_parts
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._pascal_params
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._pascal_proc_name
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._pascal_return_type
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._pascal_type_name
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._qualify
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._resolve_call_target
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._resolve_pascal_unit
                            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._pascal_dir_listing
                        * → code-graph/src/codegraph/parsing/parser.py::EdgeInfo
                        * → code-graph/src/codegraph/parsing/parser.py::NodeInfo
                        * → code-graph/src/codegraph/parsing/parser.py::_is_test_function
                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._extract_r_constructs
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._handle_r_binary_operator
                            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._extract_from_tree
                            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._get_params
                            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._handle_r_class_call
                                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._extract_r_methods
                                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._extract_from_tree
                                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._get_params
                                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._qualify
                                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._r_iter_args
                                    * → code-graph/src/codegraph/parsing/parser.py::EdgeInfo
                                    * → code-graph/src/codegraph/parsing/parser.py::NodeInfo
                                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._qualify
                                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._r_find_named_arg
                                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._r_iter_args
                                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._r_first_string_arg
                                * → code-graph/src/codegraph/parsing/parser.py::EdgeInfo
                                * → code-graph/src/codegraph/parsing/parser.py::NodeInfo
                            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._qualify
                            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._r_call_func_name
                            * → code-graph/src/codegraph/parsing/parser.py::EdgeInfo
                            * → code-graph/src/codegraph/parsing/parser.py::NodeInfo
                            * → code-graph/src/codegraph/parsing/parser.py::_is_test_function
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._handle_r_call
                            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._extract_from_tree
                            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._extract_import
                            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._get_call_name
                            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._handle_r_class_call
                            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._qualify
                            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._r_call_func_name
                            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._resolve_call_target
                            * → code-graph/src/codegraph/parsing/parser.py::EdgeInfo
                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._extract_solidity_constructs
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._qualify
                        * → code-graph/src/codegraph/parsing/parser.py::EdgeInfo
                        * → code-graph/src/codegraph/parsing/parser.py::NodeInfo
                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._extract_value_references
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._emit_reference_if_known
                            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._resolve_call_target
                            * → code-graph/src/codegraph/parsing/parser.py::EdgeInfo
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._qualify
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._ref_from_arguments
                            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._emit_reference_if_known
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._ref_from_array
                            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._emit_reference_if_known
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._ref_from_assignment
                            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._emit_reference_if_known
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._ref_from_pair
                            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._emit_reference_if_known
                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._get_parser
                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._parse_databricks_py_notebook
                    * → code-graph/src/codegraph/parsing/parser.py::CellInfo
                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._parse_notebook_cells
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._collect_file_scope
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._extract_from_tree
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._get_parser
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._qualify
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._resolve_call_targets
                        * → code-graph/src/codegraph/parsing/parser.py::EdgeInfo
                        * → code-graph/src/codegraph/parsing/parser.py::NodeInfo
                        * → code-graph/src/codegraph/parsing/parser.py::_is_test_file
                    * → code-graph/src/codegraph/parsing/parser.py::NodeInfo
                    * → code-graph/src/codegraph/parsing/parser.py::_is_test_file
                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._parse_notebook
                    * → code-graph/src/codegraph/parsing/parser.py::CellInfo
                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._parse_notebook_cells
                    * → code-graph/src/codegraph/parsing/parser.py::NodeInfo
                    * → code-graph/src/codegraph/parsing/parser.py::_is_test_file
                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._parse_rescript
                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._qualify
                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._resolve_call_targets
                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser.enclosing_module
                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser.is_top_level
                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser.offset_to_line
                    * → code-graph/src/codegraph/parsing/parser.py::EdgeInfo
                    * → code-graph/src/codegraph/parsing/parser.py::NodeInfo
                    * → code-graph/src/codegraph/parsing/parser.py::_is_test_file
                    * → code-graph/src/codegraph/parsing/parser.py::_is_test_function
                        * → code-graph/src/codegraph/parsing/parser.py::_is_test_file
                    * → code-graph/src/codegraph/parsing/parser.py::_rescript_brace_depth_array
                    * → code-graph/src/codegraph/parsing/parser.py::_scan_rescript_modules
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser.offset_to_line
                    * → code-graph/src/codegraph/parsing/parser.py::_strip_rescript_noise
                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._parse_sql
                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._get_parser
                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._walk_sql_tree
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._extract_sql_ddl
                            * → code-graph/src/codegraph/parsing/parser.py::EdgeInfo
                            * → code-graph/src/codegraph/parsing/parser.py::NodeInfo
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._walk_sql_tree
                    * → code-graph/src/codegraph/parsing/parser.py::EdgeInfo
                    * → code-graph/src/codegraph/parsing/parser.py::NodeInfo
                    * → code-graph/src/codegraph/parsing/parser.py::_is_test_file
                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._parse_svelte
                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._collect_file_scope
                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._extract_from_tree
                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._get_parser
                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._qualify
                    * → code-graph/src/codegraph/parsing/parser.py::EdgeInfo
                    * → code-graph/src/codegraph/parsing/parser.py::NodeInfo
                    * → code-graph/src/codegraph/parsing/parser.py::_is_test_file
                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._parse_vue
                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._collect_file_scope
                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._extract_from_tree
                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._get_parser
                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._qualify
                    * → code-graph/src/codegraph/parsing/parser.py::EdgeInfo
                    * → code-graph/src/codegraph/parsing/parser.py::NodeInfo
                    * → code-graph/src/codegraph/parsing/parser.py::_is_test_file
                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._qualify
                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._resolve_call_targets
                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._qualify
                    * → code-graph/src/codegraph/parsing/parser.py::EdgeInfo
                * → code-graph/src/codegraph/parsing/parser.py::CodeParser.detect_language
                * → code-graph/src/codegraph/parsing/parser.py::EdgeInfo
                * → code-graph/src/codegraph/parsing/parser.py::NodeInfo
                * → code-graph/src/codegraph/parsing/parser.py::_is_test_file
            * → code-graph/src/codegraph/pipeline/incremental.py::_PendingWrites
            * → code-graph/src/codegraph/pipeline/incremental.py::_PendingWrites.add
                * → code-graph/src/codegraph/pipeline/incremental.py::_PendingWrites.flush
            * → code-graph/src/codegraph/pipeline/incremental.py::_PendingWrites.flush
            * → code-graph/src/codegraph/pipeline/incremental.py::_make_executor
                * → code-graph/src/codegraph/pipeline/incremental.py::_select_executor_kind
            * → code-graph/src/codegraph/pipeline/incremental.py::_run_receiver_resolver
                * → code-graph/src/codegraph/resolvers/receivers.py::resolve_typed_receivers
            * → code-graph/src/codegraph/pipeline/incremental.py::_run_rescript_resolver
                * → code-graph/src/codegraph/resolvers/rescript.py::resolve_rescript_cross_module
                    * → code-graph/src/codegraph/graph/graph_store.py::GraphStore._invalidate_cache
                    * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.get_all_files
                    * → code-graph/src/codegraph/resolvers/rescript.py::_resolve_call_target
                        * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.get_node
                            * → code-graph/src/codegraph/graph/graph_store.py::GraphStore._row_to_node
                        * → code-graph/src/codegraph/resolvers/rescript.py::_pick_existing_qualified
                            * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.get_node
            * → code-graph/src/codegraph/pipeline/incremental.py::_run_spring_resolver
                * → code-graph/src/codegraph/resolvers/spring.py::resolve_spring_di_calls
            * → code-graph/src/codegraph/pipeline/incremental.py::_run_temporal_resolver
                * → code-graph/src/codegraph/resolvers/temporal.py::resolve_temporal_calls
            * → code-graph/src/codegraph/pipeline/incremental.py::_store_batch_size
            * → code-graph/src/codegraph/pipeline/incremental.py::_store_vcs_metadata
                * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.set_metadata
                * → code-graph/src/codegraph/pipeline/incremental.py::_git_branch_info
                * → code-graph/src/codegraph/pipeline/incremental.py::_svn_revision_info
                * → code-graph/src/codegraph/pipeline/incremental.py::detect_vcs
            * → code-graph/src/codegraph/pipeline/incremental.py::collect_all_files
                * → code-graph/src/codegraph/parsing/parser.py::CodeParser
                * → code-graph/src/codegraph/parsing/parser.py::CodeParser.detect_language
                * → code-graph/src/codegraph/pipeline/incremental.py::_get_candidates
                    * → code-graph/src/codegraph/pipeline/incremental.py::_should_ignore
                * → code-graph/src/codegraph/pipeline/incremental.py::_is_binary
                * → code-graph/src/codegraph/pipeline/incremental.py::_load_ignore_patterns
                * → code-graph/src/codegraph/pipeline/incremental.py::_should_ignore
                * → code-graph/src/codegraph/pipeline/incremental.py::get_all_tracked_files
                    * → code-graph/src/codegraph/pipeline/incremental.py::_get_svn_all_tracked_files
                    * → code-graph/src/codegraph/pipeline/incremental.py::detect_vcs
        * → code-graph/src/codegraph/pipeline/incremental.py::incremental_update
            * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.commit
            * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.get_nodes_by_file
                * → code-graph/src/codegraph/graph/graph_store.py::GraphStore._row_to_node
                    * → code-graph/src/codegraph/graph/graph_store.py::GraphNode
            * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.remove_file_data
            * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.set_metadata
            * → code-graph/src/codegraph/parsing/parser.py::CodeParser
            * → code-graph/src/codegraph/parsing/parser.py::CodeParser.detect_language
                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._detect_language_from_shebang
            * → code-graph/src/codegraph/parsing/parser.py::CodeParser.parse_bytes
            * → code-graph/src/codegraph/pipeline/incremental.py::_PendingWrites
            * → code-graph/src/codegraph/pipeline/incremental.py::_PendingWrites.add
            * → code-graph/src/codegraph/pipeline/incremental.py::_PendingWrites.flush
            * → code-graph/src/codegraph/pipeline/incremental.py::_load_ignore_patterns
            * → code-graph/src/codegraph/pipeline/incremental.py::_make_executor
            * → code-graph/src/codegraph/pipeline/incremental.py::_run_receiver_resolver
            * → code-graph/src/codegraph/pipeline/incremental.py::_run_rescript_resolver
            * → code-graph/src/codegraph/pipeline/incremental.py::_run_spring_resolver
            * → code-graph/src/codegraph/pipeline/incremental.py::_run_temporal_resolver
            * → code-graph/src/codegraph/pipeline/incremental.py::_should_ignore
                * → code-graph/src/codegraph/pipeline/incremental.py::_is_match
            * → code-graph/src/codegraph/pipeline/incremental.py::_store_batch_size
            * → code-graph/src/codegraph/pipeline/incremental.py::_store_vcs_metadata
            * → code-graph/src/codegraph/pipeline/incremental.py::find_dependents
                * → code-graph/src/codegraph/pipeline/incremental.py::DependentList
                * → code-graph/src/codegraph/pipeline/incremental.py::_single_hop_dependents
                    * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.get_edges_by_target
                        * → code-graph/src/codegraph/graph/graph_store.py::GraphStore._row_to_edge
                            * → code-graph/src/codegraph/graph/graph_store.py::GraphEdge
                    * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.get_nodes_by_file
                    * → code-graph/src/codegraph/pipeline/incremental.py::_PendingWrites.add
            * → code-graph/src/codegraph/pipeline/incremental.py::get_changed_files
                * → code-graph/src/codegraph/pipeline/incremental.py::_get_svn_changed_files
                * → code-graph/src/codegraph/pipeline/incremental.py::detect_vcs
        * → code-graph/src/codegraph/pipeline/postprocessing.py::run_post_processing
            * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.set_metadata
            * → code-graph/src/codegraph/pipeline/postprocessing.py::_compute_signatures
                * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.commit
                * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.get_nodes_without_signature
                * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.update_node_signature
            * → code-graph/src/codegraph/pipeline/postprocessing.py::_compute_summaries
            * → code-graph/src/codegraph/pipeline/postprocessing.py::_detect_communities
                * → code-graph/src/codegraph/query/communities.py::detect_communities
                    * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.get_all_edges
                        * → code-graph/src/codegraph/graph/graph_store.py::GraphStore._row_to_edge
                    * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.get_all_nodes
                        * → code-graph/src/codegraph/graph/graph_store.py::GraphStore._row_to_node
                    * → code-graph/src/codegraph/query/communities.py::_build_adjacency
                    * → code-graph/src/codegraph/query/communities.py::_detect_file_based
                        * → code-graph/src/codegraph/query/communities.py::_compute_cohesion_batch
                        * → code-graph/src/codegraph/query/communities.py::_generate_community_name
                            * → code-graph/src/codegraph/query/communities.py::_extract_file_prefix
                                * → code-graph/src/codegraph/query/communities.py::_to_slug
                            * → code-graph/src/codegraph/query/communities.py::_extract_keywords
                                * → code-graph/src/codegraph/query/communities.py::_split_name
                            * → code-graph/src/codegraph/query/communities.py::_to_slug
                        * → code-graph/src/codegraph/query/communities.py::_group_at_depth
                    * → code-graph/src/codegraph/query/communities.py::_detect_leiden
                        * → code-graph/src/codegraph/query/communities.py::_compute_cohesion_batch
                        * → code-graph/src/codegraph/query/communities.py::_detect_file_based
                        * → code-graph/src/codegraph/query/communities.py::_generate_community_name
                    * → code-graph/src/codegraph/query/communities.py::_split_oversized
                * → code-graph/src/codegraph/query/communities.py::incremental_detect_communities
                    * → code-graph/src/codegraph/query/communities.py::detect_communities
                    * → code-graph/src/codegraph/query/communities.py::store_communities
                * → code-graph/src/codegraph/query/communities.py::store_communities
            * → code-graph/src/codegraph/pipeline/postprocessing.py::_rebuild_fts_index
                * → code-graph/src/codegraph/query/search.py::rebuild_fts_index
            * → code-graph/src/codegraph/pipeline/postprocessing.py::_trace_flows
                * → code-graph/src/codegraph/query/flows.py::incremental_trace_flows
                    * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.load_flow_adjacency
                        * → code-graph/src/codegraph/graph/graph_store.py::FlowAdjacency
                        * → code-graph/src/codegraph/graph/graph_store.py::GraphStore._row_to_node
                    * → code-graph/src/codegraph/query/flows.py::_trace_single_flow
                        * → code-graph/src/codegraph/graph/graph_store.py::_sanitize_name
                        * → code-graph/src/codegraph/query/flows.py::compute_criticality
                    * → code-graph/src/codegraph/query/flows.py::detect_entry_points
                        * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.get_all_call_targets
                        * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.get_nodes_by_kind
                            * → code-graph/src/codegraph/graph/graph_store.py::GraphStore._row_to_node
                        * → code-graph/src/codegraph/query/flows.py::_has_framework_decorator
                        * → code-graph/src/codegraph/query/flows.py::_is_test_file
                        * → code-graph/src/codegraph/query/flows.py::_matches_entry_name
                * → code-graph/src/codegraph/query/flows.py::store_flows
                * → code-graph/src/codegraph/query/flows.py::trace_flows
                    * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.load_flow_adjacency
                    * → code-graph/src/codegraph/query/flows.py::_trace_single_flow
                    * → code-graph/src/codegraph/query/flows.py::detect_entry_points
<!-- CTX-SYNC:FLOW-END -->

<!-- CTX-SYNC:FLOW-START -->
* flow=code-graph/src/codegraph/cli/lifecycle.py::export_graph
    * entry_point=code-graph/src/codegraph/cli/lifecycle.py::export_graph
    * criticality=0.7398
    * nodes=42/54
* code-graph/src/codegraph/cli/lifecycle.py::export_graph
    * → code-graph/src/codegraph/cli/common.py::_emit
        * → code-graph/src/codegraph/cli/common.py::_color_enabled
        * → code-graph/src/codegraph/cli/common.py::_json_text
    * → code-graph/src/codegraph/cli/common.py::_json_text
    * → code-graph/src/codegraph/cli/common.py::_root
        * → code-graph/src/codegraph/pipeline/incremental.py::find_project_root
            * → code-graph/src/codegraph/pipeline/incremental.py::find_repo_root
                * → code-graph/src/codegraph/pipeline/incremental.py::find_svn_root
    * → code-graph/src/codegraph/cli/lifecycle.py::_export_payload
        * → code-graph/src/codegraph/graph/graph_store.py::GraphStore
        * → code-graph/src/codegraph/pipeline/export.py::build_payload
            * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.get_metadata
            * → code-graph/src/codegraph/pipeline/export.py::_call_graph_payload
                * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.get_all_edges
                    * → code-graph/src/codegraph/graph/graph_store.py::GraphStore._row_to_edge
                        * → code-graph/src/codegraph/graph/graph_store.py::GraphEdge
                * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.get_all_nodes
                    * → code-graph/src/codegraph/graph/graph_store.py::GraphStore._row_to_node
                        * → code-graph/src/codegraph/graph/graph_store.py::GraphNode
                * → code-graph/src/codegraph/graph/graph_store.py::edge_to_dict
                    * → code-graph/src/codegraph/graph/graph_store.py::_sanitize_name
                * → code-graph/src/codegraph/graph/graph_store.py::node_to_dict
                    * → code-graph/src/codegraph/graph/graph_store.py::_sanitize_name
            * → code-graph/src/codegraph/pipeline/export.py::_community_payload
                * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.get_all_nodes
                * → code-graph/src/codegraph/pipeline/export.py::community_key
                * → code-graph/src/codegraph/query/communities.py::get_architecture_overview
                    * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.get_all_edges
                    * → code-graph/src/codegraph/graph/graph_store.py::_sanitize_name
                    * → code-graph/src/codegraph/query/communities.py::_is_test_community
                    * → code-graph/src/codegraph/query/communities.py::get_communities
                        * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.get_community_member_qns
                        * → code-graph/src/codegraph/graph/graph_store.py::_sanitize_name
            * → code-graph/src/codegraph/pipeline/export.py::_flows_payload
                * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.get_node_by_id
                    * → code-graph/src/codegraph/graph/graph_store.py::GraphStore._row_to_node
                * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.load_flow_adjacency
                    * → code-graph/src/codegraph/graph/graph_store.py::FlowAdjacency
                    * → code-graph/src/codegraph/graph/graph_store.py::GraphStore._row_to_node
                * → code-graph/src/codegraph/pipeline/export.py::_flow_tree
                * → code-graph/src/codegraph/pipeline/export.py::_tree_size
                    * → code-graph/src/codegraph/pipeline/export.py::_tree_size
                * → code-graph/src/codegraph/pipeline/export.py::flow_key
                * → code-graph/src/codegraph/query/flows.py::get_flows
                    * → code-graph/src/codegraph/graph/graph_store.py::_sanitize_name
            * → code-graph/src/codegraph/pipeline/export.py::canonical_graph_type
        * → code-graph/src/codegraph/pipeline/incremental.py::get_db_path
            * → code-graph/src/codegraph/pipeline/incremental.py::get_data_dir
                * → code-graph/src/codegraph/pipeline/incremental.py::_write_data_dir_gitignore
    * → code-graph/src/codegraph/cli/lifecycle.py::_write_json
        * → code-graph/src/codegraph/cli/common.py::_json_text
    * → code-graph/src/codegraph/pipeline/export.py::canonical_graph_type
        * → code-graph/src/codegraph/pipeline/export.py::GraphTypeError
<!-- CTX-SYNC:FLOW-END -->

<!-- CTX-SYNC:FLOW-START -->
* flow=code-graph/src/codegraph/cli/lifecycle.py::status
    * entry_point=code-graph/src/codegraph/cli/lifecycle.py::status
    * criticality=0.7313
    * nodes=16/17
* code-graph/src/codegraph/cli/lifecycle.py::status
    * → code-graph/src/codegraph/cli/common.py::_emit
        * → code-graph/src/codegraph/cli/common.py::_color_enabled
        * → code-graph/src/codegraph/cli/common.py::_json_text
    * → code-graph/src/codegraph/cli/common.py::_root
        * → code-graph/src/codegraph/pipeline/incremental.py::find_project_root
            * → code-graph/src/codegraph/pipeline/incremental.py::find_repo_root
                * → code-graph/src/codegraph/pipeline/incremental.py::find_svn_root
    * → code-graph/src/codegraph/query/queries.py::list_graph_stats
        * → code-graph/src/codegraph/lib/repo.py::open_store
            * → code-graph/src/codegraph/graph/graph_store.py::GraphStore
            * → code-graph/src/codegraph/lib/repo.py::resolve_root
                * → code-graph/src/codegraph/lib/repo.py::validate_repo_root
                * → code-graph/src/codegraph/pipeline/incremental.py::find_project_root
            * → code-graph/src/codegraph/pipeline/incremental.py::get_db_path
                * → code-graph/src/codegraph/pipeline/incremental.py::get_data_dir
                    * → code-graph/src/codegraph/pipeline/incremental.py::_write_data_dir_gitignore
<!-- CTX-SYNC:FLOW-END -->

<!-- CTX-SYNC:FLOW-START -->
* flow=code-graph/src/codegraph/cli/lifecycle.py::update
    * entry_point=code-graph/src/codegraph/cli/lifecycle.py::update
    * criticality=0.7668
    * nodes=224/458
* code-graph/src/codegraph/cli/lifecycle.py::update
    * → code-graph/src/codegraph/cli/common.py::_emit
        * → code-graph/src/codegraph/cli/common.py::_color_enabled
        * → code-graph/src/codegraph/cli/common.py::_json_text
    * → code-graph/src/codegraph/cli/common.py::_root
        * → code-graph/src/codegraph/pipeline/incremental.py::find_project_root
            * → code-graph/src/codegraph/pipeline/incremental.py::find_repo_root
                * → code-graph/src/codegraph/pipeline/incremental.py::find_svn_root
    * → code-graph/src/codegraph/cli/common.py::_store
        * → code-graph/src/codegraph/graph/graph_store.py::GraphStore
        * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.close
            * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.close
        * → code-graph/src/codegraph/pipeline/incremental.py::get_db_path
            * → code-graph/src/codegraph/pipeline/incremental.py::get_data_dir
                * → code-graph/src/codegraph/pipeline/incremental.py::_write_data_dir_gitignore
    * → code-graph/src/codegraph/pipeline/build.py::build_or_update
        * → code-graph/src/codegraph/pipeline/incremental.py::full_build
            * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.commit
                * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.commit
            * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.get_all_files
            * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.remove_file_data
                * → code-graph/src/codegraph/graph/graph_store.py::GraphStore._invalidate_cache
            * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.set_metadata
                * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.commit
            * → code-graph/src/codegraph/parsing/parser.py::CodeParser
            * → code-graph/src/codegraph/parsing/parser.py::CodeParser.parse_bytes
                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._collect_file_scope
                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._collect_import_names
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._collect_js_import_names
                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._collect_js_exported_local_names
                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._get_name
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._get_name
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._leaf_name
                            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._leaf_name
                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._collect_type_bindings
                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._simple_type_name
                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser.bind
                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._extract_from_tree
                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._extract_bash_source_command
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._resolve_module_to_file
                            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._do_resolve_module
                                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._find_dart_pubspec_root
                        * → code-graph/src/codegraph/parsing/parser.py::EdgeInfo
                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._extract_calls
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._extract_from_tree
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._get_base_call_name
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._get_call_name
                            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._get_custom_call_name
                            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._normalize_php_name
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._get_call_receiver
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._get_java_method_and_receiver
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._get_test_description
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._qualify
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._resolve_call_target
                            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._qualify
                            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._resolve_imported_symbol
                                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._qualify
                                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._resolve_exported_symbol
                                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._collect_file_scope
                                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._get_parser
                                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._qualify
                                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._resolve_exported_symbol
                                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._resolve_imported_symbol
                                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._resolve_module_to_file
                                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser.detect_language
                                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._resolve_module_to_file
                        * → code-graph/src/codegraph/parsing/parser.py::EdgeInfo
                        * → code-graph/src/codegraph/parsing/parser.py::NodeInfo
                        * → code-graph/src/codegraph/parsing/parser.py::_is_test_file
                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._extract_classes
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._emit_kafka_edges_from_class
                            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._qualify
                            * → code-graph/src/codegraph/parsing/parser.py::EdgeInfo
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._emit_spring_injections
                            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._emit_spring_constructor_injection
                                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._get_java_annotations
                                * → code-graph/src/codegraph/parsing/parser.py::EdgeInfo
                            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._emit_spring_field_injection
                                * → code-graph/src/codegraph/parsing/parser.py::EdgeInfo
                            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._qualify
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._emit_temporal_stub_fields
                            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._qualify
                            * → code-graph/src/codegraph/parsing/parser.py::EdgeInfo
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._extract_from_tree
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._get_bases
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._get_class_decorators
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._get_java_annotations
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._get_name
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._qualify
                        * → code-graph/src/codegraph/parsing/parser.py::EdgeInfo
                        * → code-graph/src/codegraph/parsing/parser.py::NodeInfo
                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._extract_dart_calls_from_children
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._qualify
                        * → code-graph/src/codegraph/parsing/parser.py::EdgeInfo
                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._extract_elixir_constructs
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._elixir_call_identifier
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._elixir_function_name_and_params
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._elixir_module_name
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._extract_from_tree
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._qualify
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._resolve_call_target
                        * → code-graph/src/codegraph/parsing/parser.py::EdgeInfo
                        * → code-graph/src/codegraph/parsing/parser.py::NodeInfo
                        * → code-graph/src/codegraph/parsing/parser.py::_is_test_function
                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._extract_from_tree
                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._extract_functions
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._emit_kafka_edges_from_method
                            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._get_kafka_annotation_topics
                            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._qualify
                            * → code-graph/src/codegraph/parsing/parser.py::EdgeInfo
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._extract_from_tree
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._get_go_receiver_type
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._get_name
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._get_params
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._get_return_type
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._qualify
                        * → code-graph/src/codegraph/parsing/parser.py::EdgeInfo
                        * → code-graph/src/codegraph/parsing/parser.py::NodeInfo
                        * → code-graph/src/codegraph/parsing/parser.py::_is_test_function
                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._extract_imports
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._extract_import
                            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._find_string_literal
                                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._find_string_literal
                            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._import_path_text
                            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._r_call_func_name
                            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._r_first_string_arg
                            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._r_iter_args
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._resolve_module_to_file
                        * → code-graph/src/codegraph/parsing/parser.py::EdgeInfo
                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._extract_js_field_function
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._extract_from_tree
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._get_params
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._qualify
                        * → code-graph/src/codegraph/parsing/parser.py::EdgeInfo
                        * → code-graph/src/codegraph/parsing/parser.py::NodeInfo
                        * → code-graph/src/codegraph/parsing/parser.py::_is_test_function
                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._extract_js_var_functions
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._extract_from_tree
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._get_params
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._get_return_type
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._qualify
                        * → code-graph/src/codegraph/parsing/parser.py::EdgeInfo
                        * → code-graph/src/codegraph/parsing/parser.py::NodeInfo
                        * → code-graph/src/codegraph/parsing/parser.py::_is_test_function
                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._extract_jsx_component_call
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._qualify
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._resolve_jsx_component_target
                            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._get_jsx_component_reference
                                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._get_member_expression_root_name
                                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._get_member_expression_root_name
                                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._looks_like_component_name
                            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._resolve_call_target
                            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._resolve_imported_symbol
                        * → code-graph/src/codegraph/parsing/parser.py::EdgeInfo
                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._extract_julia_constructs
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._extract_from_tree
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._julia_call_first_identifier
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._julia_short_func_name
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._julia_string_arg
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._qualify
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._resolve_module_to_file
                        * → code-graph/src/codegraph/parsing/parser.py::EdgeInfo
                        * → code-graph/src/codegraph/parsing/parser.py::NodeInfo
                        * → code-graph/src/codegraph/parsing/parser.py::_is_test_function
                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._extract_lua_constructs
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._handle_lua_table_function
                            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._extract_from_tree
                            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._get_params
                            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._qualify
                            * → code-graph/src/codegraph/parsing/parser.py::EdgeInfo
                            * → code-graph/src/codegraph/parsing/parser.py::NodeInfo
                            * → code-graph/src/codegraph/parsing/parser.py::_is_test_function
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._handle_lua_variable_declaration
                            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._extract_from_tree
                            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._get_params
                            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._lua_get_require_target
                            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._qualify
                            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._resolve_module_to_file
                            * → code-graph/src/codegraph/parsing/parser.py::EdgeInfo
                            * → code-graph/src/codegraph/parsing/parser.py::NodeInfo
                            * → code-graph/src/codegraph/parsing/parser.py::_is_test_function
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._lua_get_require_target
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._resolve_module_to_file
                        * → code-graph/src/codegraph/parsing/parser.py::EdgeInfo
                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._extract_nix_constructs
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._extract_from_tree
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._extract_nix_flake_input_urls
                            * → code-graph/src/codegraph/parsing/parser.py::CodeParser.visit
                                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._nix_attrpath_parts
                                * → code-graph/src/codegraph/parsing/parser.py::CodeParser.first_path_arg
                                * → code-graph/src/codegraph/parsing/parser.py::CodeParser.head_call_name
                                * → code-graph/src/codegraph/parsing/parser.py::CodeParser.visit
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._extract_nix_import_targets
                            * → code-graph/src/codegraph/parsing/parser.py::CodeParser.visit
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._is_nix_flake_file
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._nix_attrpath_parts
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._qualify
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._resolve_module_to_file
                        * → code-graph/src/codegraph/parsing/parser.py::EdgeInfo
                        * → code-graph/src/codegraph/parsing/parser.py::NodeInfo
                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._extract_pascal_constructs
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._extract_from_tree
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._pascal_call_parts
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._pascal_params
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._pascal_proc_name
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._pascal_return_type
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._pascal_type_name
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._qualify
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._resolve_call_target
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._resolve_pascal_unit
                            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._pascal_dir_listing
                        * → code-graph/src/codegraph/parsing/parser.py::EdgeInfo
                        * → code-graph/src/codegraph/parsing/parser.py::NodeInfo
                        * → code-graph/src/codegraph/parsing/parser.py::_is_test_function
                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._extract_r_constructs
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._handle_r_binary_operator
                            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._extract_from_tree
                            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._get_params
                            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._handle_r_class_call
                                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._extract_r_methods
                                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._extract_from_tree
                                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._get_params
                                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._qualify
                                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._r_iter_args
                                    * → code-graph/src/codegraph/parsing/parser.py::EdgeInfo
                                    * → code-graph/src/codegraph/parsing/parser.py::NodeInfo
                                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._qualify
                                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._r_find_named_arg
                                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._r_iter_args
                                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._r_first_string_arg
                                * → code-graph/src/codegraph/parsing/parser.py::EdgeInfo
                                * → code-graph/src/codegraph/parsing/parser.py::NodeInfo
                            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._qualify
                            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._r_call_func_name
                            * → code-graph/src/codegraph/parsing/parser.py::EdgeInfo
                            * → code-graph/src/codegraph/parsing/parser.py::NodeInfo
                            * → code-graph/src/codegraph/parsing/parser.py::_is_test_function
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._handle_r_call
                            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._extract_from_tree
                            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._extract_import
                            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._get_call_name
                            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._handle_r_class_call
                            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._qualify
                            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._r_call_func_name
                            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._resolve_call_target
                            * → code-graph/src/codegraph/parsing/parser.py::EdgeInfo
                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._extract_solidity_constructs
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._qualify
                        * → code-graph/src/codegraph/parsing/parser.py::EdgeInfo
                        * → code-graph/src/codegraph/parsing/parser.py::NodeInfo
                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._extract_value_references
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._emit_reference_if_known
                            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._resolve_call_target
                            * → code-graph/src/codegraph/parsing/parser.py::EdgeInfo
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._qualify
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._ref_from_arguments
                            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._emit_reference_if_known
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._ref_from_array
                            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._emit_reference_if_known
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._ref_from_assignment
                            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._emit_reference_if_known
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._ref_from_pair
                            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._emit_reference_if_known
                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._get_parser
                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._parse_databricks_py_notebook
                    * → code-graph/src/codegraph/parsing/parser.py::CellInfo
                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._parse_notebook_cells
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._collect_file_scope
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._extract_from_tree
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._get_parser
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._qualify
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._resolve_call_targets
                        * → code-graph/src/codegraph/parsing/parser.py::EdgeInfo
                        * → code-graph/src/codegraph/parsing/parser.py::NodeInfo
                        * → code-graph/src/codegraph/parsing/parser.py::_is_test_file
                    * → code-graph/src/codegraph/parsing/parser.py::NodeInfo
                    * → code-graph/src/codegraph/parsing/parser.py::_is_test_file
                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._parse_notebook
                    * → code-graph/src/codegraph/parsing/parser.py::CellInfo
                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._parse_notebook_cells
                    * → code-graph/src/codegraph/parsing/parser.py::NodeInfo
                    * → code-graph/src/codegraph/parsing/parser.py::_is_test_file
                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._parse_rescript
                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._qualify
                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._resolve_call_targets
                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser.enclosing_module
                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser.is_top_level
                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser.offset_to_line
                    * → code-graph/src/codegraph/parsing/parser.py::EdgeInfo
                    * → code-graph/src/codegraph/parsing/parser.py::NodeInfo
                    * → code-graph/src/codegraph/parsing/parser.py::_is_test_file
                    * → code-graph/src/codegraph/parsing/parser.py::_is_test_function
                        * → code-graph/src/codegraph/parsing/parser.py::_is_test_file
                    * → code-graph/src/codegraph/parsing/parser.py::_rescript_brace_depth_array
                    * → code-graph/src/codegraph/parsing/parser.py::_scan_rescript_modules
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser.offset_to_line
                    * → code-graph/src/codegraph/parsing/parser.py::_strip_rescript_noise
                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._parse_sql
                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._get_parser
                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._walk_sql_tree
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._extract_sql_ddl
                            * → code-graph/src/codegraph/parsing/parser.py::EdgeInfo
                            * → code-graph/src/codegraph/parsing/parser.py::NodeInfo
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._walk_sql_tree
                    * → code-graph/src/codegraph/parsing/parser.py::EdgeInfo
                    * → code-graph/src/codegraph/parsing/parser.py::NodeInfo
                    * → code-graph/src/codegraph/parsing/parser.py::_is_test_file
                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._parse_svelte
                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._collect_file_scope
                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._extract_from_tree
                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._get_parser
                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._qualify
                    * → code-graph/src/codegraph/parsing/parser.py::EdgeInfo
                    * → code-graph/src/codegraph/parsing/parser.py::NodeInfo
                    * → code-graph/src/codegraph/parsing/parser.py::_is_test_file
                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._parse_vue
                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._collect_file_scope
                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._extract_from_tree
                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._get_parser
                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._qualify
                    * → code-graph/src/codegraph/parsing/parser.py::EdgeInfo
                    * → code-graph/src/codegraph/parsing/parser.py::NodeInfo
                    * → code-graph/src/codegraph/parsing/parser.py::_is_test_file
                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._qualify
                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._resolve_call_targets
                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._qualify
                    * → code-graph/src/codegraph/parsing/parser.py::EdgeInfo
                * → code-graph/src/codegraph/parsing/parser.py::CodeParser.detect_language
                * → code-graph/src/codegraph/parsing/parser.py::EdgeInfo
                * → code-graph/src/codegraph/parsing/parser.py::NodeInfo
                * → code-graph/src/codegraph/parsing/parser.py::_is_test_file
            * → code-graph/src/codegraph/pipeline/incremental.py::_PendingWrites
            * → code-graph/src/codegraph/pipeline/incremental.py::_PendingWrites.add
                * → code-graph/src/codegraph/pipeline/incremental.py::_PendingWrites.flush
            * → code-graph/src/codegraph/pipeline/incremental.py::_PendingWrites.flush
            * → code-graph/src/codegraph/pipeline/incremental.py::_make_executor
                * → code-graph/src/codegraph/pipeline/incremental.py::_select_executor_kind
            * → code-graph/src/codegraph/pipeline/incremental.py::_run_receiver_resolver
                * → code-graph/src/codegraph/resolvers/receivers.py::resolve_typed_receivers
            * → code-graph/src/codegraph/pipeline/incremental.py::_run_rescript_resolver
                * → code-graph/src/codegraph/resolvers/rescript.py::resolve_rescript_cross_module
                    * → code-graph/src/codegraph/graph/graph_store.py::GraphStore._invalidate_cache
                    * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.get_all_files
                    * → code-graph/src/codegraph/resolvers/rescript.py::_resolve_call_target
                        * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.get_node
                            * → code-graph/src/codegraph/graph/graph_store.py::GraphStore._row_to_node
                        * → code-graph/src/codegraph/resolvers/rescript.py::_pick_existing_qualified
                            * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.get_node
            * → code-graph/src/codegraph/pipeline/incremental.py::_run_spring_resolver
                * → code-graph/src/codegraph/resolvers/spring.py::resolve_spring_di_calls
            * → code-graph/src/codegraph/pipeline/incremental.py::_run_temporal_resolver
                * → code-graph/src/codegraph/resolvers/temporal.py::resolve_temporal_calls
            * → code-graph/src/codegraph/pipeline/incremental.py::_store_batch_size
            * → code-graph/src/codegraph/pipeline/incremental.py::_store_vcs_metadata
                * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.set_metadata
                * → code-graph/src/codegraph/pipeline/incremental.py::_git_branch_info
                * → code-graph/src/codegraph/pipeline/incremental.py::_svn_revision_info
                * → code-graph/src/codegraph/pipeline/incremental.py::detect_vcs
            * → code-graph/src/codegraph/pipeline/incremental.py::collect_all_files
                * → code-graph/src/codegraph/parsing/parser.py::CodeParser
                * → code-graph/src/codegraph/parsing/parser.py::CodeParser.detect_language
                * → code-graph/src/codegraph/pipeline/incremental.py::_get_candidates
                    * → code-graph/src/codegraph/pipeline/incremental.py::_should_ignore
                * → code-graph/src/codegraph/pipeline/incremental.py::_is_binary
                * → code-graph/src/codegraph/pipeline/incremental.py::_load_ignore_patterns
                * → code-graph/src/codegraph/pipeline/incremental.py::_should_ignore
                * → code-graph/src/codegraph/pipeline/incremental.py::get_all_tracked_files
                    * → code-graph/src/codegraph/pipeline/incremental.py::_get_svn_all_tracked_files
                    * → code-graph/src/codegraph/pipeline/incremental.py::detect_vcs
        * → code-graph/src/codegraph/pipeline/incremental.py::incremental_update
            * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.commit
            * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.get_nodes_by_file
                * → code-graph/src/codegraph/graph/graph_store.py::GraphStore._row_to_node
                    * → code-graph/src/codegraph/graph/graph_store.py::GraphNode
            * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.remove_file_data
            * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.set_metadata
            * → code-graph/src/codegraph/parsing/parser.py::CodeParser
            * → code-graph/src/codegraph/parsing/parser.py::CodeParser.detect_language
                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._detect_language_from_shebang
            * → code-graph/src/codegraph/parsing/parser.py::CodeParser.parse_bytes
            * → code-graph/src/codegraph/pipeline/incremental.py::_PendingWrites
            * → code-graph/src/codegraph/pipeline/incremental.py::_PendingWrites.add
            * → code-graph/src/codegraph/pipeline/incremental.py::_PendingWrites.flush
            * → code-graph/src/codegraph/pipeline/incremental.py::_load_ignore_patterns
            * → code-graph/src/codegraph/pipeline/incremental.py::_make_executor
            * → code-graph/src/codegraph/pipeline/incremental.py::_run_receiver_resolver
            * → code-graph/src/codegraph/pipeline/incremental.py::_run_rescript_resolver
            * → code-graph/src/codegraph/pipeline/incremental.py::_run_spring_resolver
            * → code-graph/src/codegraph/pipeline/incremental.py::_run_temporal_resolver
            * → code-graph/src/codegraph/pipeline/incremental.py::_should_ignore
                * → code-graph/src/codegraph/pipeline/incremental.py::_is_match
            * → code-graph/src/codegraph/pipeline/incremental.py::_store_batch_size
            * → code-graph/src/codegraph/pipeline/incremental.py::_store_vcs_metadata
            * → code-graph/src/codegraph/pipeline/incremental.py::find_dependents
                * → code-graph/src/codegraph/pipeline/incremental.py::DependentList
                * → code-graph/src/codegraph/pipeline/incremental.py::_single_hop_dependents
                    * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.get_edges_by_target
                        * → code-graph/src/codegraph/graph/graph_store.py::GraphStore._row_to_edge
                            * → code-graph/src/codegraph/graph/graph_store.py::GraphEdge
                    * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.get_nodes_by_file
                    * → code-graph/src/codegraph/pipeline/incremental.py::_PendingWrites.add
            * → code-graph/src/codegraph/pipeline/incremental.py::get_changed_files
                * → code-graph/src/codegraph/pipeline/incremental.py::_get_svn_changed_files
                * → code-graph/src/codegraph/pipeline/incremental.py::detect_vcs
        * → code-graph/src/codegraph/pipeline/postprocessing.py::run_post_processing
            * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.set_metadata
            * → code-graph/src/codegraph/pipeline/postprocessing.py::_compute_signatures
                * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.commit
                * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.get_nodes_without_signature
                * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.update_node_signature
            * → code-graph/src/codegraph/pipeline/postprocessing.py::_compute_summaries
            * → code-graph/src/codegraph/pipeline/postprocessing.py::_detect_communities
                * → code-graph/src/codegraph/query/communities.py::detect_communities
                    * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.get_all_edges
                        * → code-graph/src/codegraph/graph/graph_store.py::GraphStore._row_to_edge
                    * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.get_all_nodes
                        * → code-graph/src/codegraph/graph/graph_store.py::GraphStore._row_to_node
                    * → code-graph/src/codegraph/query/communities.py::_build_adjacency
                    * → code-graph/src/codegraph/query/communities.py::_detect_file_based
                        * → code-graph/src/codegraph/query/communities.py::_compute_cohesion_batch
                        * → code-graph/src/codegraph/query/communities.py::_generate_community_name
                            * → code-graph/src/codegraph/query/communities.py::_extract_file_prefix
                                * → code-graph/src/codegraph/query/communities.py::_to_slug
                            * → code-graph/src/codegraph/query/communities.py::_extract_keywords
                                * → code-graph/src/codegraph/query/communities.py::_split_name
                            * → code-graph/src/codegraph/query/communities.py::_to_slug
                        * → code-graph/src/codegraph/query/communities.py::_group_at_depth
                    * → code-graph/src/codegraph/query/communities.py::_detect_leiden
                        * → code-graph/src/codegraph/query/communities.py::_compute_cohesion_batch
                        * → code-graph/src/codegraph/query/communities.py::_detect_file_based
                        * → code-graph/src/codegraph/query/communities.py::_generate_community_name
                    * → code-graph/src/codegraph/query/communities.py::_split_oversized
                * → code-graph/src/codegraph/query/communities.py::incremental_detect_communities
                    * → code-graph/src/codegraph/query/communities.py::detect_communities
                    * → code-graph/src/codegraph/query/communities.py::store_communities
                * → code-graph/src/codegraph/query/communities.py::store_communities
            * → code-graph/src/codegraph/pipeline/postprocessing.py::_rebuild_fts_index
                * → code-graph/src/codegraph/query/search.py::rebuild_fts_index
            * → code-graph/src/codegraph/pipeline/postprocessing.py::_trace_flows
                * → code-graph/src/codegraph/query/flows.py::incremental_trace_flows
                    * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.load_flow_adjacency
                        * → code-graph/src/codegraph/graph/graph_store.py::FlowAdjacency
                        * → code-graph/src/codegraph/graph/graph_store.py::GraphStore._row_to_node
                    * → code-graph/src/codegraph/query/flows.py::_trace_single_flow
                        * → code-graph/src/codegraph/graph/graph_store.py::_sanitize_name
                        * → code-graph/src/codegraph/query/flows.py::compute_criticality
                    * → code-graph/src/codegraph/query/flows.py::detect_entry_points
                        * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.get_all_call_targets
                        * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.get_nodes_by_kind
                            * → code-graph/src/codegraph/graph/graph_store.py::GraphStore._row_to_node
                        * → code-graph/src/codegraph/query/flows.py::_has_framework_decorator
                        * → code-graph/src/codegraph/query/flows.py::_is_test_file
                        * → code-graph/src/codegraph/query/flows.py::_matches_entry_name
                * → code-graph/src/codegraph/query/flows.py::store_flows
                * → code-graph/src/codegraph/query/flows.py::trace_flows
                    * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.load_flow_adjacency
                    * → code-graph/src/codegraph/query/flows.py::_trace_single_flow
                    * → code-graph/src/codegraph/query/flows.py::detect_entry_points
<!-- CTX-SYNC:FLOW-END -->

<!-- CTX-SYNC:FLOW-START -->
* flow=code-graph/src/codegraph/cli/lookup.py::query
    * entry_point=code-graph/src/codegraph/cli/lookup.py::query
    * criticality=0.74
    * nodes=25/28
* code-graph/src/codegraph/cli/lookup.py::query
    * → code-graph/src/codegraph/cli/common.py::_emit
        * → code-graph/src/codegraph/cli/common.py::_color_enabled
        * → code-graph/src/codegraph/cli/common.py::_json_text
    * → code-graph/src/codegraph/cli/common.py::_root
        * → code-graph/src/codegraph/pipeline/incremental.py::find_project_root
            * → code-graph/src/codegraph/pipeline/incremental.py::find_repo_root
                * → code-graph/src/codegraph/pipeline/incremental.py::find_svn_root
    * → code-graph/src/codegraph/query/queries.py::query_graph
        * → code-graph/src/codegraph/graph/graph_store.py::edge_to_dict
            * → code-graph/src/codegraph/graph/graph_store.py::_sanitize_name
        * → code-graph/src/codegraph/graph/graph_store.py::node_to_dict
            * → code-graph/src/codegraph/graph/graph_store.py::_sanitize_name
        * → code-graph/src/codegraph/lib/repo.py::open_store
            * → code-graph/src/codegraph/graph/graph_store.py::GraphStore
            * → code-graph/src/codegraph/lib/repo.py::resolve_root
                * → code-graph/src/codegraph/lib/repo.py::validate_repo_root
                * → code-graph/src/codegraph/pipeline/incremental.py::find_project_root
            * → code-graph/src/codegraph/pipeline/incremental.py::get_db_path
                * → code-graph/src/codegraph/pipeline/incremental.py::get_data_dir
                    * → code-graph/src/codegraph/pipeline/incremental.py::_write_data_dir_gitignore
        * → code-graph/src/codegraph/lib/repo.py::resolve_graph_file_paths
            * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.get_files_matching
            * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.get_nodes_by_file
                * → code-graph/src/codegraph/graph/graph_store.py::GraphStore._row_to_node
                    * → code-graph/src/codegraph/graph/graph_store.py::GraphNode
            * → code-graph/src/codegraph/lib/repo.py::add
                * → code-graph/src/codegraph/lib/repo.py::add
<!-- CTX-SYNC:FLOW-END -->

<!-- CTX-SYNC:FLOW-START -->
* flow=code-graph/src/codegraph/cli/lookup.py::search
    * entry_point=code-graph/src/codegraph/cli/lookup.py::search
    * criticality=0.7714
    * nodes=28/30
* code-graph/src/codegraph/cli/lookup.py::search
    * → code-graph/src/codegraph/cli/common.py::_emit
        * → code-graph/src/codegraph/cli/common.py::_color_enabled
        * → code-graph/src/codegraph/cli/common.py::_json_text
    * → code-graph/src/codegraph/cli/common.py::_root
        * → code-graph/src/codegraph/pipeline/incremental.py::find_project_root
            * → code-graph/src/codegraph/pipeline/incremental.py::find_repo_root
                * → code-graph/src/codegraph/pipeline/incremental.py::find_svn_root
    * → code-graph/src/codegraph/cli/common.py::_with_hints
        * → code-graph/src/codegraph/cli/hints.py::generate_hints
            * → code-graph/src/codegraph/cli/hints.py::_build_next_steps
            * → code-graph/src/codegraph/cli/hints.py::_build_related
            * → code-graph/src/codegraph/cli/hints.py::_extract_warnings
    * → code-graph/src/codegraph/query/search.py::semantic_search_nodes
        * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.close
            * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.close
        * → code-graph/src/codegraph/lib/repo.py::open_store
            * → code-graph/src/codegraph/graph/graph_store.py::GraphStore
            * → code-graph/src/codegraph/lib/repo.py::resolve_root
                * → code-graph/src/codegraph/lib/repo.py::validate_repo_root
                * → code-graph/src/codegraph/pipeline/incremental.py::find_project_root
            * → code-graph/src/codegraph/pipeline/incremental.py::get_db_path
                * → code-graph/src/codegraph/pipeline/incremental.py::get_data_dir
                    * → code-graph/src/codegraph/pipeline/incremental.py::_write_data_dir_gitignore
        * → code-graph/src/codegraph/query/search.py::search_nodes
            * → code-graph/src/codegraph/graph/graph_store.py::_sanitize_name
            * → code-graph/src/codegraph/query/search.py::_fts_search
            * → code-graph/src/codegraph/query/search.py::_keyword_search
            * → code-graph/src/codegraph/query/search.py::detect_query_kind_boost
                * → code-graph/src/codegraph/query/search.py::extract_query_identifiers
<!-- CTX-SYNC:FLOW-END -->

<!-- CTX-SYNC:FLOW-START -->
* flow=code-graph/src/codegraph/cli/refactor.py::refactor_apply
    * entry_point=code-graph/src/codegraph/cli/refactor.py::refactor_apply
    * criticality=0.615
    * nodes=10/10
* code-graph/src/codegraph/cli/refactor.py::refactor_apply
    * → code-graph/src/codegraph/cli/common.py::_emit
        * → code-graph/src/codegraph/cli/common.py::_color_enabled
        * → code-graph/src/codegraph/cli/common.py::_json_text
    * → code-graph/src/codegraph/cli/common.py::_root
        * → code-graph/src/codegraph/pipeline/incremental.py::find_project_root
            * → code-graph/src/codegraph/pipeline/incremental.py::find_repo_root
                * → code-graph/src/codegraph/pipeline/incremental.py::find_svn_root
    * → code-graph/src/codegraph/refactor.py::apply_refactor
        * → code-graph/src/codegraph/refactor.py::_cleanup_expired
<!-- CTX-SYNC:FLOW-END -->

<!-- CTX-SYNC:FLOW-START -->
* flow=code-graph/src/codegraph/cli/refactor.py::refactor_rename
    * entry_point=code-graph/src/codegraph/cli/refactor.py::refactor_rename
    * criticality=0.6986
    * nodes=29/31
* code-graph/src/codegraph/cli/refactor.py::refactor_rename
    * → code-graph/src/codegraph/cli/common.py::_emit
        * → code-graph/src/codegraph/cli/common.py::_color_enabled
        * → code-graph/src/codegraph/cli/common.py::_json_text
    * → code-graph/src/codegraph/cli/common.py::_root
        * → code-graph/src/codegraph/pipeline/incremental.py::find_project_root
            * → code-graph/src/codegraph/pipeline/incremental.py::find_repo_root
                * → code-graph/src/codegraph/pipeline/incremental.py::find_svn_root
    * → code-graph/src/codegraph/cli/common.py::_store
        * → code-graph/src/codegraph/graph/graph_store.py::GraphStore
        * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.close
            * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.close
        * → code-graph/src/codegraph/pipeline/incremental.py::get_db_path
            * → code-graph/src/codegraph/pipeline/incremental.py::get_data_dir
                * → code-graph/src/codegraph/pipeline/incremental.py::_write_data_dir_gitignore
    * → code-graph/src/codegraph/cli/common.py::_with_hints
        * → code-graph/src/codegraph/cli/hints.py::generate_hints
            * → code-graph/src/codegraph/cli/hints.py::_build_next_steps
            * → code-graph/src/codegraph/cli/hints.py::_build_related
            * → code-graph/src/codegraph/cli/hints.py::_extract_warnings
    * → code-graph/src/codegraph/refactor.py::rename_preview
        * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.get_edges_by_target
            * → code-graph/src/codegraph/graph/graph_store.py::GraphStore._row_to_edge
                * → code-graph/src/codegraph/graph/graph_store.py::GraphEdge
        * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.search_edges_by_target_name
            * → code-graph/src/codegraph/graph/graph_store.py::GraphStore._row_to_edge
        * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.search_nodes
            * → code-graph/src/codegraph/graph/graph_store.py::GraphStore._row_to_node
                * → code-graph/src/codegraph/graph/graph_store.py::GraphNode
        * → code-graph/src/codegraph/graph/graph_store.py::_sanitize_name
        * → code-graph/src/codegraph/refactor.py::_cleanup_expired
<!-- CTX-SYNC:FLOW-END -->

<!-- CTX-SYNC:FLOW-START -->
* flow=code-graph/src/codegraph/cli/refactor.py::refactor_suggest
    * entry_point=code-graph/src/codegraph/cli/refactor.py::refactor_suggest
    * criticality=0.7088
    * nodes=40/50
* code-graph/src/codegraph/cli/refactor.py::refactor_suggest
    * → code-graph/src/codegraph/cli/common.py::_emit
        * → code-graph/src/codegraph/cli/common.py::_color_enabled
        * → code-graph/src/codegraph/cli/common.py::_json_text
    * → code-graph/src/codegraph/cli/common.py::_root
        * → code-graph/src/codegraph/pipeline/incremental.py::find_project_root
            * → code-graph/src/codegraph/pipeline/incremental.py::find_repo_root
                * → code-graph/src/codegraph/pipeline/incremental.py::find_svn_root
    * → code-graph/src/codegraph/cli/common.py::_store
        * → code-graph/src/codegraph/graph/graph_store.py::GraphStore
        * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.close
            * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.close
        * → code-graph/src/codegraph/pipeline/incremental.py::get_db_path
            * → code-graph/src/codegraph/pipeline/incremental.py::get_data_dir
                * → code-graph/src/codegraph/pipeline/incremental.py::_write_data_dir_gitignore
    * → code-graph/src/codegraph/cli/common.py::_with_hints
        * → code-graph/src/codegraph/cli/hints.py::generate_hints
            * → code-graph/src/codegraph/cli/hints.py::_build_next_steps
            * → code-graph/src/codegraph/cli/hints.py::_build_related
            * → code-graph/src/codegraph/cli/hints.py::_extract_warnings
    * → code-graph/src/codegraph/refactor.py::suggest_refactorings
        * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.get_communities_list
        * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.get_community_member_qns
        * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.get_edges_by_target
            * → code-graph/src/codegraph/graph/graph_store.py::GraphStore._row_to_edge
                * → code-graph/src/codegraph/graph/graph_store.py::GraphEdge
        * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.get_nodes_by_kind
            * → code-graph/src/codegraph/graph/graph_store.py::GraphStore._row_to_node
                * → code-graph/src/codegraph/graph/graph_store.py::GraphNode
        * → code-graph/src/codegraph/graph/graph_store.py::_sanitize_name
        * → code-graph/src/codegraph/refactor.py::find_dead_code
            * → code-graph/src/codegraph/graph/graph_store.py::GraphStore._row_to_edge
            * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.get_edges_by_source
                * → code-graph/src/codegraph/graph/graph_store.py::GraphStore._row_to_edge
            * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.get_edges_by_target
            * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.get_node
                * → code-graph/src/codegraph/graph/graph_store.py::GraphStore._row_to_node
            * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.get_nodes_by_kind
            * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.search_edges_by_target_name
                * → code-graph/src/codegraph/graph/graph_store.py::GraphStore._row_to_edge
            * → code-graph/src/codegraph/graph/graph_store.py::_sanitize_name
            * → code-graph/src/codegraph/query/flows.py::_has_framework_decorator
            * → code-graph/src/codegraph/refactor.py::_collect_type_referenced_names
                * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.get_nodes_by_kind
            * → code-graph/src/codegraph/refactor.py::_is_entry_point
                * → code-graph/src/codegraph/query/flows.py::_has_framework_decorator
                * → code-graph/src/codegraph/query/flows.py::_matches_entry_name
            * → code-graph/src/codegraph/refactor.py::_is_plausible_caller
                * → code-graph/src/codegraph/refactor.py::_path_segments
            * → code-graph/src/codegraph/refactor.py::_is_test_file
<!-- CTX-SYNC:FLOW-END -->

<!-- CTX-SYNC:FLOW-START -->
* flow=code-graph/src/codegraph/graph/graph_store.py::GraphStore.__exit__
    * entry_point=code-graph/src/codegraph/graph/graph_store.py::GraphStore.__exit__
    * criticality=0.16
    * nodes=2/3
* code-graph/src/codegraph/graph/graph_store.py::GraphStore.__exit__
    * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.close
        * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.close
<!-- CTX-SYNC:FLOW-END -->

<!-- CTX-SYNC:FLOW-START -->
* flow=code-graph/src/codegraph/graph/graph_store.py::GraphStore.__init__
    * entry_point=code-graph/src/codegraph/graph/graph_store.py::GraphStore.__init__
    * criticality=0.445
    * nodes=6/7
* code-graph/src/codegraph/graph/graph_store.py::GraphStore.__init__
    * → code-graph/src/codegraph/graph/graph_store.py::GraphStore._init_schema
        * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.commit
            * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.commit
    * → code-graph/src/codegraph/graph/migrations.py::run_migrations
        * → code-graph/src/codegraph/graph/migrations.py::_set_schema_version
        * → code-graph/src/codegraph/graph/migrations.py::get_schema_version
<!-- CTX-SYNC:FLOW-END -->

<!-- CTX-SYNC:FLOW-START -->
* flow=code-graph/src/codegraph/graph/graph_store.py::GraphStore.get_impact_radius
    * entry_point=code-graph/src/codegraph/graph/graph_store.py::GraphStore.get_impact_radius
    * criticality=0.4127
    * nodes=11/15
* code-graph/src/codegraph/graph/graph_store.py::GraphStore.get_impact_radius
    * → code-graph/src/codegraph/graph/graph_store.py::GraphStore._get_impact_radius_networkx
        * → code-graph/src/codegraph/graph/graph_store.py::GraphStore._batch_get_nodes
            * → code-graph/src/codegraph/graph/graph_store.py::GraphStore._row_to_node
                * → code-graph/src/codegraph/graph/graph_store.py::GraphNode
        * → code-graph/src/codegraph/graph/graph_store.py::GraphStore._build_networkx_graph
        * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.get_edges_among
            * → code-graph/src/codegraph/graph/graph_store.py::GraphStore._row_to_edge
                * → code-graph/src/codegraph/graph/graph_store.py::GraphEdge
        * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.get_nodes_by_file
            * → code-graph/src/codegraph/graph/graph_store.py::GraphStore._row_to_node
    * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.get_impact_radius_sql
        * → code-graph/src/codegraph/graph/graph_store.py::GraphStore._batch_get_nodes
        * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.get_edges_among
        * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.get_nodes_by_file
<!-- CTX-SYNC:FLOW-END -->

<!-- CTX-SYNC:FLOW-START -->
* flow=code-graph/src/codegraph/graph/graph_store.py::GraphStore.get_stats
    * entry_point=code-graph/src/codegraph/graph/graph_store.py::GraphStore.get_stats
    * criticality=0.36
    * nodes=3/3
* code-graph/src/codegraph/graph/graph_store.py::GraphStore.get_stats
    * → code-graph/src/codegraph/graph/graph_store.py::GraphStats
    * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.get_metadata
<!-- CTX-SYNC:FLOW-END -->

<!-- CTX-SYNC:FLOW-START -->
* flow=code-graph/src/codegraph/graph/graph_store.py::GraphStore.get_subgraph
    * entry_point=code-graph/src/codegraph/graph/graph_store.py::GraphStore.get_subgraph
    * criticality=0.38
    * nodes=7/7
* code-graph/src/codegraph/graph/graph_store.py::GraphStore.get_subgraph
    * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.get_edges_by_source
        * → code-graph/src/codegraph/graph/graph_store.py::GraphStore._row_to_edge
            * → code-graph/src/codegraph/graph/graph_store.py::GraphEdge
    * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.get_node
        * → code-graph/src/codegraph/graph/graph_store.py::GraphStore._row_to_node
            * → code-graph/src/codegraph/graph/graph_store.py::GraphNode
<!-- CTX-SYNC:FLOW-END -->

<!-- CTX-SYNC:FLOW-START -->
* flow=code-graph/src/codegraph/graph/graph_store.py::GraphStore.resolve_bare_call_targets
    * entry_point=code-graph/src/codegraph/graph/graph_store.py::GraphStore.resolve_bare_call_targets
    * criticality=0.36
    * nodes=2/3
* code-graph/src/codegraph/graph/graph_store.py::GraphStore.resolve_bare_call_targets
    * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.commit
        * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.commit
<!-- CTX-SYNC:FLOW-END -->

<!-- CTX-SYNC:FLOW-START -->
* flow=code-graph/src/codegraph/graph/graph_store.py::GraphStore.store_file_batch
    * entry_point=code-graph/src/codegraph/graph/graph_store.py::GraphStore.store_file_batch
    * criticality=0.3978
    * nodes=9/13
* code-graph/src/codegraph/graph/graph_store.py::GraphStore.store_file_batch
    * → code-graph/src/codegraph/graph/graph_store.py::GraphStore._begin_immediate
        * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.rollback
    * → code-graph/src/codegraph/graph/graph_store.py::GraphStore._invalidate_cache
    * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.commit
        * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.commit
    * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.remove_file_data
        * → code-graph/src/codegraph/graph/graph_store.py::GraphStore._invalidate_cache
    * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.rollback
        * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.rollback
    * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.upsert_edge
    * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.upsert_node
        * → code-graph/src/codegraph/graph/graph_store.py::GraphStore._make_qualified
<!-- CTX-SYNC:FLOW-END -->

<!-- CTX-SYNC:FLOW-START -->
* flow=code-graph/src/codegraph/graph/graph_store.py::GraphStore.store_file_nodes_edges
    * entry_point=code-graph/src/codegraph/graph/graph_store.py::GraphStore.store_file_nodes_edges
    * criticality=0.3978
    * nodes=9/13
* code-graph/src/codegraph/graph/graph_store.py::GraphStore.store_file_nodes_edges
    * → code-graph/src/codegraph/graph/graph_store.py::GraphStore._begin_immediate
        * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.rollback
    * → code-graph/src/codegraph/graph/graph_store.py::GraphStore._invalidate_cache
    * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.commit
        * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.commit
    * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.remove_file_data
        * → code-graph/src/codegraph/graph/graph_store.py::GraphStore._invalidate_cache
    * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.rollback
        * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.rollback
    * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.upsert_edge
    * → code-graph/src/codegraph/graph/graph_store.py::GraphStore.upsert_node
        * → code-graph/src/codegraph/graph/graph_store.py::GraphStore._make_qualified
<!-- CTX-SYNC:FLOW-END -->

<!-- CTX-SYNC:FLOW-START -->
* flow=code-graph/src/codegraph/graph/migrations.py::_migrate_v1
    * entry_point=code-graph/src/codegraph/graph/migrations.py::_migrate_v1
    * criticality=0.36
    * nodes=3/3
* code-graph/src/codegraph/graph/migrations.py::_migrate_v1
    * → code-graph/src/codegraph/graph/migrations.py::_has_column
    * → code-graph/src/codegraph/graph/migrations.py::_table_exists
<!-- CTX-SYNC:FLOW-END -->

<!-- CTX-SYNC:FLOW-START -->
* flow=code-graph/src/codegraph/parsing/parser.py::CodeParser.__init__
    * entry_point=code-graph/src/codegraph/parsing/parser.py::CodeParser.__init__
    * criticality=0.5713
    * nodes=8/8
* code-graph/src/codegraph/parsing/parser.py::CodeParser.__init__
    * → code-graph/src/codegraph/parsing/custom.py::load_custom_languages
        * → code-graph/src/codegraph/parsing/custom.py::_CacheEntry
        * → code-graph/src/codegraph/parsing/custom.py::_load_uncached
            * → code-graph/src/codegraph/parsing/custom.py::_validate_entry
                * → code-graph/src/codegraph/parsing/custom.py::CustomLanguage
    * → code-graph/src/codegraph/parsing/parser.py::_builtin_language_names
    * → code-graph/src/codegraph/parsing/tsconfig.py::TsconfigResolver
<!-- CTX-SYNC:FLOW-END -->

<!-- CTX-SYNC:FLOW-START -->
* flow=code-graph/src/codegraph/parsing/tsconfig.py::TsconfigResolver.resolve_alias
    * entry_point=code-graph/src/codegraph/parsing/tsconfig.py::TsconfigResolver.resolve_alias
    * criticality=0.39
    * nodes=8/9
* code-graph/src/codegraph/parsing/tsconfig.py::TsconfigResolver.resolve_alias
    * → code-graph/src/codegraph/parsing/tsconfig.py::TsconfigResolver._load_tsconfig_for_file
        * → code-graph/src/codegraph/parsing/tsconfig.py::TsconfigResolver._parse_tsconfig
            * → code-graph/src/codegraph/parsing/tsconfig.py::TsconfigResolver._resolve_extends
                * → code-graph/src/codegraph/parsing/tsconfig.py::TsconfigResolver._resolve_extends
                * → code-graph/src/codegraph/parsing/tsconfig.py::TsconfigResolver._strip_jsonc_comments
    * → code-graph/src/codegraph/parsing/tsconfig.py::TsconfigResolver._match_and_probe
        * → code-graph/src/codegraph/parsing/tsconfig.py::_match_pattern
        * → code-graph/src/codegraph/parsing/tsconfig.py::_probe_path
<!-- CTX-SYNC:FLOW-END -->

<!-- CTX-SYNC:FLOW-START -->
* flow=code-graph/src/codegraph/pipeline/incremental.py::_parse_single_file
    * entry_point=code-graph/src/codegraph/pipeline/incremental.py::_parse_single_file
    * criticality=0.4931
    * nodes=124/309
* code-graph/src/codegraph/pipeline/incremental.py::_parse_single_file
    * → code-graph/src/codegraph/parsing/parser.py::CodeParser
    * → code-graph/src/codegraph/parsing/parser.py::CodeParser.parse_bytes
        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._collect_file_scope
            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._collect_import_names
                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._collect_js_import_names
            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._collect_js_exported_local_names
            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._get_name
                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._get_name
                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._leaf_name
                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._leaf_name
        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._collect_type_bindings
            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._simple_type_name
            * → code-graph/src/codegraph/parsing/parser.py::CodeParser.bind
        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._extract_from_tree
            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._extract_bash_source_command
                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._resolve_module_to_file
                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._do_resolve_module
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._find_dart_pubspec_root
                * → code-graph/src/codegraph/parsing/parser.py::EdgeInfo
            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._extract_calls
                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._extract_from_tree
                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._get_base_call_name
                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._get_call_name
                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._get_custom_call_name
                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._normalize_php_name
                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._get_call_receiver
                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._get_java_method_and_receiver
                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._get_test_description
                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._qualify
                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._resolve_call_target
                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._qualify
                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._resolve_imported_symbol
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._qualify
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._resolve_exported_symbol
                            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._collect_file_scope
                            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._get_parser
                            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._qualify
                            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._resolve_exported_symbol
                            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._resolve_imported_symbol
                            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._resolve_module_to_file
                            * → code-graph/src/codegraph/parsing/parser.py::CodeParser.detect_language
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._resolve_module_to_file
                * → code-graph/src/codegraph/parsing/parser.py::EdgeInfo
                * → code-graph/src/codegraph/parsing/parser.py::NodeInfo
                * → code-graph/src/codegraph/parsing/parser.py::_is_test_file
            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._extract_classes
                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._emit_kafka_edges_from_class
                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._qualify
                    * → code-graph/src/codegraph/parsing/parser.py::EdgeInfo
                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._emit_spring_injections
                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._emit_spring_constructor_injection
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._get_java_annotations
                        * → code-graph/src/codegraph/parsing/parser.py::EdgeInfo
                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._emit_spring_field_injection
                        * → code-graph/src/codegraph/parsing/parser.py::EdgeInfo
                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._qualify
                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._emit_temporal_stub_fields
                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._qualify
                    * → code-graph/src/codegraph/parsing/parser.py::EdgeInfo
                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._extract_from_tree
                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._get_bases
                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._get_class_decorators
                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._get_java_annotations
                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._get_name
                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._qualify
                * → code-graph/src/codegraph/parsing/parser.py::EdgeInfo
                * → code-graph/src/codegraph/parsing/parser.py::NodeInfo
            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._extract_dart_calls_from_children
                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._qualify
                * → code-graph/src/codegraph/parsing/parser.py::EdgeInfo
            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._extract_elixir_constructs
                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._elixir_call_identifier
                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._elixir_function_name_and_params
                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._elixir_module_name
                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._extract_from_tree
                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._qualify
                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._resolve_call_target
                * → code-graph/src/codegraph/parsing/parser.py::EdgeInfo
                * → code-graph/src/codegraph/parsing/parser.py::NodeInfo
                * → code-graph/src/codegraph/parsing/parser.py::_is_test_function
            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._extract_from_tree
            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._extract_functions
                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._emit_kafka_edges_from_method
                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._get_kafka_annotation_topics
                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._qualify
                    * → code-graph/src/codegraph/parsing/parser.py::EdgeInfo
                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._extract_from_tree
                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._get_go_receiver_type
                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._get_name
                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._get_params
                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._get_return_type
                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._qualify
                * → code-graph/src/codegraph/parsing/parser.py::EdgeInfo
                * → code-graph/src/codegraph/parsing/parser.py::NodeInfo
                * → code-graph/src/codegraph/parsing/parser.py::_is_test_function
            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._extract_imports
                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._extract_import
                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._find_string_literal
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._find_string_literal
                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._import_path_text
                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._r_call_func_name
                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._r_first_string_arg
                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._r_iter_args
                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._resolve_module_to_file
                * → code-graph/src/codegraph/parsing/parser.py::EdgeInfo
            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._extract_js_field_function
                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._extract_from_tree
                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._get_params
                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._qualify
                * → code-graph/src/codegraph/parsing/parser.py::EdgeInfo
                * → code-graph/src/codegraph/parsing/parser.py::NodeInfo
                * → code-graph/src/codegraph/parsing/parser.py::_is_test_function
            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._extract_js_var_functions
                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._extract_from_tree
                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._get_params
                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._get_return_type
                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._qualify
                * → code-graph/src/codegraph/parsing/parser.py::EdgeInfo
                * → code-graph/src/codegraph/parsing/parser.py::NodeInfo
                * → code-graph/src/codegraph/parsing/parser.py::_is_test_function
            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._extract_jsx_component_call
                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._qualify
                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._resolve_jsx_component_target
                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._get_jsx_component_reference
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._get_member_expression_root_name
                            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._get_member_expression_root_name
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._looks_like_component_name
                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._resolve_call_target
                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._resolve_imported_symbol
                * → code-graph/src/codegraph/parsing/parser.py::EdgeInfo
            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._extract_julia_constructs
                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._extract_from_tree
                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._julia_call_first_identifier
                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._julia_short_func_name
                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._julia_string_arg
                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._qualify
                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._resolve_module_to_file
                * → code-graph/src/codegraph/parsing/parser.py::EdgeInfo
                * → code-graph/src/codegraph/parsing/parser.py::NodeInfo
                * → code-graph/src/codegraph/parsing/parser.py::_is_test_function
            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._extract_lua_constructs
                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._handle_lua_table_function
                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._extract_from_tree
                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._get_params
                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._qualify
                    * → code-graph/src/codegraph/parsing/parser.py::EdgeInfo
                    * → code-graph/src/codegraph/parsing/parser.py::NodeInfo
                    * → code-graph/src/codegraph/parsing/parser.py::_is_test_function
                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._handle_lua_variable_declaration
                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._extract_from_tree
                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._get_params
                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._lua_get_require_target
                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._qualify
                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._resolve_module_to_file
                    * → code-graph/src/codegraph/parsing/parser.py::EdgeInfo
                    * → code-graph/src/codegraph/parsing/parser.py::NodeInfo
                    * → code-graph/src/codegraph/parsing/parser.py::_is_test_function
                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._lua_get_require_target
                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._resolve_module_to_file
                * → code-graph/src/codegraph/parsing/parser.py::EdgeInfo
            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._extract_nix_constructs
                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._extract_from_tree
                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._extract_nix_flake_input_urls
                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser.visit
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._nix_attrpath_parts
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser.first_path_arg
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser.head_call_name
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser.visit
                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._extract_nix_import_targets
                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser.visit
                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._is_nix_flake_file
                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._nix_attrpath_parts
                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._qualify
                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._resolve_module_to_file
                * → code-graph/src/codegraph/parsing/parser.py::EdgeInfo
                * → code-graph/src/codegraph/parsing/parser.py::NodeInfo
            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._extract_pascal_constructs
                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._extract_from_tree
                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._pascal_call_parts
                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._pascal_params
                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._pascal_proc_name
                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._pascal_return_type
                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._pascal_type_name
                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._qualify
                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._resolve_call_target
                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._resolve_pascal_unit
                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._pascal_dir_listing
                * → code-graph/src/codegraph/parsing/parser.py::EdgeInfo
                * → code-graph/src/codegraph/parsing/parser.py::NodeInfo
                * → code-graph/src/codegraph/parsing/parser.py::_is_test_function
            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._extract_r_constructs
                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._handle_r_binary_operator
                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._extract_from_tree
                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._get_params
                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._handle_r_class_call
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._extract_r_methods
                            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._extract_from_tree
                            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._get_params
                            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._qualify
                            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._r_iter_args
                            * → code-graph/src/codegraph/parsing/parser.py::EdgeInfo
                            * → code-graph/src/codegraph/parsing/parser.py::NodeInfo
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._qualify
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._r_find_named_arg
                            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._r_iter_args
                        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._r_first_string_arg
                        * → code-graph/src/codegraph/parsing/parser.py::EdgeInfo
                        * → code-graph/src/codegraph/parsing/parser.py::NodeInfo
                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._qualify
                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._r_call_func_name
                    * → code-graph/src/codegraph/parsing/parser.py::EdgeInfo
                    * → code-graph/src/codegraph/parsing/parser.py::NodeInfo
                    * → code-graph/src/codegraph/parsing/parser.py::_is_test_function
                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._handle_r_call
                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._extract_from_tree
                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._extract_import
                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._get_call_name
                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._handle_r_class_call
                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._qualify
                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._r_call_func_name
                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._resolve_call_target
                    * → code-graph/src/codegraph/parsing/parser.py::EdgeInfo
            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._extract_solidity_constructs
                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._qualify
                * → code-graph/src/codegraph/parsing/parser.py::EdgeInfo
                * → code-graph/src/codegraph/parsing/parser.py::NodeInfo
            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._extract_value_references
                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._emit_reference_if_known
                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._resolve_call_target
                    * → code-graph/src/codegraph/parsing/parser.py::EdgeInfo
                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._qualify
                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._ref_from_arguments
                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._emit_reference_if_known
                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._ref_from_array
                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._emit_reference_if_known
                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._ref_from_assignment
                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._emit_reference_if_known
                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._ref_from_pair
                    * → code-graph/src/codegraph/parsing/parser.py::CodeParser._emit_reference_if_known
        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._get_parser
        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._parse_databricks_py_notebook
            * → code-graph/src/codegraph/parsing/parser.py::CellInfo
            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._parse_notebook_cells
                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._collect_file_scope
                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._extract_from_tree
                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._get_parser
                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._qualify
                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._resolve_call_targets
                * → code-graph/src/codegraph/parsing/parser.py::EdgeInfo
                * → code-graph/src/codegraph/parsing/parser.py::NodeInfo
                * → code-graph/src/codegraph/parsing/parser.py::_is_test_file
            * → code-graph/src/codegraph/parsing/parser.py::NodeInfo
            * → code-graph/src/codegraph/parsing/parser.py::_is_test_file
        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._parse_notebook
            * → code-graph/src/codegraph/parsing/parser.py::CellInfo
            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._parse_notebook_cells
            * → code-graph/src/codegraph/parsing/parser.py::NodeInfo
            * → code-graph/src/codegraph/parsing/parser.py::_is_test_file
        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._parse_rescript
            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._qualify
            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._resolve_call_targets
            * → code-graph/src/codegraph/parsing/parser.py::CodeParser.enclosing_module
            * → code-graph/src/codegraph/parsing/parser.py::CodeParser.is_top_level
            * → code-graph/src/codegraph/parsing/parser.py::CodeParser.offset_to_line
            * → code-graph/src/codegraph/parsing/parser.py::EdgeInfo
            * → code-graph/src/codegraph/parsing/parser.py::NodeInfo
            * → code-graph/src/codegraph/parsing/parser.py::_is_test_file
            * → code-graph/src/codegraph/parsing/parser.py::_is_test_function
                * → code-graph/src/codegraph/parsing/parser.py::_is_test_file
            * → code-graph/src/codegraph/parsing/parser.py::_rescript_brace_depth_array
            * → code-graph/src/codegraph/parsing/parser.py::_scan_rescript_modules
                * → code-graph/src/codegraph/parsing/parser.py::CodeParser.offset_to_line
            * → code-graph/src/codegraph/parsing/parser.py::_strip_rescript_noise
        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._parse_sql
            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._get_parser
            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._walk_sql_tree
                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._extract_sql_ddl
                    * → code-graph/src/codegraph/parsing/parser.py::EdgeInfo
                    * → code-graph/src/codegraph/parsing/parser.py::NodeInfo
                * → code-graph/src/codegraph/parsing/parser.py::CodeParser._walk_sql_tree
            * → code-graph/src/codegraph/parsing/parser.py::EdgeInfo
            * → code-graph/src/codegraph/parsing/parser.py::NodeInfo
            * → code-graph/src/codegraph/parsing/parser.py::_is_test_file
        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._parse_svelte
            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._collect_file_scope
            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._extract_from_tree
            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._get_parser
            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._qualify
            * → code-graph/src/codegraph/parsing/parser.py::EdgeInfo
            * → code-graph/src/codegraph/parsing/parser.py::NodeInfo
            * → code-graph/src/codegraph/parsing/parser.py::_is_test_file
        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._parse_vue
            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._collect_file_scope
            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._extract_from_tree
            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._get_parser
            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._qualify
            * → code-graph/src/codegraph/parsing/parser.py::EdgeInfo
            * → code-graph/src/codegraph/parsing/parser.py::NodeInfo
            * → code-graph/src/codegraph/parsing/parser.py::_is_test_file
        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._qualify
        * → code-graph/src/codegraph/parsing/parser.py::CodeParser._resolve_call_targets
            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._qualify
            * → code-graph/src/codegraph/parsing/parser.py::EdgeInfo
        * → code-graph/src/codegraph/parsing/parser.py::CodeParser.detect_language
            * → code-graph/src/codegraph/parsing/parser.py::CodeParser._detect_language_from_shebang
        * → code-graph/src/codegraph/parsing/parser.py::EdgeInfo
        * → code-graph/src/codegraph/parsing/parser.py::NodeInfo
        * → code-graph/src/codegraph/parsing/parser.py::_is_test_file
<!-- CTX-SYNC:FLOW-END -->

<!-- CTX-SYNC:FLOW-START -->
* flow=ctx-sync/src/ctxsync/cli.py::sync
    * entry_point=ctx-sync/src/ctxsync/cli.py::sync
    * criticality=0.7
    * nodes=45/75
* ctx-sync/src/ctxsync/cli.py::sync
    * → ctx-sync/src/ctxsync/cli.py::_atomic_write
    * → ctx-sync/src/ctxsync/cli.py::_read_payload
        * → ctx-sync/src/ctxsync/cli.py::_run_code_graph
            * → ctx-sync/src/ctxsync/graph.py::GraphInputError
        * → ctx-sync/src/ctxsync/graph.py::GraphInputError
        * → ctx-sync/src/ctxsync/graph.py::load_graph
            * → ctx-sync/src/ctxsync/graph.py::GraphInputError
    * → ctx-sync/src/ctxsync/cli.py::_write_flow_document
        * → ctx-sync/src/ctxsync/cli.py::_atomic_write
    * → ctx-sync/src/ctxsync/graph.py::canonical_context_type
        * → ctx-sync/src/ctxsync/graph.py::ContextTypeError
    * → ctx-sync/src/ctxsync/graph.py::entities_for_context
        * → ctx-sync/src/ctxsync/graph.py::ContextTypeError
        * → ctx-sync/src/ctxsync/graph.py::_from_communities
            * → ctx-sync/src/ctxsync/graph.py::GraphInputError
            * → ctx-sync/src/ctxsync/graph.py::_as_int
            * → ctx-sync/src/ctxsync/graph.py::_dedupe
            * → ctx-sync/src/ctxsync/graph.py::_membership_digest
                * → ctx-sync/src/ctxsync/graph.py::_path_digest
            * → ctx-sync/src/ctxsync/graph.py::_posix
            * → ctx-sync/src/ctxsync/graph.py::_relative
            * → ctx-sync/src/ctxsync/graph.py::_single_line
            * → ctx-sync/src/ctxsync/model.py::Entity
        * → ctx-sync/src/ctxsync/graph.py::canonical_context_type
        * → ctx-sync/src/ctxsync/graph.py::entities_from_graph
            * → ctx-sync/src/ctxsync/graph.py::GraphInputError
            * → ctx-sync/src/ctxsync/graph.py::_from_entities
                * → ctx-sync/src/ctxsync/graph.py::_as_int
                * → ctx-sync/src/ctxsync/graph.py::_as_opt_str
                * → ctx-sync/src/ctxsync/graph.py::_bare_name
                * → ctx-sync/src/ctxsync/graph.py::_dedupe
                * → ctx-sync/src/ctxsync/graph.py::_posix
                * → ctx-sync/src/ctxsync/graph.py::_relative
                * → ctx-sync/src/ctxsync/model.py::Entity
            * → ctx-sync/src/ctxsync/graph.py::_from_nodes
                * → ctx-sync/src/ctxsync/graph.py::_as_int
                * → ctx-sync/src/ctxsync/graph.py::_as_opt_str
                * → ctx-sync/src/ctxsync/graph.py::_dedupe
                * → ctx-sync/src/ctxsync/graph.py::_node_qn
                    * → ctx-sync/src/ctxsync/graph.py::_lookup_node
                * → ctx-sync/src/ctxsync/graph.py::_posix
                * → ctx-sync/src/ctxsync/graph.py::_ref
                    * → ctx-sync/src/ctxsync/graph.py::_lookup_node
                    * → ctx-sync/src/ctxsync/graph.py::_posix
                    * → ctx-sync/src/ctxsync/graph.py::_relative
                * → ctx-sync/src/ctxsync/graph.py::_relative
                * → ctx-sync/src/ctxsync/model.py::Entity
    * → ctx-sync/src/ctxsync/graph.py::flows_for_context
        * → ctx-sync/src/ctxsync/graph.py::GraphInputError
        * → ctx-sync/src/ctxsync/graph.py::_as_int
        * → ctx-sync/src/ctxsync/graph.py::_dedupe_flows
        * → ctx-sync/src/ctxsync/graph.py::_rel_ref
            * → ctx-sync/src/ctxsync/graph.py::_posix
            * → ctx-sync/src/ctxsync/graph.py::_relative
        * → ctx-sync/src/ctxsync/graph.py::_rel_tree
            * → ctx-sync/src/ctxsync/graph.py::_rel_ref
            * → ctx-sync/src/ctxsync/graph.py::_rel_tree
        * → ctx-sync/src/ctxsync/graph.py::_single_line
    * → ctx-sync/src/ctxsync/markdown.py::parse_document
        * → ctx-sync/src/ctxsync/markdown.py::_marker_regions
            * → ctx-sync/src/ctxsync/markdown.py::block_pattern
                * → ctx-sync/src/ctxsync/markdown.py::block_markers
            * → ctx-sync/src/ctxsync/model.py::ContextEntity
        * → ctx-sync/src/ctxsync/model.py::ContextDocument
    * → ctx-sync/src/ctxsync/sync.py::sync_document
        * → ctx-sync/src/ctxsync/markdown.py::append_blocks
        * → ctx-sync/src/ctxsync/markdown.py::render_block
            * → ctx-sync/src/ctxsync/markdown.py::_fact_lines
                * → ctx-sync/src/ctxsync/markdown.py::_line_range
                * → ctx-sync/src/ctxsync/markdown.py::_one_line
            * → ctx-sync/src/ctxsync/markdown.py::block_markers
        * → ctx-sync/src/ctxsync/markdown.py::render_new_document
            * → ctx-sync/src/ctxsync/markdown.py::render_block
        * → ctx-sync/src/ctxsync/model.py::SyncResult
<!-- CTX-SYNC:FLOW-END -->

<!-- CTX-SYNC:FLOW-START -->
* flow=ctx-sync/src/ctxsync/flows_doc.py::parse_blocks
    * entry_point=ctx-sync/src/ctxsync/flows_doc.py::parse_blocks
    * criticality=0.36
    * nodes=2/2
* ctx-sync/src/ctxsync/flows_doc.py::parse_blocks
    * → ctx-sync/src/ctxsync/flows_doc.py::_parse_meta
<!-- CTX-SYNC:FLOW-END -->

<!-- CTX-SYNC:FLOW-START -->
* flow=ctx-sync/src/ctxsync/flows_doc.py::render_document
    * entry_point=ctx-sync/src/ctxsync/flows_doc.py::render_document
    * criticality=0.445
    * nodes=6/7
* ctx-sync/src/ctxsync/flows_doc.py::render_document
    * → ctx-sync/src/ctxsync/flows_doc.py::FlowSyncResult
    * → ctx-sync/src/ctxsync/flows_doc.py::_parse_meta
    * → ctx-sync/src/ctxsync/flows_doc.py::render_block
        * → ctx-sync/src/ctxsync/flows_doc.py::_tree_lines
            * → ctx-sync/src/ctxsync/flows_doc.py::_tree_lines
    * → ctx-sync/src/ctxsync/markdown.py::append_blocks
<!-- CTX-SYNC:FLOW-END -->

<!-- CTX-SYNC:FLOW-START -->
* flow=ctx-sync/src/ctxsync/sync.py::render_empty
    * entry_point=ctx-sync/src/ctxsync/sync.py::render_empty
    * criticality=0.54
    * nodes=15/17
* ctx-sync/src/ctxsync/sync.py::render_empty
    * → ctx-sync/src/ctxsync/markdown.py::parse_document
        * → ctx-sync/src/ctxsync/markdown.py::_marker_regions
            * → ctx-sync/src/ctxsync/markdown.py::block_pattern
                * → ctx-sync/src/ctxsync/markdown.py::block_markers
            * → ctx-sync/src/ctxsync/model.py::ContextEntity
        * → ctx-sync/src/ctxsync/model.py::ContextDocument
    * → ctx-sync/src/ctxsync/sync.py::sync_document
        * → ctx-sync/src/ctxsync/markdown.py::append_blocks
        * → ctx-sync/src/ctxsync/markdown.py::render_block
            * → ctx-sync/src/ctxsync/markdown.py::_fact_lines
                * → ctx-sync/src/ctxsync/markdown.py::_line_range
                * → ctx-sync/src/ctxsync/markdown.py::_one_line
            * → ctx-sync/src/ctxsync/markdown.py::block_markers
        * → ctx-sync/src/ctxsync/markdown.py::render_new_document
            * → ctx-sync/src/ctxsync/markdown.py::render_block
        * → ctx-sync/src/ctxsync/model.py::SyncResult
<!-- CTX-SYNC:FLOW-END -->
