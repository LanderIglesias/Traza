"""Pestaña Flame: los tests del layout son JS (tests/flame.test.mjs, `node --test`). Este los
lanza con el árbol real de la fixture (el que sirve /api/sessions/{id}), así `pytest` sigue
siendo el único comando."""
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from traza import db, views
from traza.watcher import scan

FIX = Path(__file__).parent / "fixtures"
NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="node no está instalado")
def test_layout_del_flame(tmp_path):
    root = tmp_path / "projects"
    shutil.copytree(FIX, root / "proj")
    conn = db.connect(tmp_path / "traza.db")
    scan(conn, root)
    tree = views.session_summary(conn, "sess-A", root=root)["tree"]
    (tmp_path / "tree.json").write_text(json.dumps(tree), encoding="utf-8")
    r = subprocess.run([NODE, "--test", str(Path(__file__).parent / "flame.test.mjs")],
                       capture_output=True, text=True, timeout=60,
                       env={**os.environ, "FLAME_TREE": str(tmp_path / "tree.json")})
    assert r.returncode == 0, r.stdout + r.stderr
    assert "skipped 0" in r.stdout and "fail 0" in r.stdout   # el test del árbol real sí corrió
