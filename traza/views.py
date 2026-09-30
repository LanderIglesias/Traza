"""Lo que pinta la pantalla (design.md §7, §7.1), calculado desde la caché. Solo lectura."""
import json
import time
from collections import defaultdict
from datetime import datetime, timezone

from . import db
from .parser import prompt_text
from .pricing import request_cost

# Única constante del autómata (§7): más de 10 min sin tocar ningún fichero de la sesión = no
# está viva, aunque su última línea diga que el modelo tenía el turno (sesión cerrada de golpe).
# Es el mismo umbral que "herramienta colgada" (§7.2).
STALE_AFTER_S = 600
_RANK = {"idle": 0, "done": 0, "thinking": 1, "tool": 2}
# Estados de subagente que dicen que terminó (vistos en disco: toolUseResult.status y
# <status> de task-notification). "async_launched" NO: es el resultado de lanzarlo en segundo
# plano; y un valor desconocido tampoco (mejor "sin terminar" que inventar un fin).
DONE_STATUSES = ("completed", "forked", "failed", "stopped")
TASK_CHARS = 160
# Texto sin stop_reason: en 2.1.284 es una respuesta a medias (los subagentes la escriben
# mientras se genera; 0,4–1,3 s hasta la línea siguiente); en ≤ 2.1.268 podía ser el final (44
# casos). ponytail: umbral fijo; el fin en esas versiones antiguas se ve 30 s tarde.
TEXT_GRACE_S = 30


# Prompts que el modelo no contesta (en disco: 0 de 21 salidas de comando local, 1 de 9
# interrupciones): tras ellos el agente espera al usuario, no "piensa".
UNANSWERED_ORIGINS = frozenset({"interrupted", "local-command"})


def agent_state(last_kind: str | None, tool_in_flight: bool, origin: str | None = None,
                stop_reason: str | None = None, quiet_s: float = float("inf")) -> str:
    """Estado de un agente por su última línea (§7.1). `stop_reason` es el último no nulo de su
    petición; `quiet_s`, segundos desde la última escritura en su fichero."""
    if tool_in_flight:
        return "tool"
    if last_kind == "text":
        if stop_reason == "tool_use":   # en la misma respuesta viene una herramienta
            return "thinking"
        if stop_reason is None and quiet_s < TEXT_GRACE_S:
            return "thinking"           # respuesta aún escribiéndose (subagentes, 2.1.284)
        return "idle"
    if last_kind == "prompt" and origin in UNANSWERED_ORIGINS:
        return "idle"
    return "thinking"  # prompt, tool_result, api_error, compactación…: el turno es del modelo


def session_state(agent_states, idle_for_s: float) -> str:
    """El del agente más activo, salvo que la sesión lleve > STALE_AFTER_S quieta. Un subagente
    terminado no hace que la sesión esté "terminada": solo no trabaja."""
    if idle_for_s > STALE_AFTER_S:
        return "idle"
    st = max(agent_states, key=_RANK.__getitem__, default="idle")
    return "idle" if st == "done" else st


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


def _money(costs) -> dict:
    """Suma de costes (None = sin precio) con los tres casos de §6.6: todo con precio → cost;
    parte → cost + unpriced ("$X+"); nada con precio → cost None ("?"), nunca un 0 engañoso."""
    priced = [c for c in costs if c is not None]
    unpriced = len(costs) - len(priced)
    return {"cost": None if unpriced and not priced else sum(priced), "unpriced": unpriced}


def _iso(mtime_ns: int) -> str:
    return datetime.fromtimestamp(mtime_ns / 1e9, timezone.utc).isoformat(timespec="seconds")


def _agent_states(conn, now: float):
    """({(sesión, agente): estado}, {(sesión, agente): (herramienta, desde)}) — §7.1."""
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
    # El tope de 10 min se aplica POR AGENTE: un subagente interrumpido con una herramienta en
    # vuelo no puede dejar su sesión en "tool" para siempre mientras el principal sigue activo.
    agent_mtime = {(sid, aid): m for sid, aid, m in conn.execute(
        "SELECT session_id, agent_id, mtime_ns FROM files")}
    # Terminado (§7.1): alguien de la sesión recibió su fin (resultado de primer plano o de skill
    # fork, o task-notification de segundo plano). Manda sobre lo demás: un Grep sin resultado
    # de un agente que ya terminó es una herramienta abandonada, no en curso.
    # Primer plano: su toolUseId tiene resultado en el padre, salvo que ese resultado sea el de
    # lanzarlo en segundo plano ("async_launched").
    done = set(conn.execute(f"""
        SELECT a.session_id, a.agent_id FROM agents a WHERE a.agent_id <> 'main' AND (
            EXISTS (SELECT 1 FROM events e WHERE e.session_id = a.session_id
                      AND e.agent_ref = a.agent_id
                      AND e.agent_status IN ({','.join('?' * len(DONE_STATUSES))}))
            OR EXISTS (SELECT 1 FROM events e WHERE e.session_id = a.session_id
                      AND e.kind = 'tool_result' AND e.tool_use_id = a.parent_tool_use_id
                      AND e.agent_status IS NOT 'async_launched'))""", DONE_STATUSES))
    agent_st = {}
    for sid, aid, kind, origin, stop in conn.execute("""
            SELECT e.session_id, e.agent_id, e.kind, e.origin, q.stop_reason
            FROM events e LEFT JOIN requests q USING (request_id)
            WHERE e.id IN (SELECT MAX(id) FROM events WHERE kind <> 'task_notification'
                           GROUP BY session_id, agent_id)"""):
        quiet = now - agent_mtime.get((sid, aid), 0) / 1e9
        stale = quiet > STALE_AFTER_S
        if (sid, aid) in done:
            agent_st[(sid, aid)] = "done"
        else:
            agent_st[(sid, aid)] = "idle" if stale else agent_state(
                kind, (sid, aid) in in_flight, origin, stop, quiet)
    return agent_st, in_flight


def overview(conn, now: float | None = None) -> dict:
    """Todo lo que necesita la pantalla principal en una sola respuesta."""
    now = time.time() if now is None else now
    sessions = {sid: {"id": sid, "project": project, "title": title}
                for sid, project, title in conn.execute(
                    "SELECT session_id, project, title FROM sessions")}
    agents = dict(conn.execute("SELECT session_id, COUNT(*) FROM agents GROUP BY session_id"))
    mtimes = dict(conn.execute("SELECT session_id, MAX(mtime_ns) FROM files GROUP BY session_id"))
    # errores reales vs bloqueos (la herramienta no llegó a ejecutarse: hook, permisos…)
    errors = {sid: (e, b) for sid, e, b in conn.execute("""
        SELECT session_id,
               COALESCE(SUM(kind = 'api_error' OR (is_error = 1 AND denial IS NULL)), 0),
               COALESCE(SUM(is_error = 1 AND denial IS NOT NULL), 0)
        FROM events GROUP BY session_id""")}
    agent_st, in_flight = _agent_states(conn, now)
    agent_types = {(sid, aid): t for sid, aid, t in conn.execute(
        "SELECT session_id, agent_id, agent_type FROM agents")}
    states = defaultdict(list)
    for (sid, _), st in agent_st.items():
        states[sid].append(st)

    # coste: cada petición cuenta una vez, en su sesión dueña (§6.2); None = sin precio
    owner = db.owners(conn)
    reqs = db.requests(conn)
    cost = {r.request_id: request_cost(r) for r in reqs}
    own = defaultdict(list)
    for rid, c in cost.items():
        own[owner.get(rid)].append(c)
    inherited = defaultdict(lambda: defaultdict(list))
    for sid, rid in conn.execute("SELECT DISTINCT session_id, request_id FROM request_refs"):
        o = owner.get(rid)
        if o is not None and o != sid:
            inherited[sid][o].append(cost.get(rid))
    live_sessions = active_agents = 0
    for sid, s in sessions.items():
        idle_for = now - mtimes.get(sid, 0) / 1e9
        s["state"] = session_state(states.get(sid, []), idle_for)
        s["live"] = s["state"] != "idle"
        s["agents"] = agents.get(sid, 0)
        s["errors"], s["blocked"] = errors.get(sid, (0, 0))
        s["last_activity"] = _iso(mtimes.get(sid, 0))
        s.update(_money(own[sid]))
        s["inherits"] = [{"from": o, "requests": len(cs), **_money(cs)}
                         for o, cs in sorted(inherited[sid].items())]
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
            active_agents += sum(st in ("tool", "thinking") for st in states.get(sid, []))

    # "Cost today": peticiones desde la medianoche LOCAL de `now` (cada una una vez, como la ventana)
    midnight = _local_midnight(now)
    today = _money([cost[r.request_id] for r in reqs
                    if (ts := _parse_ts(r.timestamp)) is not None and ts >= midnight])

    ignored = db.ignored_counts(conn)
    return {
        "generation": db.generation(conn),
        "window": _money(list(cost.values())),
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
    s["tree"] = agent_tree(conn, session_id, now)
    return s


def _task(conn, session_id: str, agent_id: str) -> str | None:
    """Principio del primer prompt del agente, leído del JSONL (la caché no guarda texto, §6.2).
    Para los skill forks, que no traen `description`."""
    row = conn.execute("""SELECT file_path, byte_offset, length FROM events
                          WHERE session_id = ? AND agent_id = ? AND kind = 'prompt'
                          ORDER BY id LIMIT 1""", (session_id, agent_id)).fetchone()
    if row is None:
        return None
    try:
        with open(row[0], "rb") as f:
            f.seek(row[1])
            d = json.loads(f.read(row[2]))
        text = prompt_text(d["message"]["content"])
    except (OSError, ValueError, KeyError, TypeError):  # fichero cambiado o línea rara: sin texto
        return None
    return " ".join(text.split())[:TASK_CHARS] or None


def agent_tree(conn, session_id: str, now: float | None = None) -> list[dict]:
    """Nodos del árbol de agentes de una sesión (§7, F4); el frontend los anida por `parent`.
    Coste propio = peticiones de las que este agente es dueño (§6.2: lo heredado de otra sesión
    no cuenta); acumulado = propio + el de todo su subárbol."""
    now = time.time() if now is None else now
    rows = conn.execute("""SELECT a.agent_id, a.agent_type, a.description, f.first_ts
                           FROM agents a LEFT JOIN files f USING (session_id, agent_id)
                           WHERE a.session_id = ?
                           ORDER BY COALESCE(f.first_ts, '9999'), a.agent_id""",
                        (session_id,)).fetchall()
    if not rows:
        return []
    parents = db.agent_parents(conn, session_id)
    orphans = db.orphans(conn, session_id)
    # ponytail: calcula los estados de todas las sesiones para pintar una; ~ms con 26k eventos.
    # Si pesa, filtrar las consultas de _agent_states por sesión.
    states, _ = _agent_states(conn, now)
    signals = {aid: (e, b) for aid, e, b in conn.execute("""
        SELECT agent_id,
               COALESCE(SUM(kind = 'api_error' OR (is_error = 1 AND denial IS NULL)), 0),
               COALESCE(SUM(is_error = 1 AND denial IS NOT NULL), 0)
        FROM events WHERE session_id = ? GROUP BY agent_id""", (session_id,))}
    owner_agent = dict(conn.execute("""SELECT request_id, owner_agent_id FROM request_owner
                                       WHERE owner_session_id = ?""", (session_id,)))
    costs, outputs = defaultdict(list), defaultdict(list)
    for r in db.requests(conn, session_id):
        a = owner_agent[r.request_id]
        costs[a].append(request_cost(r))
        outputs[a].append(r.tokens.output)
    children = defaultdict(list)
    for aid, parent in parents.items():
        if parent is not None and parent != aid:
            children[parent].append(aid)

    def subtree(aid, seen):  # `seen`: un meta.json manipulado no puede crear un ciclo infinito
        acc = list(costs[aid])
        for c in children[aid]:
            if c not in seen:
                seen.add(c)
                acc += subtree(c, seen)
        return acc

    nodes = []
    for aid, agent_type, description, started in rows:
        outs = outputs[aid]
        errors, blocked = signals.get(aid, (0, 0))
        nodes.append({
            "id": aid, "type": agent_type, "description": description,
            "parent": parents.get(aid), "orphan": orphans.get(aid),
            # el principio del encargo solo para skill forks: "padre desconocido" no lo lleva
            "task": _task(conn, session_id, aid) if orphans.get(aid) == "sin_tool_use_id" else None,
            "state": states.get((session_id, aid), "idle"),
            "requests": len(costs[aid]),
            "output": None if None in outs else sum(outs),
            **_money(costs[aid]),
            "total": _money(subtree(aid, {aid})),
            "errors": errors, "blocked": blocked, "started": started,
        })
    return nodes
