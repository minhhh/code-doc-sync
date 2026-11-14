"""Tests for the schema migration framework."""

import sqlite3
import tempfile
from pathlib import Path

from codegraph.graph import GraphStore
from codegraph.graph.migrations import (
    LATEST_VERSION,
    MIGRATIONS,
    get_schema_version,
    run_migrations,
)


class TestMigrations:
    def setup_method(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.store = GraphStore(self.tmp.name)

    def teardown_method(self):
        self.store.close()
        Path(self.tmp.name).unlink(missing_ok=True)

    def test_fresh_db_gets_latest_version(self):
        """A newly created DB should be at the latest schema version."""
        version = get_schema_version(self.store._conn)
        assert version == LATEST_VERSION

    def test_baseline_schema_is_complete(self):
        """The v1 baseline creates every final table in one shot."""
        tables = _get_table_names(self.store._conn)
        for table in (
            "flows", "flow_memberships", "communities", "nodes_fts",
            "community_summaries", "flow_snapshots", "risk_index",
        ):
            assert table in tables

    def test_migration_is_idempotent(self):
        """Opening GraphStore twice should leave schema at latest version."""
        self.store.close()
        self.store = GraphStore(self.tmp.name)
        assert get_schema_version(self.store._conn) == LATEST_VERSION

        self.store.close()
        self.store = GraphStore(self.tmp.name)
        assert get_schema_version(self.store._conn) == LATEST_VERSION

    def test_signature_column_exists_after_migration(self):
        """The nodes table should have a 'signature' column after migration."""
        cursor = self.store._conn.execute("PRAGMA table_info(nodes)")
        columns = [row[1] if isinstance(row, tuple) else row["name"] for row in cursor]
        assert "signature" in columns

    def test_flows_table_exists_after_migration(self):
        """The flows and flow_memberships tables should exist after migration."""
        tables = _get_table_names(self.store._conn)
        assert "flows" in tables
        assert "flow_memberships" in tables

    def test_communities_table_exists_after_migration(self):
        """The communities table should exist and nodes should have community_id."""
        tables = _get_table_names(self.store._conn)
        assert "communities" in tables

        cursor = self.store._conn.execute("PRAGMA table_info(nodes)")
        columns = [row[1] if isinstance(row, tuple) else row["name"] for row in cursor]
        assert "community_id" in columns

    def test_fts5_table_exists_after_migration(self):
        """The nodes_fts FTS5 virtual table should exist after migration."""
        tables = _get_table_names(self.store._conn)
        assert "nodes_fts" in tables

    def test_get_schema_version_no_metadata_table(self):
        """get_schema_version returns 0 when metadata table doesn't exist."""
        conn = sqlite3.connect(":memory:")
        assert get_schema_version(conn) == 0
        conn.close()

    def test_get_schema_version_no_key(self):
        """get_schema_version returns 0 when the baseline has not run yet."""
        conn = sqlite3.connect(":memory:")
        conn.execute(
            "CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        conn.commit()
        assert get_schema_version(conn) == 0
        conn.close()

    def test_migrations_dict_has_single_baseline(self):
        """This project ships one baseline migration, not the historical chain."""
        assert set(MIGRATIONS.keys()) == {1}
        assert LATEST_VERSION == 1

    def test_run_migrations_on_already_current_db(self):
        """run_migrations should be a no-op on an already-current database."""
        version_before = get_schema_version(self.store._conn)
        run_migrations(self.store._conn)
        version_after = get_schema_version(self.store._conn)
        assert version_before == version_after == LATEST_VERSION


    def test_summary_tables_exist(self):
        """Summary tables should exist after the baseline migration."""
        tables = _get_table_names(self.store._conn)
        assert "community_summaries" in tables
        assert "flow_snapshots" in tables
        assert "risk_index" in tables

    def test_baseline_migration_idempotent(self):
        """Running the baseline migration twice should not fail."""
        from codegraph.graph.migrations import _migrate_v1

        _migrate_v1(self.store._conn)
        _migrate_v1(self.store._conn)
        tables = _get_table_names(self.store._conn)
        assert "community_summaries" in tables

    def test_compound_edge_indexes_exist(self):
        """Compound edge indexes should exist after the baseline migration."""
        rows = self.store._conn.execute("PRAGMA index_list(edges)").fetchall()
        indexes = {row[1] if isinstance(row, tuple) else row["name"] for row in rows}

        assert "idx_edges_target_kind" in indexes
        assert "idx_edges_source_kind" in indexes


def _get_table_names(conn: sqlite3.Connection) -> set[str]:
    """Helper: return all table/view names in the database."""
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
    ).fetchall()
    return {row[0] if isinstance(row, (tuple, list)) else row["name"] for row in rows}
