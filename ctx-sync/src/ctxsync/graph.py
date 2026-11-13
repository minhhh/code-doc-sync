"""Load code-graph dumps and normalize them into document entities."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from .model import Entity


SUPPORTED_CONTEXT_TYPES = ("call-graph", "community", "flows")

CONTEXT_TYPE_ALIASES = {
    "call-graph": "call-graph",
    "callgraph": "call-graph",
    "ctx": "call-graph",
    "community": "community",
    "communities": "community",
    "architecture": "community",
    "flows": "flows",
    "flow": "flows",
}


class GraphInputError(ValueError):
    """Raised when a dump does not look like a graph payload."""


class ContextTypeError(ValueError):
    """Raised for an unknown or unimplemented context type."""


def canonical_context_type(value: str) -> str:
    """Canonicalize a ``--type`` spelling, or raise :class:`ContextTypeError`."""
    canonical = CONTEXT_TYPE_ALIASES.get(value.strip().lower())
    if canonical is None:
        supported = ", ".join(SUPPORTED_CONTEXT_TYPES)
        raise ContextTypeError(
            f"unsupported context type '{value}'; supported: {supported}"
        )
    return canonical


def entities_for_context(
    context_type: str,
    payload: dict[str, Any],
    *,
    root: Path | None = None,
) -> list[Entity]:
    """Select the projection for a context type.

    ``call-graph`` reads nodes and ``CALLS`` edges, ``community`` reads
    communities plus their inter-community pairs, and ``flows`` reads execution
    paths. Unknown types fail with the supported list instead of guessing.
    """
    canonical = canonical_context_type(context_type)
    if canonical == "community":
        return _from_communities(payload, root=root)
    if canonical == "flows":
        raise ContextTypeError(
            "the flows context type is written by the flow document renderer"
        )
    return entities_from_graph(payload, root=root)


def load_graph(path: Path | None = None, text: str | None = None) -> dict[str, Any]:
    """Read a JSON graph payload from a file or an in-memory string."""
    if path is not None:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise GraphInputError(f"cannot read {path}: {exc}") from exc
    if text is None or not text.strip():
        raise GraphInputError("no graph input: pass --input, --update, or pipe a dump on stdin")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise GraphInputError(f"invalid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise GraphInputError("graph input must be a JSON object")
    return payload


def entities_from_graph(
    payload: dict[str, Any],
    *,
    root: Path | None = None,
) -> list[Entity]:
    """Normalize either a code-graph dump or a pre-built ``entities`` array."""
    status = payload.get("status")
    if status in {"error", "not_found"}:
        detail = payload.get("error") or payload.get("summary") or status
        raise GraphInputError(f"graph input is not usable: {detail}")
    raw_entities = payload.get("entities")
    if isinstance(raw_entities, list):
        return _from_entities(raw_entities, root=root)
    nodes = payload.get("nodes")
    if isinstance(nodes, list):
        return _from_nodes(nodes, payload.get("edges", []), root=root)
    raise GraphInputError("graph input has neither 'entities' nor 'nodes'")


def _from_entities(raw_entities: list[Any], *, root: Path | None) -> list[Entity]:
    entities: list[Entity] = []
    for item in raw_entities:
        if not isinstance(item, dict):
            continue
        entity_id = str(item.get("id") or item.get("qualified_name") or "")
        if not entity_id:
            continue
        loc_value = item.get("loc")
        loc: dict[str, Any] = loc_value if isinstance(loc_value, dict) else {}
        calls_value = item.get("calls")
        calls: list[Any] = calls_value if isinstance(calls_value, list) else []
        called_by_value = item.get("called_by")
        called_by: list[Any] = called_by_value if isinstance(called_by_value, list) else []
        file_value = str(item.get("file") or item.get("file_path") or "")
        entities.append(
            Entity(
                entity_id=_posix(_relative(entity_id, root)),
                name=str(item.get("symbol") or item.get("name") or _bare_name(entity_id)),
                kind=str(item.get("kind") or "Function"),
                file=_posix(_relative(file_value, root)),
                line_start=_as_int(loc.get("start_line", item.get("line_start"))),
                line_end=_as_int(loc.get("end_line", item.get("line_end"))),
                language=str(item.get("language") or ""),
                signature=_as_opt_str(item.get("signature")),
                docstring=_as_opt_str(item.get("docstring")),
                calls=tuple(_posix(_relative(str(c), root)) for c in calls),
                called_by=tuple(_posix(_relative(str(c), root)) for c in called_by),
                is_test=bool(item.get("is_test", False)),
            )
        )
    return _dedupe(sorted(entities, key=lambda e: e.entity_id))


def _from_nodes(
    nodes: list[Any],
    edges: Any,
    *,
    root: Path | None,
) -> list[Entity]:
    by_qn: dict[str, dict[str, Any]] = {}
    by_name: dict[str, list[dict[str, Any]]] = {}
    for node in nodes:
        if isinstance(node, dict) and node.get("qualified_name"):
            by_qn[str(node["qualified_name"])] = node
            name = str(node.get("name") or "")
            if name:
                by_name.setdefault(name, []).append(node)

    calls_by_source: dict[str, list[str]] = {}
    called_by_target: dict[str, list[str]] = {}
    if isinstance(edges, list):
        for edge in edges:
            if not isinstance(edge, dict) or edge.get("kind") != "CALLS":
                continue
            source = str(edge.get("source_qualified") or edge.get("source") or "")
            target = str(edge.get("target_qualified") or edge.get("target") or "")
            if not source or not target:
                continue
            source_key = _node_qn(source, by_qn, by_name)
            target_key = _node_qn(target, by_qn, by_name)
            calls_by_source.setdefault(source_key, []).append(target)
            called_by_target.setdefault(target_key, []).append(source)

    entities: list[Entity] = []
    for qn, node in by_qn.items():
        kind = str(node.get("kind") or "")
        if kind == "File":
            continue
        file_value = str(node.get("file_path") or node.get("name") or "")
        rel_file = _posix(_relative(file_value, root))
        suffix = qn.split("::", 1)[1] if "::" in qn else str(node.get("name") or "")
        entity_id = f"{rel_file}::{suffix}" if suffix else rel_file
        entities.append(
            Entity(
                entity_id=entity_id,
                name=str(node.get("name") or suffix),
                kind=kind or "Function",
                file=rel_file,
                line_start=_as_int(node.get("line_start")),
                line_end=_as_int(node.get("line_end")),
                language=str(node.get("language") or ""),
                signature=_as_opt_str(node.get("signature")),
                calls=tuple(
                    dict.fromkeys(
                        _ref(target, by_qn, by_name, root)
                        for target in calls_by_source.get(qn, [])
                    )
                ),
                called_by=tuple(
                    dict.fromkeys(
                        _ref(source, by_qn, by_name, root)
                        for source in called_by_target.get(qn, [])
                    )
                ),
                is_test=bool(node.get("is_test", False)),
            )
        )
    return _dedupe(sorted(entities, key=lambda e: e.entity_id))


def _from_communities(
    payload: dict[str, Any],
    *,
    root: Path | None,
) -> list[Entity]:
    """One entity per community, with its inter-community edges as relations."""
    raw_communities = payload.get("communities")
    if not isinstance(raw_communities, list):
        raise GraphInputError(
            "community payload has no 'communities' array; "
            "run `code-graph export --type community`"
        )

    names: dict[Any, str] = {}
    for community in raw_communities:
        if isinstance(community, dict):
            names[community.get("id")] = _single_line(str(community.get("name") or ""))

    neighbours: dict[Any, list[str]] = {}
    raw_edges = payload.get("inter_community_edges")
    if isinstance(raw_edges, list):
        for edge in raw_edges:
            if not isinstance(edge, dict):
                continue
            source = edge.get("source_community")
            target = edge.get("target_community")
            count = _as_int(edge.get("edge_count")) or 0
            label = f"{count} edge{'s' if count != 1 else ''}"
            kinds = edge.get("edge_kinds")
            if isinstance(kinds, list) and kinds:
                label += " (" + "/".join(str(kind) for kind in kinds) + ")"
            neighbours.setdefault(source, []).append(f"{names.get(target, target)} — {label}")
            neighbours.setdefault(target, []).append(f"{names.get(source, source)} — {label}")

    entities: list[Entity] = []
    seen: set[str] = set()
    for community in raw_communities:
        if not isinstance(community, dict):
            continue
        community_id = community.get("id")
        name = _single_line(str(community.get("name") or "")) or f"community-{community_id}"
        # Names collide (two communities can both be called
        # ``codegraph-find``) and the numeric id is reissued on every rebuild,
        # so identity is the name plus a digest of the membership.
        key = str(community.get("key") or "")
        digest = key.partition("#")[2]
        if not digest:
            digest = _membership_digest(community)
        entity_id = f"community::{name}#{digest}"
        if entity_id in seen:
            continue
        seen.add(entity_id)
        metadata: list[tuple[str, str]] = []
        size = _as_int(community.get("size")) or _as_int(community.get("member_count"))
        if size is not None:
            metadata.append(("Members", str(size)))
        cohesion = community.get("cohesion")
        if isinstance(cohesion, (int, float)):
            metadata.append(("Cohesion", f"{float(cohesion):.3f}"))
        description = _single_line(str(community.get("description") or ""))
        if description:
            metadata.append(("Description", description))
        files = community.get("files")
        if isinstance(files, list) and files:
            rendered = [_posix(_relative(str(path), root)) for path in files]
            metadata.append(("Files", ", ".join(f"`{path}`" for path in rendered)))
        connected = neighbours.get(community_id)
        if connected:
            metadata.append(("Connected to", "; ".join(connected)))

        entities.append(
            Entity(
                entity_id=entity_id,
                name=name,
                kind="",
                file="",
                language=str(community.get("dominant_language") or ""),
                metadata=tuple(metadata),
                show_call_edges=False,
            )
        )
    return _dedupe(sorted(entities, key=lambda e: e.entity_id))


def flows_for_context(
    payload: dict[str, Any],
    *,
    root: Path | None = None,
) -> list[dict[str, Any]]:
    """Normalize a flows payload into repo-relative flow documents."""
    raw_flows = payload.get("flows")
    if not isinstance(raw_flows, list):
        raise GraphInputError(
            "flows payload has no 'flows' array; "
            "run `code-graph export --type flows`"
        )

    flows: list[dict[str, Any]] = []
    for flow in raw_flows:
        if not isinstance(flow, dict):
            continue
        tree = flow.get("root")
        key = str(flow.get("key") or "")
        if not isinstance(tree, dict) or not key:
            raise GraphInputError(
                "flows payload is missing 'key'/'root'; "
                "re-export with `code-graph export --type flows`"
            )
        path, digest = (key.split("#", 1) + [""])[:2]
        normalized = {
            "key": f"{_rel_ref(path, root)}#{digest}" if digest else _rel_ref(path, root),
            "name": _single_line(str(flow.get("name") or "")),
            "entry_point": _rel_ref(str(flow.get("entry_point") or ""), root),
            "criticality": flow.get("criticality"),
            "unique_nodes": _as_int(flow.get("unique_nodes")),
            "tree_nodes": _as_int(flow.get("tree_nodes")),
            "root": _rel_tree(tree, root),
        }
        flows.append({k: v for k, v in normalized.items() if v is not None})

    return _dedupe_flows(flows)


def _rel_tree(node: dict[str, Any], root: Path | None) -> dict[str, Any]:
    """Relativize every qualified name inside a flow tree."""
    if "ref" in node:
        return {"ref": _rel_ref(str(node["ref"]), root)}
    calls = node.get("calls")
    return {
        "qn": _rel_ref(str(node.get("qn") or ""), root),
        "calls": [
            _rel_tree(child, root)
            for child in (calls if isinstance(calls, list) else [])
            if isinstance(child, dict)
        ],
    }


def _rel_ref(value: str, root: Path | None) -> str:
    return _posix(_relative(value, root))


def _dedupe_flows(flows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for flow in flows:
        key = str(flow["key"])
        if key in seen:
            continue
        seen.add(key)
        result.append(flow)
    return sorted(result, key=lambda flow: str(flow["key"]))


def _membership_digest(community: dict[str, Any]) -> str:
    """Fallback identity for payloads that predate the ``key`` field."""
    files = community.get("files")
    parts = [str(path) for path in files] if isinstance(files, list) else []
    parts.append(str(community.get("member_count") or community.get("size") or ""))
    return _path_digest(parts)


def _path_digest(step_qns: list[str]) -> str:
    """Digest an ordered path into a stable short key."""
    return hashlib.sha1(
        "\n".join(step_qns).encode("utf-8"), usedforsecurity=False
    ).hexdigest()[:12]


def _single_line(value: str) -> str:
    """Collapse whitespace so a value cannot break a field line."""
    return " ".join(value.split())


def _ref(
    qualified_name: str,
    by_qn: dict[str, dict[str, Any]],
    by_name: dict[str, list[dict[str, Any]]],
    root: Path | None,
) -> str:
    node = _lookup_node(qualified_name, by_qn, by_name)
    resolved_name = qualified_name
    if node is not None and "::" not in qualified_name:
        resolved_name = str(node["qualified_name"])
    if node is None:
        return _posix(_relative(qualified_name, root))
    file_value = str(node.get("file_path") or node.get("name") or "")
    rel_file = _posix(_relative(file_value, root))
    suffix = (
        resolved_name.split("::", 1)[1]
        if "::" in resolved_name
        else str(node.get("name") or "")
    )
    return f"{rel_file}::{suffix}" if suffix else rel_file


def _node_qn(
    qualified_name: str,
    by_qn: dict[str, dict[str, Any]],
    by_name: dict[str, list[dict[str, Any]]],
) -> str:
    node = _lookup_node(qualified_name, by_qn, by_name)
    if node is None:
        return qualified_name
    return str(node["qualified_name"])


def _lookup_node(
    qualified_name: str,
    by_qn: dict[str, dict[str, Any]],
    by_name: dict[str, list[dict[str, Any]]],
) -> dict[str, Any] | None:
    node = by_qn.get(qualified_name)
    if node is not None or "::" in qualified_name:
        return node
    candidates = by_name.get(qualified_name, [])
    return candidates[0] if len(candidates) == 1 else None


def _relative(value: str, root: Path | None) -> str:
    if not value:
        return ""
    path_text, suffix = (value.split("::", 1) + [""])[:2] if "::" in value else (value, "")
    path = Path(path_text)
    if root is not None and path.is_absolute():
        try:
            path = path.relative_to(root)
        except ValueError:
            try:
                path = Path(os.path.relpath(path, root))
            except ValueError:
                pass
    rendered = path.as_posix()
    return f"{rendered}::{suffix}" if suffix else rendered


def _posix(value: str) -> str:
    return value.replace("\\", "/")


def _bare_name(entity_id: str) -> str:
    tail = entity_id.rsplit("::", 1)[-1]
    return tail.rsplit(".", 1)[-1]


def _as_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _as_opt_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _dedupe(entities: list[Entity]) -> list[Entity]:
    seen: set[str] = set()
    result: list[Entity] = []
    for entity in entities:
        if entity.entity_id in seen:
            continue
        seen.add(entity.entity_id)
        result.append(entity)
    return result
