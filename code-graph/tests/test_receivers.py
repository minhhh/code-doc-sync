"""Tests for typed-receiver resolution of member calls."""

import tempfile
from pathlib import Path

from codegraph.graph import GraphStore
from codegraph.parsing.parser import CodeParser, EdgeInfo, NodeInfo
from codegraph.parsing.receivers import resolve_typed_receivers
from tests.support import parse_path


class TestPythonReceiverMetadata:
    """The parser records the receiver, and its type when one is declared."""

    def setup_method(self):
        self.parser = CodeParser()

    def _call_edges(self, source: Path) -> list[EdgeInfo]:
        _, edges = parse_path(self.parser, source)
        return [e for e in edges if e.kind == "CALLS"]

    def _find(self, edges, method: str) -> EdgeInfo:
        matches = [
            e for e in edges if e.target == method or e.target.endswith(f".{method}")
        ]
        assert len(matches) == 1, [e.target for e in edges]
        return matches[0]

    def test_annotation_supplies_receiver_type(self, tmp_path: Path):
        source = tmp_path / "mod.py"
        source.write_text(
            "class Store:\n"
            "    def commit(self):\n"
            "        pass\n"
            "\n\n"
            "def run(store: Store):\n"
            "    store.commit()\n"
        )

        edge = self._find(self._call_edges(source), "commit")
        assert edge.extra["receiver"] == "store"
        assert edge.extra["receiver_type"] == "Store"

    def test_constructor_local_supplies_receiver_type(self, tmp_path: Path):
        source = tmp_path / "mod.py"
        source.write_text(
            "class Store:\n"
            "    def close(self):\n"
            "        pass\n"
            "\n\n"
            "def run():\n"
            "    s = Store()\n"
            "    s.close()\n"
        )

        edge = self._find(self._call_edges(source), "close")
        assert edge.extra["receiver"] == "s"
        assert edge.extra["receiver_type"] == "Store"

    def test_untyped_receiver_has_no_type(self, tmp_path: Path):
        source = tmp_path / "mod.py"
        source.write_text(
            "def run(conn):\n"
            "    conn.commit()\n"
        )

        edge = self._find(self._call_edges(source), "commit")
        assert edge.extra["receiver"] == "conn"
        assert "receiver_type" not in edge.extra

    def test_attribute_receiver_is_skipped(self, tmp_path: Path):
        source = tmp_path / "mod.py"
        source.write_text(
            "def run(self):\n"
            "    self._conn.commit()\n"
        )

        edge = self._find(self._call_edges(source), "commit")
        assert "receiver" not in edge.extra

    def test_conflicting_rebindings_drop_the_type(self, tmp_path: Path):
        source = tmp_path / "mod.py"
        source.write_text(
            "class A:\n"
            "    pass\n"
            "\n\n"
            "class B:\n"
            "    pass\n"
            "\n\n"
            "def run():\n"
            "    x = A()\n"
            "    x = B()\n"
            "    x.save()\n"
        )

        edge = self._find(self._call_edges(source), "save")
        assert edge.extra["receiver"] == "x"
        assert "receiver_type" not in edge.extra

    def test_generic_annotation_is_ignored(self, tmp_path: Path):
        source = tmp_path / "mod.py"
        source.write_text(
            "from typing import Optional\n"
            "\n\n"
            "def run(store: Optional[object]):\n"
            "    store.commit()\n"
        )

        edge = self._find(self._call_edges(source), "commit")
        assert "receiver_type" not in edge.extra


class TestTypeScriptReceiverMetadata:
    def setup_method(self):
        self.parser = CodeParser()

    def test_typed_parameter_and_new_assignment(self, tmp_path: Path):
        source = tmp_path / "mod.ts"
        source.write_text(
            "class Store {\n"
            "  commit(): void {}\n"
            "  close(): void {}\n"
            "}\n"
            "function run(store: Store): void {\n"
            "  store.commit();\n"
            "}\n"
            "function other(): void {\n"
            "  const s = new Store();\n"
            "  s.close();\n"
            "}\n"
        )

        _, edges = parse_path(self.parser, source)
        by_method = {
            e.target.rsplit(".", 1)[-1]: e.extra
            for e in edges if e.kind == "CALLS"
        }
        assert by_method["commit"]["receiver_type"] == "Store"
        assert by_method["close"]["receiver_type"] == "Store"


class TestResolveTypedReceivers:
    def setup_method(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.store = GraphStore(self.tmp.name)

    def teardown_method(self):
        self.store.close()
        Path(self.tmp.name).unlink(missing_ok=True)

    def _store(self, class_file: str, class_name: str, methods: list[str],
               caller_file: str, calls: list[tuple[str, str, dict]]) -> None:
        nodes = [
            NodeInfo(
                kind="File", name=class_file, file_path=class_file,
                line_start=1, line_end=50, language="python",
            ),
            NodeInfo(
                kind="Class", name=class_name, file_path=class_file,
                line_start=1, line_end=40, language="python",
            ),
        ]
        for method in methods:
            nodes.append(NodeInfo(
                kind="Function", name=method, file_path=class_file,
                line_start=5, line_end=10, language="python",
                parent_name=class_name,
            ))
        self.store.store_file_nodes_edges(class_file, nodes, [])

        caller_nodes = [
            NodeInfo(
                kind="File", name=caller_file, file_path=caller_file,
                line_start=1, line_end=20, language="python",
            ),
            NodeInfo(
                kind="Function", name="run", file_path=caller_file,
                line_start=2, line_end=10, language="python",
            ),
        ]
        edges = [
            EdgeInfo(
                kind="CALLS",
                source=f"{caller_file}::run",
                target=target,
                file_path=caller_file,
                line=3,
                extra=extra,
            )
            for target, _qualified, extra in calls
        ]
        self.store.store_file_nodes_edges(caller_file, caller_nodes, edges)

    def _targets(self) -> dict[str, str]:
        rows = self.store._conn.execute(
            "SELECT target_qualified FROM edges WHERE kind = 'CALLS'"
        ).fetchall()
        out = {}
        for r in rows:
            target = r["target_qualified"]
            out[target.rsplit(".", 1)[-1]] = target
        return out

    def test_resolves_declared_receiver(self):
        self._store(
            "/repo/store.py", "Store", ["commit"], "/repo/cli.py",
            [("commit", "commit", {"receiver": "store",
                                   "receiver_type": "Store"})],
        )

        stats = resolve_typed_receivers(self.store)

        assert stats["resolved"] == 1
        assert self._targets()["commit"] == "/repo/store.py::Store.commit"

    def test_leaves_unknown_receiver_bare(self):
        self._store(
            "/repo/store.py", "Store", ["commit"], "/repo/cli.py",
            [("commit", "commit", {"receiver": "conn"})],
        )

        stats = resolve_typed_receivers(self.store)

        assert stats["resolved"] == 0
        assert self._targets()["commit"] == "commit"

    def test_leaves_missing_method_bare(self):
        self._store(
            "/repo/store.py", "Store", ["commit"], "/repo/cli.py",
            [("execute", "execute", {"receiver": "store",
                                     "receiver_type": "Store"})],
        )

        stats = resolve_typed_receivers(self.store)

        assert stats["no_method"] == 1
        assert self._targets()["execute"] == "execute"

    def test_ambiguous_classes_stay_bare(self):
        self._store(
            "/repo/store.py", "Store", ["commit"], "/repo/cli.py",
            [("commit", "commit", {"receiver": "store",
                                   "receiver_type": "Store"})],
        )
        self._store(
            "/repo/other.py", "Store", ["commit"], "/repo/other_cli.py", [],
        )

        stats = resolve_typed_receivers(self.store)

        assert stats["ambiguous"] == 1
        assert self._targets()["commit"] == "commit"

    def test_is_idempotent(self):
        self._store(
            "/repo/store.py", "Store", ["commit"], "/repo/cli.py",
            [("commit", "commit", {"receiver": "store",
                                   "receiver_type": "Store"})],
        )

        resolve_typed_receivers(self.store)
        stats = resolve_typed_receivers(self.store)

        assert stats["candidates"] == 0
        assert self._targets()["commit"] == "/repo/store.py::Store.commit"
