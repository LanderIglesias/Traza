"""Caché SQLite desechable (design.md §6.2 y §6.4). La fuente de verdad son los JSONL."""
import sqlite3
import uuid
from pathlib import Path

from .parser import Request, Tokens

# Subir cualquiera de los dos borra y reconstruye la BD: no hay migraciones (§6.4).
PARSER_VERSION = "11"
SCHEMA_VERSION = "5"

SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE files (
    path TEXT PRIMARY KEY, session_id TEXT NOT NULL, agent_id TEXT NOT NULL,
    size INTEGER NOT NULL DEFAULT 0, mtime_ns INTEGER NOT NULL DEFAULT 0,
    offset INTEGER NOT NULL DEFAULT 0,
    first_ts TEXT,            -- timestamp de la primera línea que lo trae: inicio de la sesión
    head TEXT                 -- hash de la primera línea: si cambia, el fichero fue sustituido
);
CREATE TABLE sessions (session_id TEXT PRIMARY KEY, project TEXT, title TEXT);
CREATE TABLE agents (
    session_id TEXT NOT NULL, agent_id TEXT NOT NULL,
    parent_tool_use_id TEXT, agent_type TEXT, description TEXT, spawn_depth INTEGER,
    meta_read INTEGER NOT NULL DEFAULT 0,  -- 1 cuando su meta.json se leyó completo
    PRIMARY KEY (session_id, agent_id)
);
CREATE TABLE requests (
    request_id TEXT PRIMARY KEY, timestamp TEXT, model TEXT,
    input INTEGER, output INTEGER, cache_read INTEGER, cache_write_5m INTEGER,
    cache_write_1h INTEGER, speed TEXT, inference_geo TEXT, web_search_requests INTEGER,
    stop_reason TEXT
);
CREATE TABLE request_refs (
    request_id TEXT NOT NULL, file_path TEXT NOT NULL, session_id TEXT NOT NULL,
    agent_id TEXT NOT NULL, PRIMARY KEY (request_id, file_path)
);
CREATE TABLE ignored (
    file_path TEXT NOT NULL, type TEXT NOT NULL, n INTEGER NOT NULL,
    PRIMARY KEY (file_path, type)
);
CREATE TABLE events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_path TEXT NOT NULL, byte_offset INTEGER NOT NULL, block INTEGER NOT NULL,
    length INTEGER NOT NULL, uuid TEXT, session_id TEXT NOT NULL, agent_id TEXT NOT NULL,
    timestamp TEXT, kind TEXT NOT NULL, tool_name TEXT, tool_use_id TEXT, is_error INTEGER,
    input_hash TEXT, origin TEXT, request_id TEXT,
    denial TEXT,              -- toolDenialKind: la herramienta no se ejecutó (hook, permisos…)
    agent_ref TEXT, agent_status TEXT,  -- subagente lanzado/notificado y su estado (§7.1)
    UNIQUE (file_path, byte_offset, block)
);
CREATE INDEX events_agent ON events (session_id, agent_id, id);
CREATE INDEX events_tool_use ON events (tool_use_id);
CREATE INDEX events_uuid ON events (uuid);
CREATE INDEX events_request ON events (request_id);
CREATE INDEX events_agent_ref ON events (agent_ref);
CREATE INDEX request_refs_file ON request_refs (file_path);
CREATE INDEX request_refs_session ON request_refs (session_id);

-- Dueña de cada petición = la sesión que empezó antes (§6.2). Calculada, nunca guardada:
-- no depende del orden de ingesta y al borrar una sesión la siguiente hereda sola.
CREATE VIEW request_owner AS
SELECT request_id, session_id AS owner_session_id, agent_id AS owner_agent_id FROM (
    SELECT r.request_id, r.session_id, r.agent_id, ROW_NUMBER() OVER (
        PARTITION BY r.request_id
        ORDER BY COALESCE(f.first_ts, '9999'), r.session_id, r.agent_id) AS rn
    FROM request_refs r
    LEFT JOIN files f ON f.session_id = r.session_id AND f.agent_id = 'main'
) WHERE rn = 1;
"""


def connect(path, check_same_thread: bool = True) -> sqlite3.Connection:
    """Abre la caché; si no existe o es de otra versión, la crea de cero."""
    path = Path(path)
    if path.exists():
        meta = _cache_meta(path)
        if meta is None:  # no es una caché de traza: jamás se borra lo ajeno
            raise RuntimeError(f"{path} existe y no es una caché de traza; elige otra ruta")
        if (meta.get("parser_version"), meta.get("schema_version")) != (
                PARSER_VERSION, SCHEMA_VERSION):
            try:
                for p in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
                    p.unlink(missing_ok=True)
            except PermissionError:  # Windows: otro proceso (otro traza) la tiene abierta
                raise RuntimeError(f"{path} está en uso por otro proceso de traza con otra "
                                   "versión; ciérralo y vuelve a intentarlo") from None
    fresh = not path.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    # timeout: otro escritor (servidor + `traza.scan`) espera hasta 30 s en vez de fallar a los 5
    conn = sqlite3.connect(path, timeout=30, check_same_thread=check_same_thread)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    if fresh:  # todo o nada: un Ctrl+C a medias deja una BD vacía, que se puede reconstruir
        conn.executescript(
            "BEGIN;" + SCHEMA + "INSERT INTO meta VALUES "
            f"('cache_generation', '{uuid.uuid4().hex}'), "
            f"('parser_version', '{PARSER_VERSION}'), ('schema_version', '{SCHEMA_VERSION}');"
            "COMMIT;")
    return conn


def _cache_meta(path: Path) -> dict | None:
    """La tabla meta si `path` es una caché de traza; {} si es una BD SQLite vacía (una
    creación interrumpida: se puede reconstruir); None si es cualquier otra cosa."""
    try:
        c = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)  # solo lectura
        try:
            if c.execute("SELECT 1 FROM sqlite_master").fetchone() is None:
                return {}
            meta = dict(c.execute("SELECT key, value FROM meta"))
        finally:
            c.close()
    except sqlite3.DatabaseError:
        return None
    return meta if "cache_generation" in meta else None


def generation(conn) -> str:
    return conn.execute("SELECT value FROM meta WHERE key = 'cache_generation'").fetchone()[0]


def requests(conn, owner_session_id: str | None = None) -> list[Request]:
    """Peticiones (una por requestId). Con owner_session_id, solo las que son de esa sesión."""
    sql = """SELECT q.request_id, q.model, q.timestamp, q.input, q.output, q.cache_read,
                    q.cache_write_5m, q.cache_write_1h, q.speed, q.inference_geo,
                    q.web_search_requests, q.stop_reason
             FROM requests q"""
    args = ()
    if owner_session_id is not None:
        sql += " JOIN request_owner o USING (request_id) WHERE o.owner_session_id = ?"
        args = (owner_session_id,)
    return [Request(r[0], r[1], r[2], Tokens(*r[3:8]), *r[8:]) for r in conn.execute(sql, args)]


def owners(conn) -> dict[str, str]:
    return dict(conn.execute("SELECT request_id, owner_session_id FROM request_owner"))


def inherited(conn, session_id: str) -> dict[str, int]:
    """{sesión dueña: nº de peticiones} que `session_id` contiene pero son de otra (§6.2)."""
    return dict(conn.execute("""
        SELECT o.owner_session_id, COUNT(*)
        FROM (SELECT DISTINCT request_id FROM request_refs WHERE session_id = ?) r
        JOIN request_owner o USING (request_id)
        WHERE o.owner_session_id <> ?
        GROUP BY o.owner_session_id""", (session_id, session_id)))


def agent_parents(conn, session_id: str) -> dict[str, str | None]:
    """{agent_id: agente padre}. Padre = quien tiene el tool_use que lo lanzó; None = raíz o
    huérfano (§6.1). Se calcula al consultar, así que no importa qué fichero se leyó antes."""
    return dict(conn.execute("""
        SELECT a.agent_id, (SELECT e.agent_id FROM events e
                            WHERE e.session_id = a.session_id AND e.kind = 'tool_use'
                              AND e.tool_use_id = a.parent_tool_use_id LIMIT 1)
        FROM agents a WHERE a.session_id = ?""", (session_id,)))


def orphans(conn, session_id: str) -> dict[str, str]:
    """{agent_id: motivo} de los subagentes sin padre. Dos clases distintas (§6.1):
    - "sin_tool_use_id": su meta.json no dice quién lo lanzó; nunca tendrá padre (en disco:
      subagentes de skills en modo fork, p. ej. /code-review).
    - "padre_no_encontrado": dice quién lo lanzó pero ese tool_use no está (aún) en la sesión,
      o su meta.json aún no se ha podido leer (puede llegar más tarde)."""
    return {a: ("sin_tool_use_id" if tid is None and read else "padre_no_encontrado")
            for a, tid, read, parent in conn.execute("""
                SELECT a.agent_id, a.parent_tool_use_id, a.meta_read,
                       (SELECT 1 FROM events e WHERE e.session_id = a.session_id
                          AND e.kind = 'tool_use' AND e.tool_use_id = a.parent_tool_use_id)
                FROM agents a WHERE a.session_id = ? AND a.agent_id <> 'main'""", (session_id,))
            if parent is None}


def ignored_counts(conn) -> dict[str, int]:
    return dict(conn.execute("SELECT type, SUM(n) FROM ignored GROUP BY type"))
