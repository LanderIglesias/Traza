"""Comando de verificación de F2: python -m traza.scan [--db RUTA] [--root RUTA]

Construye (o actualiza) la caché desde todos los JSONL y muestra qué hay dentro.
Por defecto: caché en ~/.traza/traza.db, datos en ~/.claude/projects (solo lectura).
"""
import argparse
import time
from pathlib import Path

from . import db
from .watcher import scan


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="traza scan")
    ap.add_argument("--db", type=Path, default=Path.home() / ".traza" / "traza.db")
    ap.add_argument("--root", type=Path, default=Path.home() / ".claude" / "projects")
    args = ap.parse_args(argv)

    conn = db.connect(args.db)
    t0 = time.perf_counter()
    stats = scan(conn, args.root)
    elapsed = time.perf_counter() - t0

    def one(sql):
        return conn.execute(sql).fetchone()[0]
    ignored = db.ignored_counts(conn)
    print(f"cache {args.db}  (generation {db.generation(conn)[:8]})")
    print(f"read {stats['files_read']} files, {stats['lines']} lines "
          f"({stats['deleted']} deleted, {stats['truncated']} truncated) in {elapsed:.2f} s")
    print(f"sessions {one('SELECT COUNT(*) FROM sessions')} · "
          f"agents {one('SELECT COUNT(*) FROM agents')} · "
          f"requests {one('SELECT COUNT(*) FROM requests')} · "
          f"events {one('SELECT COUNT(*) FROM events')}")
    unknown = one("SELECT COUNT(*) FROM events WHERE kind = 'unknown'")
    print(f"unknown {unknown} · "
          f"ignored on purpose {sum(ignored.values())} {dict(sorted(ignored.items(), key=lambda kv: -kv[1]))}")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
