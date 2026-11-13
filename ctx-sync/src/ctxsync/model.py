"""Core entities for document synchronization."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Entity:
    """A code entity from a graph dump, normalized for document output.

    ``metadata`` carries type-specific ``(label, value)`` pairs rendered as
    extra ``**Label:** value`` lines. Call-graph entities leave it empty and
    use the dedicated call fields instead.
    """

    entity_id: str
    name: str
    kind: str
    file: str
    line_start: int | None = None
    line_end: int | None = None
    language: str = ""
    signature: str | None = None
    docstring: str | None = None
    calls: tuple[str, ...] = ()
    called_by: tuple[str, ...] = ()
    is_test: bool = False
    metadata: tuple[tuple[str, str], ...] = ()
    show_call_edges: bool = True


@dataclass
class ContextEntity:
    """One managed region found in an existing document.

    ``start`` and ``end`` delimit the bytes that sync may replace: a whole
    ``CTX-SYNC:<TYPE>-START`` to ``CTX-SYNC:<TYPE>-END`` block, markers
    included. Everything outside a region is opaque and is copied through
    unchanged.
    """

    entity_id: str
    start: int = 0
    end: int = 0


@dataclass
class ContextDocument:
    """An existing document plus the managed regions found inside it."""

    text: str = ""
    entities: list[ContextEntity] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.text.strip()


@dataclass
class SyncResult:
    """What changed during a merge.

    ``updated`` counts matched regions whose bytes changed, ``added`` the
    regions appended for entities the document did not have, ``unchanged``
    matched regions that were already current, and ``ignored`` regions left
    alone because the export had no matching entity.
    """

    text: str
    updated: int = 0
    added: int = 0
    unchanged: int = 0
    ignored: int = 0

    @property
    def changed(self) -> bool:
        return bool(self.updated or self.added)
