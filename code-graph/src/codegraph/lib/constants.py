"""Shared constants for code-graph."""

from __future__ import annotations

import os

SECURITY_KEYWORDS: frozenset[str] = frozenset({
    "auth", "login", "password", "token", "session", "crypt", "secret",
    "credential", "permission", "sql", "query", "execute", "connect",
    "socket", "request", "http", "sanitize", "validate", "encrypt",
    "decrypt", "hash", "sign", "verify", "admin", "privilege",
})

# ---------------------------------------------------------------------------
# Configurable limits (override via environment variables)
# ---------------------------------------------------------------------------
MAX_IMPACT_NODES = int(os.environ.get("CODE_GRAPH_MAX_IMPACT_NODES", "500"))
MAX_IMPACT_DEPTH = int(os.environ.get("CODE_GRAPH_MAX_IMPACT_DEPTH", "2"))

# BFS engine: "sql" (SQLite recursive CTE) or "networkx" (Python-side BFS)
BFS_ENGINE = os.environ.get("CODE_GRAPH_BFS_ENGINE", "sql")
