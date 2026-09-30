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
    ap = argparse.ArgumentParser(prog="python -m traza.scan")
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
    print(f"caché {args.db}  (generación {db.generation(conn)[:8]})")
    print(f"tick: {stats['files_read']} ficheros leídos, {stats['lines']} líneas, "
          f"{stats['deleted']} borrados, {stats['truncated']} truncados en {elapsed:.2f} s")
    print(f"sesiones {one('SELECT COUNT(*) FROM sessions')} · "
          f"agentes {one('SELECT COUNT(*) FROM agents')} · "
          f"peticiones {one('SELECT COUNT(*) FROM requests')} · "
          f"eventos {one('SELECT COUNT(*) FROM events')}")
    unknown = one("SELECT COUNT(*) FROM events WHERE kind = 'unknown'")
    print(f"unknown {unknown} · "
          f"ignorados {sum(ignored.values())} {dict(sorted(ignored.items(), key=lambda kv: -kv[1]))}")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
