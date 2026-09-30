"""Un tick del watcher (design.md §5): descubre ficheros, lee solo lo nuevo y lo guarda.

Solo lee ~/.claude; escribe únicamente en la caché. Todo un tick es una transacción.
"""
import asyncio
import hashlib
import os
import sqlite3
import traceback
from collections import Counter
from pathlib import Path

from . import db
from .parser import parse_line, parse_meta


def scan(conn, root) -> dict:
    """Sincroniza la caché con los JSONL bajo `root` (~/.claude/projects). Idempotente."""
    root = Path(root).resolve()  # misma ruta guardada venga como venga (relativa, mayúsculas)
    found = {str(p) for p in root.glob("*/*.jsonl")}
    found |= {str(p) for p in root.glob("*/*/subagents/agent-*.jsonl")}
    stats = {"files_read": 0, "lines": 0, "deleted": 0, "truncated": 0, "errors": 0}
    with conn:
        # Cerrojo de escritura ANTES de leer offsets: con dos escáneres a la vez (servidor +
        # `traza.scan`) el segundo espera y luego ve lo que escribió el primero.
        conn.execute("BEGIN IMMEDIATE")
        known = {r[0]: r[1:] for r in conn.execute(
            "SELECT path, size, mtime_ns, offset, head FROM files")}
        pending_meta = set(conn.execute(
            "SELECT session_id, agent_id FROM agents WHERE agent_id <> 'main' AND meta_read = 0"))
        for path in known.keys() - found:
            _forget(conn, path)
            stats["deleted"] += 1
        for path in sorted(found):
            try:
                st = os.stat(path)
            except FileNotFoundError:  # borrado entre el glob y el stat
                if path in known:
                    _forget(conn, path)
                    stats["deleted"] += 1
                continue
            if path not in known:
                _register(conn, Path(path))
                size, mtime, offset, head = 0, 0, 0, None
            else:
                size, mtime, offset, head = known[path]
                if (st.st_size, st.st_mtime_ns) == (size, mtime):
                    _, sid, aid = _ids(Path(path))
                    if (sid, aid) in pending_meta:  # su meta.json puede llegar más tarde
                        _load_meta(conn, path, sid, aid)
                    continue  # sin cambios: ni se abre
            try:
                new_head = _head(path)
                # más corto, o su primera línea ya no es la misma: truncado o sustituido
                if st.st_size < offset or (head and new_head and new_head != head):
                    _clear(conn, path)
                    offset = 0
                    stats["truncated"] += 1
                stats["lines"] += _ingest(conn, path, offset, st.st_mtime_ns, new_head)
            except FileNotFoundError:  # borrado entre el stat y la lectura
                _forget(conn, path)
                stats["deleted"] += 1
                continue
            except OSError:  # bloqueado (antivirus, permisos): se reintenta en el siguiente tick
                stats["errors"] += 1
                continue
            stats["files_read"] += 1
    return stats


def _head(path: str) -> str | None:
    """Huella de la primera línea (o de los primeros 4 KB si es más larga), estable mientras el
    fichero solo crece. None si aún no hay una primera línea completa."""
    with open(path, "rb") as f:
        chunk = f.read(4096)
    nl = chunk.find(b"\n")
    if nl < 0 and len(chunk) < 4096:
        return None
    return hashlib.sha256(chunk[:nl] if nl >= 0 else chunk).hexdigest()[:16]


def _ids(path: Path) -> tuple[str, str, str]:
    """(proyecto, session_id, agent_id) a partir de la ruta."""
    if path.parent.name == "subagents":
        return path.parents[2].name, path.parents[1].name, path.stem.removeprefix("agent-")
    return path.parent.name, path.stem, "main"


def _register(conn, path: Path) -> None:
    project, session_id, agent_id = _ids(path)
    conn.execute("INSERT INTO files (path, session_id, agent_id) VALUES (?, ?, ?)",
                 (str(path), session_id, agent_id))
    conn.execute("INSERT OR IGNORE INTO sessions (session_id, project) VALUES (?, ?)",
                 (session_id, project))
    conn.execute("INSERT OR IGNORE INTO agents (session_id, agent_id) VALUES (?, ?)",
                 (session_id, agent_id))


def _ingest(conn, path: str, offset: int, mtime_ns: int, head: str | None) -> int:
    """Lee desde `offset` solo líneas completas y las guarda. Devuelve cuántas leyó."""
    _, session_id, agent_id = _ids(Path(path))
    with open(path, "rb") as f:
        f.seek(offset)
        data = f.read()
    end = data.rfind(b"\n") + 1  # lo que haya después es una línea a medias: se relee luego
    events, refs, reqs, ignored = [], [], [], Counter()
    title = first_ts = None
    pos = n = 0
    while pos < end:
        nl = data.index(b"\n", pos)
        raw, line_offset = data[pos:nl], offset + pos
        pos = nl + 1
        n += 1
        p = parse_line(raw.decode("utf-8", errors="replace"))
        if p.ignored:
            ignored[p.ignored] += 1
        if p.title:
            title = p.title
        if p.request:
            r, t = p.request, p.request.tokens
            reqs.append((r.request_id, r.timestamp, r.model, *t, r.speed, r.inference_geo,
                         r.web_search_requests, r.stop_reason))
            refs.append((r.request_id, path, session_id, agent_id))
        for e in p.events:
            events.append((path, line_offset, e.block, len(raw), e.uuid, session_id, agent_id,
                           e.timestamp, e.kind, e.tool_name, e.tool_use_id, e.is_error,
                           e.input_hash, e.origin, e.request_id, e.denial,
                           e.agent_ref, e.agent_status, e.chars))
        first_ts = first_ts or p.timestamp  # primera línea con hora, sea del tipo que sea

    # Las líneas de una misma respuesta pueden llegar en ticks distintos (como
    # parser.dedupe_requests): output, el mayor visto (crece); stop_reason, el último no nulo;
    # el resto, de la primera línea.
    conn.executemany("""INSERT INTO requests VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT (request_id) DO UPDATE SET
            output = MAX(COALESCE(output, excluded.output), COALESCE(excluded.output, output)),
            stop_reason = COALESCE(excluded.stop_reason, stop_reason)""", reqs)
    conn.executemany("INSERT OR IGNORE INTO request_refs VALUES (?,?,?,?)", refs)
    conn.executemany("""INSERT OR IGNORE INTO events (file_path, byte_offset, block, length,
        uuid, session_id, agent_id, timestamp, kind, tool_name, tool_use_id, is_error,
        input_hash, origin, request_id, denial, agent_ref, agent_status, chars)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                     events)
    conn.executemany("""INSERT INTO ignored VALUES (?, ?, ?)
        ON CONFLICT (file_path, type) DO UPDATE SET n = n + excluded.n""",
                     [(path, t, c) for t, c in ignored.items()])
    if title:
        conn.execute("UPDATE sessions SET title = ? WHERE session_id = ?", (title, session_id))
    conn.execute("""UPDATE files SET size = ?, mtime_ns = ?, offset = ?,
                    first_ts = COALESCE(first_ts, ?), head = COALESCE(?, head) WHERE path = ?""",
                 (offset + len(data), mtime_ns, offset + end, first_ts, head, path))
    if agent_id != "main":
        _load_meta(conn, path, session_id, agent_id)
    return n


def _load_meta(conn, path: str, session_id: str, agent_id: str) -> None:
    """Lee el meta.json del subagente. Si aún no existe o está a medias, meta_read sigue en 0 y
    se reintenta en el siguiente tick (aunque el .jsonl ya no cambie)."""
    try:
        text = Path(path).with_suffix(".meta.json").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    m = parse_meta(text)
    if m is None:
        return
    conn.execute("""UPDATE agents SET parent_tool_use_id = ?, agent_type = ?, description = ?,
                    spawn_depth = ?, meta_read = 1 WHERE session_id = ? AND agent_id = ?""",
                 (m.parent_tool_use_id, m.agent_type, m.description, m.spawn_depth,
                  session_id, agent_id))


def _clear(conn, path: str) -> None:
    """Borra lo leído de un fichero y lo deja para releer desde 0."""
    for table in ("events", "request_refs", "ignored"):
        conn.execute(f"DELETE FROM {table} WHERE file_path = ?", (path,))
    _, session_id, agent_id = _ids(Path(path))
    if agent_id == "main":  # el título salía de las líneas que ya no están
        conn.execute("UPDATE sessions SET title = NULL WHERE session_id = ?", (session_id,))
    conn.execute("UPDATE files SET size = 0, mtime_ns = 0, offset = 0, first_ts = NULL, "
                 "head = NULL WHERE path = ?", (path,))
    # peticiones que ya nadie referencia; las compartidas pasan solas a otra dueña (vista)
    conn.execute("DELETE FROM requests WHERE request_id NOT IN "
                 "(SELECT request_id FROM request_refs)")


def _forget(conn, path: str) -> None:
    """El fichero ya no existe: fuera todo lo suyo (la caché es espejo del disco, §6.4)."""
    _clear(conn, path)
    session_id, agent_id = conn.execute(
        "SELECT session_id, agent_id FROM files WHERE path = ?", (path,)).fetchone()
    conn.execute("DELETE FROM files WHERE path = ?", (path,))
    conn.execute("DELETE FROM agents WHERE session_id = ? AND agent_id = ?", (session_id, agent_id))
    conn.execute("DELETE FROM sessions WHERE session_id = ? AND NOT EXISTS "
                 "(SELECT 1 FROM files WHERE session_id = ?)", (session_id, session_id))


async def watch(db_path, root, interval: float = 0.5, on_tick=None) -> None:
    """Bucle del watcher para el servidor (design.md §5). Invariante: **el event loop nunca se
    bloquea**. Cada tick corre en un hilo (`asyncio.to_thread`) con una conexión propia; si otro
    proceso tiene el cerrojo de escritura, quien espera es ese hilo, no el servidor."""
    conn = await asyncio.to_thread(db.connect, db_path, check_same_thread=False)
    pending = None
    try:
        while True:
            pending = asyncio.ensure_future(asyncio.to_thread(scan, conn, root))
            try:
                stats = await asyncio.shield(pending)  # si nos cancelan, el hilo termina su tick
            except sqlite3.OperationalError:  # cerrojo ocupado > 30 s: se reintenta luego
                stats = None
            except Exception:  # un fallo inesperado no puede congelar el panel en silencio
                traceback.print_exc()
                stats = None
            if stats and on_tick:
                await on_tick(stats)
            await asyncio.sleep(interval)
    finally:
        if pending is not None and not pending.done():
            await asyncio.wait({pending})  # no cerrar la conexión mientras el hilo la usa
        conn.close()
