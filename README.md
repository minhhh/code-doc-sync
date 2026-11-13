# code-doc-sync

Two tools that keep a codebase's documentation tied to the code. `code-graph`
builds a structural graph of the repository offline. `ctx-sync` projects that
graph into Markdown, refreshing the marked blocks and leaving everything outside
them byte for byte untouched.

Tree-sitter and SQLite, both local. No network access, no background
service: every answer comes from the on-disk graph.

## Layout

| Path | Role | Docs |
| --- | --- | --- |
| `code-graph/` | Parses the repo, resolves calls and imports, stores the graph, answers queries, and exports projections | [code-graph/README.md](code-graph/README.md) |
| `ctx-sync/` | Merges an export into a Markdown document's marked blocks, keeping notes and everything else it did not write | [ctx-sync/README.md](ctx-sync/README.md) |

The contract between the two is the export payload and its `--type` names,
documented in [code-graph/README.md](code-graph/README.md#export). Both sides
accept `call-graph`, `community` (alias `architecture`), and `flows`.

## Document contract

A managed document is plain Markdown with marker pairs in it. Facts live inside
the markers, notes live outside:

```markdown
<!-- CTX-SYNC:CALL-GRAPH-START -->
**Source Status:** active
**ID:** `src/billing.py::charge_user`
**Kind:** Function
**File:** `src/billing.py`
<!-- CTX-SYNC:CALL-GRAPH-END -->

#### Why the retry cap is three

Notes are ordinary Markdown outside the markers, and sync never reads them.
```

All three types use the same marker pair, differing only in tag:
`CTX-SYNC:CALL-GRAPH-START`/`END`, `CTX-SYNC:COMMUNITY-START`/`END`, and
`CTX-SYNC:FLOW-START`/`END`.

`ctx-sync` rewrites the bytes between the markers of a block whose entity is in
the export, and appends a block for each entity the document does not have yet.
Everything outside the markers stays where it is: headings, prose, block order,
and every block whose entity left the code. That last rule is what keeps notes
attached through renames and removals.

## Quick start

```bash
pip install -e code-graph
pip install -e ctx-sync

code-graph build --root .
ctx-sync --type call-graph --update --root . --target CALL_GRAPH.md
ctx-sync --type community  --update --root . --target ARCHITECTURE.md
ctx-sync --type flows      --update --root . --target FLOWS.md
```

Each document records its projection on the first line, and `ctx-sync` refuses
to write a projection into a file marked with another one. Keep one file per
type.

## Projections

| `--type` | Renders | Document |
| --- | --- | --- |
| `call-graph` (default) | Functions and classes with their `CALLS` edges | `CALL_GRAPH.md` |
| `community` | Communities with size, cohesion, language, files, and inter-community edges | `ARCHITECTURE.md` |
| `flows` | Execution flows as indented call trees | `FLOWS.md` |

## Further reading

- [Architecture](code-graph/docs/architecture.md): build and query flow.
- [Development](code-graph/docs/development.md): extending the tool.
- [ctx-sync rules](ctx-sync/README.md): merge rules and document format.
