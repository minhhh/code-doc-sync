# Issues

## ISSUE-001 — `external_calls` scores stdlib method calls, not external dependencies

**Severity:** high (silently corrupts flow ranking)
**Component:** `code-graph`
**Status:** fixed
**Affects:** `compute_criticality` (`src/codegraph/query/flows.py`), every `flows` consumer (`flow`, `flows`, `export --type flows`, `ctx-sync --type flows`)

### Symptom

`criticality` is meant to rank execution flows by risk. In this repository it
ranks them by how much stdlib surface they touch.

Across 57 detected flows (average criticality `0.3693`), the weighted
contributions are:

| Factor | Weight | Avg contribution | Share |
| --- | --- | --- | --- |
| `external_calls` | 0.20 | 0.1896 | **51%** |
| `test_gap` | 0.15 | 0.1500 | 40% |
| `depth` | 0.10 | 0.0195 | 5% |
| `security_hits` | 0.25 | 0.0127 | 3% |
| `file_spread` | 0.30 | 0.0000 | 0% |

`51 / 57` flows saturate `external_calls` at its cap (≥ 5 counted calls). The
factor with the largest remaining weight, `file_spread`, is `0.0` for all 57
flows — every traced flow sits inside a single file — and `test_gap` is `1.0`
for all 57 (no `TESTED_BY` edges on any traced node), so neither discriminates
anything. The ranking is effectively
`external_calls` alone.

### Root cause

Two defects compound.

**1. "External" is defined as "not a node", and most edges point at non-nodes.**

```python
# flows.py:345
for target_qn in calls_out.get(n.qualified_name, ()):
    if target_qn not in nodes_by_qn:
        external_count += 1
```

Any `CALLS` target that is not a node qualified name counts as an external
dependency. Measured on this repo (3,948 `CALLS` edges):

| Target kind | Distinct names | Edges |
| --- | --- | --- |
| Real internal symbol, stored unqualified | 108 | 458 |
| Not a node at all (builtins, stdlib, third-party) | 162 | 2,829 |
| Resolved to a `qualified_name` | 269 | 661 |

Top offenders: `append` (370), `decode` (217), `get` (202), `len` (195),
`execute` (171), `compile` (97), `str` (87), `set` (73), `fetchall` (70),
`strip` (63). Method calls on objects the parser cannot type are recorded as
bare names and never resolved, so they inflate the count.

**2. The normalization cap is too low for that noise floor.**

`min(external_count / 5.0, 1.0)` saturates at five. A nine-node pure-Python
helper with no external dependencies reaches the cap from `append`/`get`/`len`
alone.

### Evidence — `generate_hints`

```
$ code-graph export --root . --type flows    # 50 of 85 flows (default --limit)
```

```
nodes=9  files=1  external=42  security=3  tested=0  depth=2

file spread  0.000 × 0.30 = 0.0000
external     1.000 × 0.20 = 0.2000     <- 42 counted, capped at 5
security     0.333 × 0.25 = 0.0833
test gap     1.000 × 0.15 = 0.1500
depth        0.200 × 0.10 = 0.0200
                            ------
                            0.4533  = stored criticality
```

`generate_hints` lives entirely in `src/codegraph/cli/hints.py` and calls nothing
outside the project. 42 "external calls" is the bare-name artifact.

### Reproduction

```bash
cd code-graph
code-graph build --root .
code-graph export --root . --type flows > /tmp/flows.json

# Rank flows and compare against how many CALLS targets are non-nodes
python - <<'PY'
import json, collections
from pathlib import Path
from codegraph.graph import GraphStore
from codegraph.flows import get_flows

store = GraphStore(Path(".code-graph/graph.db"))
adj = store.load_flow_adjacency()
for row in sorted(get_flows(store, limit=200), key=lambda r: -r["criticality"])[:10]:
    nodes = [adj.nodes_by_id[i] for i in row["path"] if i in adj.nodes_by_id]
    ext = sum(
        1
        for n in nodes
        for t in adj.calls_out.get(n.qualified_name, ())
        if t not in adj.nodes_by_qn
    )
    print(f"{row['criticality']:.4f}  external={ext:>3}  {row['name']}")
PY
```

The top-ranked flows are those with the most unresolved method calls, not the
ones with the widest blast radius.

### Impact

- `code-graph flows --sort criticality` and `flow <id>` ordering is meaningless.
- `export --type flows` emits a `criticality` field that readers treat as a
  risk score — including `ctx-sync`, which renders `**Criticality:**` into
  `FLOWS.md`.
- Any downstream prioritisation (review order, test effort) built on it is
  inverted or arbitrary.
- The defect is silent: no warning, no confidence marker, plausible-looking
  values in `[0, 1]`.

### Proposed fixes

**A. Redefine the measure (small, high value).** Count a call as external only
when it resolves to a node *outside the flow's own files*, instead of when it
fails to resolve at all. Unresolved bare names then contribute nothing, which
is the correct default — an untracked `append` is not a dependency.

```python
flow_files = {n.file_path for n in nodes}
external_count = sum(
    1
    for n in nodes
    for target in calls_out.get(n.qualified_name, ())
    if (resolved := nodes_by_qn.get(target)) is not None
    and resolved.file_path not in flow_files
)
```

Note the current `FlowAdjacency.calls_out` stores raw targets, so qualified
targets have to be matched against `nodes_by_qn` rather than assumed.

**B. Resolve bare names at parse time (medium, fixes the data).**
`parser.py:2164` (`_resolve_call_targets`) resolves a bare target only
against same-file symbols. Extend
the pass to project-wide names that are unique, leaving genuinely ambiguous
ones untouched. This upgrades 470 edges here and would let a stricter
`external_count` work.

**C. Widen and re-classify the builtin filter (medium).**
`lib/store.py` `BUILTIN_CALL_NAMES` is JavaScript-heavy (`flatMap`,
`charCodeAt`) and misses the Python set actually seen here (`append`, `decode`,
`len`, `get`, `execute`, `fetchall`). Only useful together with A or B — on its
own it is an endless allowlist.

**D. Do not ship a score nobody can trust.** Until A lands, `criticality`
should be documented as a heuristic over call volume, not risk. `ctx-sync`
already renders it as a bare fact.

Recommended order: **A** (stop the bleeding, no data change), then **B**
(improve the underlying edges), and re-measure before touching weights.

### Resolution

Proposed fix A as written — "count a call as external only when it resolves to
a node outside the flow's own files" — is structurally always `0`. Tracing
follows every resolved `CALLS` target into the path, so a resolved callee is by
definition inside the flow. Measured on this repo before changing anything:
`external = 0` for all 38 flows, i.e. the factor would have become an inert
`0.20` of weight rather than a corrected one.

What shipped instead keeps the factor and makes it decidable: **external means
the callee is a member of a module imported from outside the project.**

- The parser collects plain `import x` / `import x as y` into the import map
  (it previously only collected `from x import y`), so module receivers are
  known at all.
- A member call whose receiver is in the import map, whose module has no file in
  the repository (`_resolve_module_to_file` returns `None`), and whose receiver
  type is not declared gets `extra.external_module = <module>`.
- `load_flow_adjacency` collects those into `FlowAdjacency.external_calls`;
  `compute_criticality` counts **distinct modules**, normalized `min(n / 8, 1)`.

That separates `logging.getLogger()` (a dependency) from `seen.add()`
(a local value) and from `append`/`get`/`len` (unresolved bare names, now
counted as nothing).

Re-measured on this repository (38 flows):

| Factor | Saturating before | After |
| --- | --- | --- |
| `external_calls` | 37/38 (97%) | 3/38 (8%) |
| `file_spread` | 0/38 (all zero) | 2/38 (5%) |
| `test_gap` | 38/38 | 38/38 (no `TESTED_BY` edges exist) |
| `security` | 0/38 | 0/38 |
| `depth` | 0/38 | 0/38 |

`file_spread`'s cap was raised from 5 to 8 files (`(file_count - 1) / 7`),
because 24 of 38 flows span 5+ files and the factor saturated at 63%; the
distribution is bimodal (16 flows at 6 files, 2 at 14).

The `generate_hints` example is stale: it now has an incoming call from
`cli/common.py::_with_hints`, so it is no longer traced as an entry point.

Proposed fix B (project-wide bare-name resolution) was not needed for this: the
new definition doesn't depend on bare names resolving, and precision-over-recall
still argues against a name-based fallback pass. Proposal C (`BUILTIN_CALL_NAMES`)
became unnecessary for the same reason.

### Acceptance criteria

- A flow whose nodes are all in one file and whose only out-edges are
  unresolved bare names scores `external = 0`.
- `generate_hints` criticality drops below its current `0.4533` and stops
  sitting at the `external` cap.
- Across this repo, fewer than 20% of flows saturate any single factor.
- `file_spread` becomes non-zero for at least the cross-file flows that exist,
  or the factor's weight is redistributed. Today every one of the 57 traced
  flows spans exactly one file, so the factor cannot fire even after A/B land:
  cross-file `CALLS` edges have to resolve first (see B).

### Related

- Dangling `CALLS` targets also inflate `ctx-sync --type call-graph` output:
  `CALL_GRAPH.md` lists `min`, `round`, `get`, `sort`, `float` under
  `**Calls:**` for functions that call none of them.
- `community` cohesion shows `0.000` for large communities for a related
  reason: edge topology built on partially unresolved targets.

## ISSUE-002 — Custom languages can only be mapped by extension, repo-wide

**Severity:** low (missing capability, not a defect in what exists)
**Component:** `code-graph` (`languages/custom.py`, `.code-graph/languages.toml`)
**Status:** open (feature request)
**Affects:** `parser.CodeParser.detect_language`, every build

### Symptom

The "bring your own language" config (`languages/custom.py`) keys exclusively
on file extension, and one mapping applies to the whole repository. Three cases
it cannot express:

1. **The same extension means different languages in different subtrees.**
   `.cls` is Apex in `force-app/` and VBA or LaTeX elsewhere. Today one entry
   wins for the entire repo, so whichever subtree loses is parsed with the
   wrong grammar (or not at all, if the entry is left out).
2. **Extensionless units.** Delphi/FPC units ship as `Unit1`, `Unit2` with no
   suffix. `detect_language` falls through to a shebang probe
   (`parser.py:855`) and returns `None` — no config key can reach them.
3. **A vendored or generated subtree that needs its own grammar** while the
   same extension elsewhere stays on the built-in parser.

### Workaround

None config-side. Requires forking the parser or renaming files.

### Proposed scope

Add path scoping to `[languages.<name>]`, keeping today's entries valid:

```toml
[languages.apex]
extensions = [".cls", ".trigger"]
paths = ["force-app/**"]           # NEW: only these files

[languages.vba]
extensions = [".cls"]
paths = ["vendor/legacy/**"]       # NEW: same extension, different subtree

[languages.delphi]
paths = ["src/legacy/**"]          # NEW: every file under the prefix
grammar = "pascal"
```

Rules to settle during implementation:

- **Precedence:** a path-scoped match beats an unscoped one; among scoped
  matches the longest prefix wins. Built-in languages must still win over
  custom entries, as they do now.
- **Lookup needs a repo-relative path.** `detect_language(path)` currently
  receives an absolute path only, so the parser must know the repo root (it
  already stores one) and normalise before matching.
- **Config with neither `extensions` nor `paths`** is invalid and skipped with
  the existing `logger.warning`, per the loader's defensive contract.
- **Glob syntax** should stay to `**` and `*` prefixes, validated like
  `_EXTENSION_RE`, so a bad pattern cannot silently disable a language.
- Keep the caps: `MAX_CUSTOM_LANGUAGES`, and the rule that custom entries can
  neither override a built-in extension nor reuse a built-in language name.

### Does not fix `{$I}` includes

Scoping `.inc` to a folder was considered and rejected: an include file is a
textual fragment whose enclosing type lives in the includer, so making `.inc`
visible produces phantom nodes (`frag.inc::DoWork`) beside the real
`Unit1.pas::TThing.DoWork`. Includes need `{$I}`/`{$INCLUDE}` expansion before
the parse, with a line map back to `(file, line)`. That is a separate change in
`parser.py`; see the Pascal notes in `docs/development.md`.

### Acceptance criteria

- Two subtrees with the same extension resolve to different languages in one
  build, and `status` reports both language counts.
- A file matched only by `paths` (no extension entry) is parsed and gets the
  configured `language` field on its nodes.
- A repo with no `languages.toml` behaves exactly as today; an invalid entry
  warns and disables only itself.
- `tests/test_custom_languages.py` covers scoping precedence, the extensionless
  case, and both invalid-config paths.
- `docs/CUSTOM_LANGUAGES.md` exists. The `languages/custom.py` docstring has
  pointed at it since it was written ("See docs/CUSTOM_LANGUAGES.md for the
  full schema reference"), but `docs/` holds only `architecture.md` and
  `development.md` — the schema reference was never written.

## Cross-cutting note: identity must never come from a mutable database column

Two document keys were derived from values that a rebuild rewrites: the flow
key from `flows.id`, and the community key from `communities.id` (the latter
via a `_unique_id` helper in `ctx-sync`, since removed). Both are now digests
of stable content — the flow's ordered qualified names, the community's sorted
membership — produced by `code-graph` (`export.flow_key`,
`export.community_key`) and shipped in the payload as `key`. Verified: both
emitters set `key` (`export.py:216`, `:152`), and all three `ctx-sync`
projections build their `entity_id` without touching a database id —
`_from_nodes` uses `<relative path>::<qualified suffix>`, `_from_communities`
uses the payload `key` (falling back to a membership digest), `flows_doc` uses
`key`.

Note this is not ISSUE-001's defect; the flow-key bug merely appeared in the
same payload as it.

Any future projection must answer: *what makes this entity the same entity
after a full rebuild?* If the answer is a database id, the key is wrong.

### Residual risk

`node_to_dict` still emits `id` (the SQLite row id) on every node and edge, and
`ctx-sync/src/ctxsync/graph.py:111` (`_from_entities`) prefers
`item.get("id")` over `qualified_name` when building an entity. No current
export uses the `entities` shape — `build_payload` emits `nodes` — so nothing
is keyed on a reissued integer today, but that branch is a loaded gun for the
next payload format. `_community_payload` also emits `id`; it is consumed only
for neighbour lookup (`graph.py:246`), never for identity.
