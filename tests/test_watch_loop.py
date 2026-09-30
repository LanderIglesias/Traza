"""Invariante de F3 (design.md §5): el event loop nunca se bloquea por el watcher, ni siquiera
cuando otro proceso (p. ej. `python -m traza.scan`) tiene el cerrojo de escritura de la BD."""
import asyncio
import shutil
import time
from contextlib import closing
from pathlib import Path

from traza import db
from traza.watcher import watch

FIX = Path(__file__).parent / "fixtures"


def test_event_loop_responde_mientras_otro_tiene_el_cerrojo(tmp_path):
    root = tmp_path / "projects"
    shutil.copytree(FIX, root / "proj")
    path = tmp_path / "traza.db"
    db.connect(path).close()

    async def main():
        blocker = db.connect(path)
        blocker.execute("BEGIN IMMEDIATE")        # "otro proceso" escribiendo
        task = asyncio.create_task(watch(path, root, interval=0.05))
        worst = 0.0
        for _ in range(30):                        # ~0,3 s midiendo cuánto tarda el loop
            t = time.perf_counter()
            await asyncio.sleep(0.01)
            worst = max(worst, time.perf_counter() - t - 0.01)
        blocker.rollback()                         # suelta el cerrojo
        blocker.close()
        for _ in range(100):                       # y el watcher termina su tick
            await asyncio.sleep(0.02)
            with closing(db.connect(path)) as c:
                if c.execute("SELECT COUNT(*) FROM events").fetchone()[0]:
                    break
        task.cancel()
        return worst

    worst = asyncio.run(main())
    assert worst < 0.1, f"el event loop se bloqueó {worst:.2f} s"
    with closing(db.connect(path)) as c:
        assert c.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 24
