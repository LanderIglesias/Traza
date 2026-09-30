"""Lo que pinta la pantalla (design.md §7, §7.1), calculado desde la caché. Solo lectura."""
import time
from collections import defaultdict
from datetime import datetime, timezone

from . import db
from .pricing import request_cost

# Única constante del autómata (§7): más de 10 min sin tocar ningún fichero de la sesión = no
# está viva, aunque su última línea diga que el modelo tenía el turno (sesión cerrada de golpe).
# Es el mismo umbral que "herramienta colgada" (§7.2).
STALE_AFTER_S = 600
_RANK = {"idle": 0, "thinking": 1, "tool": 2}


# Prompts que el modelo no contesta (en disco: 0 de 21 salidas de comando local, 1 de 9
# interrupciones): tras ellos el agente espera al usuario, no "piensa".
UNANSWERED_ORIGINS = frozenset({"interrupted", "local-command"})


def agent_state(last_kind: str | None, tool_in_flight: bool, origin: str | None = None) -> str:
    """Estado de un agente por su última línea (§7.1). No usa stop_reason: las versiones
    2.1.24–2.1.27 lo escriben null incluso en la respuesta final (findings.md §F3)."""
    if tool_in_flight:
        return "tool"
    if last_kind == "text":  # el modelo terminó con texto: si quisiera seguir, pediría herramienta
        return "idle"
    if last_kind == "prompt" and origin in UNANSWERED_ORIGINS:
        return "idle"
    return "thinking"  # prompt, tool_result, api_error, compactación…: el turno es del modelo


def session_state(agent_states, idle_for_s: float) -> str:
    """El del agente más activo, salvo que la sesión lleve > STALE_AFTER_S quieta."""
    if idle_for_s > STALE_AFTER_S:
        return "idle"
    return max(agent_states, key=_RANK.__getitem__, default="idle")


def _parse_ts(ts: str | None) -> datetime | None:
    """Timestamp ISO-8601 del JSONL ("…Z") a datetime con zona; None si falta o no se entiende.
    Sin zona se toma como UTC (los JSONL escriben UTC): comparar uno sin zona con uno con zona
    lanzaría TypeError y tumbaría /api/overview entero."""
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _local_midnight(now: float, tz=None) -> datetime:
    """Medianoche local del día de `now`, con el desfase de ESA hora (no el de ahora): en un día
    de cambio de hora, medianoche y mediodía tienen desfases distintos."""
    if tz is not None:
        return datetime.fromtimestamp(now, tz).replace(hour=0, minute=0, second=0, microsecond=0)
    return datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0).astimezone()


def _iso(mtime_ns: int) -> str:
    return datetime.fromtimestamp(mtime_ns / 1e9, timezone.utc).isoformat(timespec="seconds")


def overview(conn, now: float | None = None) -> dict:
    """Todo lo que necesita la pantalla principal en una sola respuesta."""
    now = time.time() if now is None else now
    sessions = {sid: {"id": sid, "project": project, "title": title}
                for sid, project, title in conn.execute(
                    "SELECT session_id, project, title FROM sessions")}
    agents = dict(conn.execute("SELECT session_id, COUNT(*) FROM agents GROUP BY session_id"))
    mtimes = dict(conn.execute("SELECT session_id, MAX(mtime_ns) FROM files GROUP BY session_id"))
    errors = dict(conn.execute("""SELECT session_id, SUM(kind = 'api_error' OR is_error = 1)
                                  FROM events GROUP BY session_id"""))
    # Herramienta en vuelo (§7.1): pedida en la ÚLTIMA respuesta del agente, sin resultado y sin
    # prompt posterior. Las de respuestas anteriores ya acabaron o se abandonaron (el modelo no
    # vuelve a responder hasta tener sus resultados); contarlas dejaría una sesión en "tool"
    # para siempre por una herramienta interrumpida hace días.
    in_flight = {}
    for sid, aid, tool, ts in conn.execute("""
        WITH last_req AS (
            SELECT session_id, agent_id, request_id FROM events WHERE id IN (
                SELECT MAX(id) FROM events WHERE request_id IS NOT NULL
                GROUP BY session_id, agent_id)),
        last_prompt AS (
            SELECT session_id, agent_id, MAX(id) AS id FROM events WHERE kind = 'prompt'
            GROUP BY session_id, agent_id)
        SELECT e.session_id, e.agent_id, e.tool_name, e.timestamp FROM events e
        JOIN last_req l ON l.session_id = e.session_id AND l.agent_id = e.agent_id
                       AND l.request_id = e.request_id
        LEFT JOIN last_prompt p ON p.session_id = e.session_id AND p.agent_id = e.agent_id
        WHERE e.kind = 'tool_use' AND e.id > COALESCE(p.id, 0) AND NOT EXISTS (
            SELECT 1 FROM events r WHERE r.kind = 'tool_result'
              AND r.tool_use_id = e.tool_use_id AND r.session_id = e.session_id)
        ORDER BY e.id"""):
        in_flight[(sid, aid)] = (tool, ts)  # si hay varias en paralelo, gana la última pedida
    agent_types = {(sid, aid): t for sid, aid, t in conn.execute(
        "SELECT session_id, agent_id, agent_type FROM agents")}
    # El tope de 10 min se aplica POR AGENTE: un subagente interrumpido con una herramienta en
    # vuelo no puede dejar su sesión en "tool" para siempre mientras el principal sigue activo.
    agent_mtime = {(sid, aid): m for sid, aid, m in conn.execute(
        "SELECT session_id, agent_id, mtime_ns FROM files")}
    agent_st = {}
    for sid, aid, kind, origin in conn.execute("""
            SELECT session_id, agent_id, kind, origin FROM events
            WHERE id IN (SELECT MAX(id) FROM events GROUP BY session_id, agent_id)"""):
        stale = now - agent_mtime.get((sid, aid), 0) / 1e9 > STALE_AFTER_S
        agent_st[(sid, aid)] = "idle" if stale else agent_state(kind, (sid, aid) in in_flight, origin)
    states = defaultdict(list)
    for (sid, _), st in agent_st.items():
        states[sid].append(st)

    # coste: cada petición cuenta una vez, en su sesión dueña (§6.2); None = sin precio
    owner = db.owners(conn)
    reqs = db.requests(conn)
    cost = {r.request_id: request_cost(r) for r in reqs}
    for s in sessions.values():
        s.update(cost=0.0, unpriced=0, inherits=[])
    for rid, c in cost.items():
        s = sessions.get(owner.get(rid))
        if s is not None:
            if c is None:
                s["unpriced"] += 1
            else:
                s["cost"] += c
    inherited = defaultdict(lambda: defaultdict(lambda: [0, 0.0]))
    for sid, rid in conn.execute("SELECT DISTINCT session_id, request_id FROM request_refs"):
        own = owner.get(rid)
        if own is not None and own != sid:
            acc = inherited[sid][own]
            acc[0] += 1
            acc[1] += cost.get(rid) or 0.0
    live_sessions = active_agents = 0
    for sid, s in sessions.items():
        idle_for = now - mtimes.get(sid, 0) / 1e9
        s["state"] = session_state(states.get(sid, []), idle_for)
        s["live"] = s["state"] != "idle"
        s["agents"] = agents.get(sid, 0)
        s["errors"] = errors.get(sid) or 0
        s["last_activity"] = _iso(mtimes.get(sid, 0))
        s["inherits"] = [{"from": o, "requests": n, "cost": c}
                         for o, (n, c) in sorted(inherited[sid].items())]
        running = [(ts or "", aid, tool) for (s_id, aid), (tool, ts) in in_flight.items()
                   if s_id == sid and agent_st.get((s_id, aid)) == "tool"]
        if s["state"] == "tool" and running:
            ts, aid, tool = max(running)  # la herramienta más reciente de la sesión
            s["activity"] = {"agent": agent_types.get((sid, aid)) or aid, "tool": tool,
                             "since": ts or None}
        else:
            s["activity"] = None
        if s["live"]:
            live_sessions += 1
            active_agents += sum(st != "idle" for st in states.get(sid, []))

    # "Cost today": peticiones desde la medianoche LOCAL de `now` (cada una una vez, como la ventana)
    midnight = _local_midnight(now)
    today = {"cost": 0, "unpriced": 0}
    for r in reqs:
        ts = _parse_ts(r.timestamp)
        if ts is not None and ts >= midnight:
            c = cost[r.request_id]
            if c is None:
                today["unpriced"] += 1
            else:
                today["cost"] += c

    ignored = db.ignored_counts(conn)
    priced = [c for c in cost.values() if c is not None]
    return {
        "generation": db.generation(conn),
        "window": {"cost": sum(priced), "unpriced": len(cost) - len(priced)},
        "today": today,
        "counts": {"live_sessions": live_sessions, "active_agents": active_agents,
                   "sessions": len(sessions)},
        "health": {"unknown": conn.execute(
                       "SELECT COUNT(*) FROM events WHERE kind = 'unknown'").fetchone()[0],
                   "ignored": sum(ignored.values()), "ignored_types": len(ignored)},
        "sessions": sorted(sessions.values(), key=lambda s: s["last_activity"], reverse=True),
    }


def session_summary(conn, session_id: str, now: float | None = None) -> dict | None:
    """Resumen para el panel de trabajo (§7, clic en una sesión en F3)."""
    s = next((s for s in overview(conn, now)["sessions"] if s["id"] == session_id), None)
    if s is None:
        return None
    s["models"] = dict(conn.execute("""
        SELECT q.model, COUNT(*) FROM requests q JOIN request_owner o USING (request_id)
        WHERE o.owner_session_id = ? GROUP BY q.model ORDER BY COUNT(*) DESC""", (session_id,)))
    s["requests"] = sum(s["models"].values())
    s["started"] = conn.execute(
        "SELECT first_ts FROM files WHERE session_id = ? AND agent_id = 'main'",
        (session_id,)).fetchone()
    s["started"] = s["started"][0] if s["started"] else None
    return s
