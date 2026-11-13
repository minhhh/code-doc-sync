# ctx-sync

Reconcile a [`code-graph`](../code-graph) export with a Markdown document,
without touching anything a human wrote.

`ctx-sync` owns the machine facts: signatures, paths, line ranges, calls, and
called-by lists. It rewrites only the bytes between a `CTX-SYNC:<TYPE>-START`
and a `CTX-SYNC:<TYPE>-END` marker, for the blocks whose entity is in the
export. Everything else belongs to the author: headings, prose, block order,
and every block whose entity has left the code stay byte-for-byte.

## Pipeline

One run reads the export, the existing document, and the marked blocks inside
it.

1. Locate every block: a start marker, a body, an end marker.
2. Read the entity ID from the `**ID:**` line inside the body. Flow blocks
   carry a `flow=` line instead.
3. Replace the whole block, markers included, for every block whose ID appears
   in the export.
4. Leave every other block alone, whether the export no longer contains it or
   its contents could not be read.
5. Append a block for each export entity the document does not have yet, at the
   end of the file, in ID order.

No other bytes change: sync does not add section headings, does not reorder
blocks, and does not move anything between sections.

## Install

```bash
cd ctx-sync
python -m pip install -e .
```

Requires Python 3.11+.

## Quick start

```bash
# Pipe a live export
code-graph export --format json --root . | ctx-sync --type call-graph --target CONTEXT.md

# Or from a saved export
code-graph export --format json --root . > .graph_snapshot.json
ctx-sync --type call-graph --input .graph_snapshot.json --target CONTEXT.md

# Let ctx-sync run code-graph itself
ctx-sync --type call-graph --update --root .
```

`--target` defaults to `CONTEXT.md`. The repo root defaults to the target's
directory. Paths and IDs are written relative to the root.

One type per file: a call-graph block is not matched by a community export and
vice versa, so mixing types in one file leaves the other type's blocks stale.
The names used for code-graph's own documents:

| Type | Target |
| --- | --- |
| `call-graph` | `CALL_GRAPH.md` |
| `community` | `ARCHITECTURE.md` |
| `flows` | `FLOWS.md` |

## Commands

| Command | Description |
| --- | --- |
| `ctx-sync [--type call-graph] [--target FILE] [--input FILE]` | Merge a payload from a file or stdin |
| `ctx-sync --update [--root DIR]` | Run `code-graph export --format json` and merge its output |
| `ctx-sync --check` | Exit 1 if `CONTEXT.md` would change; write nothing |
| `ctx-sync --dry-run` | Print the merged document to stdout |
| `ctx-sync --quiet` | Suppress the summary line |
| `ctx-sync version` | Print the version |

## Context types

`--type` selects the projection. Three types are implemented:

| `--type` | Aliases | Renders |
| --- | --- | --- |
| `call-graph` *(default)* | `callgraph`, `ctx` | Functions and classes with their `CALLS` edges |
| `community` | `architecture`, `communities` | One block per community: members, cohesion, language, description, files, inter-community edges |
| `flows` | `flow` | One block per execution flow: a metadata line plus the traced call tree as an indented arrow list |

Each matches a `code-graph export --type` payload, so the projection is chosen
on both sides with one name. All three write the same marker pair and differ
only in the tag: `CALL-GRAPH`, `COMMUNITY`, `FLOW`. A block holds graph facts
and nothing else: no heading, no caption, no generated prose.

```markdown
<!-- CTX-SYNC:CALL-GRAPH-START -->
**Source Status:** active
**ID:** `src/app.py::main`
**Kind:** Function
**File:** `src/app.py`
**Lines:** 3-8
**Language:** python
**Calls:** `src/util.py::helper`
**Called by:** _(none)_
<!-- CTX-SYNC:CALL-GRAPH-END -->
```

```markdown
<!-- CTX-SYNC:COMMUNITY-START -->
**Source Status:** active
**ID:** `community::core#4d562c3a071b`
**Language:** python
**Members:** 7
**Cohesion:** 0.412
**Files:** `src/app.py`, `src/util.py`
**Connected to:** util — 4 edges (CALLS)
<!-- CTX-SYNC:COMMUNITY-END -->
```

```markdown
<!-- CTX-SYNC:FLOW-START -->
* flow=src/hints.py::generate_hints#4d562c3a071b
    * entry_point=src/hints.py::generate_hints
    * criticality=0.4533
    * nodes=9/9
* src/hints.py::generate_hints
    * → src/hints.py::_track_result
        * → src/hints.py::SessionState.record_files
    * → src/hints.py::generate_hints
<!-- CTX-SYNC:FLOW-END -->
```

Four spaces of indentation are one level of depth, `→` marks a call. A target
already expanded earlier is listed again with no children: the call is recorded,
the subtree is not walked twice. Flow blocks are matched by the `flow:` line, so
order and everything outside them is the author's. A flow that disappears from
the export keeps its block verbatim; new flows are appended at the end.

A document-level type marker does not exist, and a file is not checked against
`--type`. What keeps types apart is the marker tag and the ID: a `COMMUNITY`
block never satisfies a `call-graph` export, so it is left alone rather than
rewritten. A payload of the wrong shape fails instead of guessing: the community
loader points at `code-graph export --type community`, and the flows loader at
`code-graph export --type flows`.

## Merge rules

| Situation | Result |
| --- | --- |
| Entity in the export and in the document | Block body regenerated, markers kept in place |
| Entity in the export, missing from the document | New block appended at the end of the file, in ID order |
| Block in the document, missing from the export | Left exactly as it is |
| Block with no readable ID | Left exactly as it is |
| Block of another context type | Left exactly as it is |
| Existing order, prose, and headings | Kept; sync never reorders or rewrites them |

The merge is idempotent: syncing the same graph twice produces the same file.

## Document format

A managed document is ordinary Markdown with marker pairs in it. Notes live
outside the markers, under any heading, in any order. A new document is nothing
but blocks of graph facts: no title, no section heading, no preamble. Every
other line in the file is the author's.

```markdown
<!-- CTX-SYNC:CALL-GRAPH-START -->
**Source Status:** active
**ID:** `src/app.py::main`
**Kind:** Function
**File:** `src/app.py`
**Lines:** 3-8
**Language:** python
**Calls:** `src/util.py::helper`
**Called by:** `src/test_app.py::test_main`
<!-- CTX-SYNC:CALL-GRAPH-END -->

## Why the retry cap is three

Human notes go here, outside the markers. They are never read, matched, or
rewritten.
```

Everything outside the markers is yours, and on an existing document nothing is
ever added, moved, or removed there.

Content inside a marker pair is machine-owned: anything typed there is replaced
on the next sync. Content that looks like a marker but is not one of the three
known tags is ignored, as is a start marker with no matching end marker.

## Input schemas

The loader accepts either:

- A `code-graph export --format json` payload with `nodes` and `edges`. `CALLS` edges may use
  `source`/`target` or `source_qualified`/`target_qualified`; bare targets are
  resolved when the name is unique.
- A pre-built `entities` array with `id`, `file`, `symbol`, `kind`, `loc`,
  `calls`, `called_by`, `signature`, and optional `docstring`.

`File` nodes are skipped; only `Function`, `Class`, `Type`, and `Test` entities
become blocks.

## Guarantees and limits

- Every byte outside a marker pair is preserved, not reflowed.
- Only bytes inside a matched marker pair change.
- Writes are atomic (`.tmp` then replace).
- Unmarked prose and unmatched blocks are not managed, and are never removed.
- The target file is the only file touched.
