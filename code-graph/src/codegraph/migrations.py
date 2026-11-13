"""Schema migration framework for the code-graph SQLite database.

A versioned registry of migration functions, each applied in its own
transaction, with the current version stored in ``metadata.schema_version``.

This project ships a single baseline (v1) that brings a freshly initialised
database to the final schema.  Historical migrations are intentionally not
carried over; future schema changes append v2, v3, ...
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Callable

logger = logging.getLogger(__name__)


def get_schema_version(conn: sqlite3.Connection) -> int:
    """Read the current schema version (0 when metadata does not exist)."""
    try:
        row = conn.execute(
            "SELECT value FROM metadata WHERE key = 'schema_version'"
        ).fetchone()
        if row is None:
            return 0
        return int(row[0] if isinstance(row, (tuple, list)) else row["value"])
    except sqlite3.OperationalError:
        return 0


def _set_schema_version(conn: sqlite3.Connection, version: int) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO metadata (key, value) VALUES ('schema_version', ?)",
        (str(version),),
    )


_KNOWN_TABLES = frozenset({
    "nodes", "edges", "metadata", "communities", "flows", "flow_memberships",
    "nodes_fts", "community_summaries", "flow_snapshots", "risk_index",
})


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    """Check if a column exists in a table."""
    if table not in _KNOWN_TABLES:
        raise ValueError(f"Unknown table: {table}")
    cursor = conn.execute(f"PRAGMA table_info({table})")  # noqa: S608
    columns = [row[1] if isinstance(row, tuple) else row["name"] for row in cursor]
    return column in columns


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    """Check if a table exists."""
    if table not in _KNOWN_TABLES:
        raise ValueError(f"Unknown table: {table}")
    row = conn.execute(
        "SELECT count(*) FROM sqlite_master WHERE type IN ('table', 'view') "
        "AND name = ?",
        (table,),
    ).fetchone()
    return bool(row[0] > 0)


# ---------------------------------------------------------------------------
# Baseline: final schema
# ---------------------------------------------------------------------------


def _migrate_v1(conn: sqlite3.Connection) -> None:
    """v1: complete final schema (signatures, flows, communities, FTS, summaries)."""
    # v2: signature column (v9 confidence columns already live in the core schema)
    if not _has_column(conn, "nodes", "signature"):
        conn.execute("ALTER TABLE nodes ADD COLUMN signature TEXT")

    # v3: flows
    conn.execute("""
        CREATE TABLE IF NOT EXISTS flows (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            entry_point_id INTEGER NOT NULL,
            depth INTEGER NOT NULL,
            node_count INTEGER NOT NULL,
            file_count INTEGER NOT NULL,
            criticality REAL NOT NULL DEFAULT 0.0,
            path_json TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS flow_memberships (
            flow_id INTEGER NOT NULL,
            node_id INTEGER NOT NULL,
            position INTEGER NOT NULL,
            PRIMARY KEY (flow_id, node_id)
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_flows_criticality ON flows(criticality DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_flows_entry ON flows(entry_point_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_flow_memberships_node ON flow_memberships(node_id)"
    )

    # v4: communities
    conn.execute("""
        CREATE TABLE IF NOT EXISTS communities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            level INTEGER NOT NULL DEFAULT 0,
            parent_id INTEGER,
            cohesion REAL NOT NULL DEFAULT 0.0,
            size INTEGER NOT NULL DEFAULT 0,
            dominant_language TEXT,
            description TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    if not _has_column(conn, "nodes", "community_id"):
        conn.execute("ALTER TABLE nodes ADD COLUMN community_id INTEGER")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_nodes_community ON nodes(community_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_communities_parent ON communities(parent_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_communities_cohesion ON communities(cohesion DESC)"
    )

    # v5: FTS5
    if not _table_exists(conn, "nodes_fts"):
        conn.execute("""
            CREATE VIRTUAL TABLE nodes_fts USING fts5(
                name, qualified_name, file_path, signature,
                content='nodes', content_rowid='rowid',
                tokenize='porter unicode61'
            )
        """)

    # v6: summary / risk tables
    conn.execute("""
        CREATE TABLE IF NOT EXISTS community_summaries (
            community_id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            purpose TEXT DEFAULT '',
            key_symbols TEXT DEFAULT '[]',
            risk TEXT DEFAULT 'unknown',
            size INTEGER DEFAULT 0,
            dominant_language TEXT DEFAULT '',
            FOREIGN KEY (community_id) REFERENCES communities(id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS flow_snapshots (
            flow_id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            entry_point TEXT NOT NULL,
            critical_path TEXT DEFAULT '[]',
            criticality REAL DEFAULT 0.0,
            node_count INTEGER DEFAULT 0,
            file_count INTEGER DEFAULT 0,
            FOREIGN KEY (flow_id) REFERENCES flows(id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS risk_index (
            node_id INTEGER PRIMARY KEY,
            qualified_name TEXT NOT NULL,
            risk_score REAL DEFAULT 0.0,
            caller_count INTEGER DEFAULT 0,
            test_coverage TEXT DEFAULT 'unknown',
            security_relevant INTEGER DEFAULT 0,
            last_computed TEXT DEFAULT '',
            FOREIGN KEY (node_id) REFERENCES nodes(id)
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_risk_index_score "
        "ON risk_index(risk_score DESC)"
    )

    # v7 / v8: edge indexes
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_edges_target_kind "
        "ON edges(target_qualified, kind)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_edges_source_kind "
        "ON edges(source_qualified, kind)"
    )
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_edges_composite
        ON edges(kind, source_qualified, target_qualified, file_path, line)
    """)
    logger.info("Migration v1: applied baseline final schema")


# ---------------------------------------------------------------------------
# Migration registry
# ---------------------------------------------------------------------------

MIGRATIONS: dict[int, Callable[[sqlite3.Connection], None]] = {
    1: _migrate_v1,
}

LATEST_VERSION = max(MIGRATIONS.keys())


def run_migrations(conn: sqlite3.Connection) -> None:
    """Run pending migrations in order; each has its own transaction."""
    current = get_schema_version(conn)
    if current >= LATEST_VERSION:
        return

    logger.info("Schema version %d -> %d: running migrations", current, LATEST_VERSION)
    for version in sorted(MIGRATIONS):
        if version <= current:
            continue
        try:
            MIGRATIONS[version](conn)
            _set_schema_version(conn, version)
            conn.commit()
        except sqlite3.Error:
            conn.rollback()
            logger.error("Migration v%d failed, rolling back", version, exc_info=True)
            raise
    logger.info("Migrations complete, now at schema version %d", LATEST_VERSION)

