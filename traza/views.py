"""Lo que pinta la pantalla (design.md §7, §7.1), calculado desde la caché. Solo lectura."""
import json
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from . import db
from . import signals as sig
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


_parse_ts = sig.parse_ts   # sin zona = UTC; ilegible = None (no tumba el panel)


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
    # Primer plano: su toolUseId tiene resultado en el padre, salvo que sea el de lanzarlo en
    # segundo plano ("async_launched"). Y toda prueba de fin cuenta solo si es POSTERIOR a la
    # última línea del agente: un resultado anterior es un lanzamiento, y líneas nuevas tras un
    # fin son un agente reanudado. ponytail: compara timestamps ISO como texto (todos "…Z").
    done = set(conn.execute(f"""
        SELECT a.session_id, a.agent_id FROM agents a
        JOIN (SELECT session_id, agent_id, MAX(timestamp) AS last_ts FROM events
              WHERE kind <> 'task_notification' GROUP BY session_id, agent_id) l
          USING (session_id, agent_id)
        WHERE a.agent_id <> 'main' AND EXISTS (
            SELECT 1 FROM events e WHERE e.session_id = a.session_id AND e.timestamp >= l.last_ts
              AND ((e.agent_ref = a.agent_id
                    AND e.agent_status IN ({','.join('?' * len(DONE_STATUSES))}))
                   OR (e.kind = 'tool_result' AND e.tool_use_id = a.parent_tool_use_id
                       AND e.agent_status IS NOT 'async_launched')))""", DONE_STATUSES))
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


_SIG_COLS = ("id", "kind", "tool_name", "tool_use_id", "input_hash", "is_error", "denial", "ts",
             "request_id")


def agent_signal_map(conn, now: float, session_id: str | None = None, states=None) -> dict:
    """{(sesión, agente): [señal]} (§7.2), de una sesión o de todas. Un subagente cuyo fichero
    se escribió hace menos de HUNG_AFTER_S está trabajando: el tool_use que lo lanzó no cuenta
    como colgado."""
    where, args = ("WHERE session_id = ?", (session_id,)) if session_id else ("", ())
    by_agent = defaultdict(list)
    for r in conn.execute(f"""SELECT session_id, agent_id, {', '.join(_SIG_COLS[:-2])},
                                     timestamp, request_id
                              FROM events {where} ORDER BY session_id, agent_id, id""", args):
        by_agent[(r[0], r[1])].append(dict(zip(_SIG_COLS, r[2:])))
    live = defaultdict(set)
    for sid, tuid in conn.execute("""SELECT a.session_id, a.parent_tool_use_id FROM agents a
                                     JOIN files f USING (session_id, agent_id)
                                     WHERE a.parent_tool_use_id IS NOT NULL AND f.mtime_ns > ?""",
                                  (int((now - sig.HUNG_AFTER_S) * 1e9),)):
        live[sid].add(tuid)
    agent_st = (states or _agent_states(conn, now))[0]   # "done" manda (§7.1)
    return {k: sig.agent_signals(evs, now, live[k[0]], agent_st.get(k) == "done")
            for k, evs in by_agent.items()}


def _signal_summary(signals: list[dict]) -> dict:
    """Recuentos para una fila o un nodo. errors/blocked salen de las señales: una sola
    definición (F4 los contaba con su propio SQL)."""
    counts = defaultdict(int)
    for x in signals:
        counts[x["kind"]] += 1
    alerts = [x for x in signals if x["alert"]]
    return {"signals": dict(counts), "alerts": len(alerts),
            "alert_event": alerts[-1]["event"] if alerts else None,   # la más reciente
            "errors": counts["error"] + counts["api_error"], "blocked": counts["blocked"]}


def overview(conn, now: float | None = None, states=None, signals=None) -> dict:
    """Todo lo que necesita la pantalla principal en una sola respuesta. `states`: el resultado
    de _agent_states si ya se calculó (session_summary lo reutiliza para el árbol)."""
    now = time.time() if now is None else now
    sessions = {sid: {"id": sid, "project": project, "title": title}
                for sid, project, title in conn.execute(
                    "SELECT session_id, project, title FROM sessions")}
    agents = dict(conn.execute("SELECT session_id, COUNT(*) FROM agents GROUP BY session_id"))
    mtimes = dict(conn.execute("SELECT session_id, MAX(mtime_ns) FROM files GROUP BY session_id"))
    states = states or _agent_states(conn, now)
    agent_st, in_flight = states
    # señales (§7.2): errores reales, bloqueos (la herramienta no llegó a ejecutarse), bucles…
    per_session = defaultdict(list)
    for (sid, _), xs in (signals or agent_signal_map(conn, now, states=states)).items():
        per_session[sid] += xs
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
        s.update(_signal_summary(per_session[sid]))
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
                   "ignored": sum(ignored.values()), "ignored_types": len(ignored),
                   # output_tokens mal escrito por Claude Code: aviso contado (design.md §8)
                   "implausible": len(db.implausible_output(conn)),
                   "ignored_by_type": dict(sorted(ignored.items(), key=lambda kv: (-kv[1], kv[0])))},
        "sessions": sorted(sessions.values(), key=lambda s: s["last_activity"], reverse=True),
    }


def session_summary(conn, session_id: str, now: float | None = None,
                    root=None) -> dict | None:
    """Resumen para el panel de trabajo (§7, clic en una sesión en F3)."""
    now = time.time() if now is None else now
    states = _agent_states(conn, now)   # una vez para la fila y el árbol
    signals = agent_signal_map(conn, now, states=states)
    s = next((s for s in overview(conn, now, states, signals)["sessions"]
              if s["id"] == session_id), None)
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
    s["tree"] = agent_tree(conn, session_id, now, states, root, signals)
    return s


def _first_prompt(conn, session_id: str, agent_id: str) -> int | None:
    row = conn.execute("""SELECT id FROM events WHERE session_id = ? AND agent_id = ?
                          AND kind = 'prompt' ORDER BY id LIMIT 1""",
                       (session_id, agent_id)).fetchone()
    return row[0] if row else None


def _task(conn, session_id: str, agent_id: str, root) -> str | None:
    """Principio del primer prompt del agente (para los skill forks, que no traen description)."""
    eid = _first_prompt(conn, session_id, agent_id)
    got = read_content(conn, [eid], root).get(eid) if eid is not None else None
    if not got or not got["ok"]:
        return None
    return " ".join(got["text"].split())[:TASK_CHARS] or None


def agent_tree(conn, session_id: str, now: float | None = None, states=None,
               root=None, signals=None) -> list[dict]:
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
    # ponytail: calcula los estados de TODAS las sesiones para pintar una (~150 ms con 26k
    # eventos). Si pesa, filtrar las consultas de _agent_states por sesión.
    pair = states or _agent_states(conn, now)       # (estados, herramientas en vuelo)
    states = pair[0]
    signals = signals or agent_signal_map(conn, now, session_id, pair)
    owner_agent = dict(conn.execute("""SELECT request_id, owner_agent_id FROM request_owner
                                       WHERE owner_session_id = ?""", (session_id,)))
    costs, outputs, written, suspect = (defaultdict(list), defaultdict(list), defaultdict(int),
                                        defaultdict(int))
    chars = db.request_chars(conn)
    for r in db.requests(conn, session_id):
        a = owner_agent[r.request_id]
        costs[a].append(request_cost(r))
        outputs[a].append(r.tokens.output)
        written[a] += chars.get(r.request_id, 0)
        suspect[a] += db.implausible(r.tokens.output, chars.get(r.request_id))
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
        nodes.append({
            "id": aid, "type": agent_type, "description": description,
            "parent": parents.get(aid), "orphan": orphans.get(aid),
            # el principio del encargo solo para skill forks: "padre desconocido" no lo lleva
            "task": (_task(conn, session_id, aid, root)
                     if orphans.get(aid) == "sin_tool_use_id" else None),
            "state": states.get((session_id, aid), "idle"),
            "requests": len(costs[aid]),
            "output": None if None in outs else sum(outs),
            # "?" del agente: solo si SU SUMA es implausible (design.md §8); las peticiones
            # sospechosas sueltas se cuentan para el tooltip
            "implausible": db.implausible(None if None in outs else sum(outs), written[aid]),
            "implausible_requests": suspect[aid],
            **_money(costs[aid]),
            "total": _money(subtree(aid, {aid})),
            **_signal_summary(signals.get((session_id, aid), [])), "started": started,
        })
    return nodes


# --- F5: contenido bajo demanda (§6.5) y vista de juicio (§7) ------------------------------------

CONTENT_MAX_CHARS = 20_000   # por bloque: hay salidas de herramienta de varios MB
CONTENT_MAX_IDS = 200        # por petición
TIMELINE_TURNS = 50          # §7: últimos 50 turnos + "cargar anteriores"


def _block_text(b) -> str:
    """Contenido de un tool_result: texto, o bloques de texto; lo demás (imágenes) se nombra."""
    c = b.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "\n".join(x.get("text", "") if x.get("type") == "text" else f"[{x.get('type')}]"
                         for x in c if isinstance(x, dict))
    return ""


def _extract(d: dict, kind: str, block: int) -> str | None:
    """El texto del evento en su línea, o None si la línea ya no es la que se indexó."""
    try:
        if kind == "prompt":
            return prompt_text(d["message"]["content"])
        if kind == "task_notification":
            return d["attachment"]["prompt"]
        if kind == "api_error":
            return str(d.get("error") or "API error")
        if kind == "compact_boundary":
            return str(d.get("content") or "Conversation compacted")
        b = d["message"]["content"][block]
        if b.get("type") != kind:          # el bloque de ese índice no es el que se guardó
            return None
        if kind == "text":
            return b["text"]
        if kind == "tool_use":
            return f"{b.get('name')}\n{json.dumps(b.get('input'), indent=2, ensure_ascii=False)}"
        if kind == "tool_result":
            return _block_text(b)
    except (KeyError, IndexError, TypeError, AttributeError):
        return None
    return None


def read_content(conn, ids, root=None) -> dict[int, dict]:
    """{id: {"ok", "text", "truncated"}} leyendo (fichero, offset, longitud) del JSONL (§6.5).
    Un único estado de error, {"ok": False}: id desconocido, fichero desaparecido, línea cuyo
    uuid o bloque ya no coincide, o ruta fuera de `root` (defensa en profundidad: el servidor
    siempre pasa la raíz de ~/.claude/projects; sin root solo lo usan los tests)."""
    out = {i: {"ok": False} for i in ids}
    if not ids:
        return out
    rows = conn.execute(f"""SELECT id, file_path, byte_offset, length, block, kind, uuid
                            FROM events WHERE id IN ({','.join('?' * len(ids))})""", list(ids))
    by_file = defaultdict(list)
    for r in rows:
        by_file[r[1]].append(r)
    root = Path(root).resolve() if root is not None else None
    for fp, rs in by_file.items():
        try:
            if root is not None and not Path(fp).resolve().is_relative_to(root):
                continue
            with open(fp, "rb") as f:
                for eid, _, offset, length, block, kind, uid in sorted(rs, key=lambda r: r[2]):
                    f.seek(offset)
                    try:
                        d = json.loads(f.read(length))
                    except ValueError:
                        continue
                    if uid is None or not isinstance(d, dict) or d.get("uuid") != uid:
                        continue
                    text = _extract(d, kind, block)
                    if isinstance(text, str):
                        out[eid] = {"ok": True, "text": text[:CONTENT_MAX_CHARS],
                                    "truncated": len(text) > CONTENT_MAX_CHARS}
        except OSError:
            continue
    return out


def agent_view(conn, session_id: str, agent_id: str, root=None, now: float | None = None,
               limit: int = TIMELINE_TURNS, before: int | None = None,
               start_at: int | None = None) -> dict | None:
    """Vista de juicio (§7): encargo, timeline por turnos y resultado devuelto al padre. Solo
    metadatos: el texto se pide aparte con read_content (§6.5).

    Turno = una petición del modelo (su texto y sus tool_use) con el tool_result de cada
    herramienta emparejado. Prompts y eventos de sistema van como elementos propios."""
    now = time.time() if now is None else now
    states = _agent_states(conn, now)
    signals = agent_signal_map(conn, now, session_id, states)
    node = next((n for n in agent_tree(conn, session_id, now, states=states, root=root,
                                       signals=signals)
                 if n["id"] == agent_id), None)
    if node is None:
        return None
    task = _first_prompt(conn, session_id, agent_id) if agent_id != "main" else None
    rows = conn.execute("""SELECT id, kind, request_id, tool_name, tool_use_id, is_error, denial,
                                  timestamp, origin FROM events
                           WHERE session_id = ? AND agent_id = ? ORDER BY id""",
                        (session_id, agent_id)).fetchall()
    rids = {r[2] for r in rows if r[2]}
    reqs = {r.request_id: r for r in db.requests(conn) if r.request_id in rids}
    suspect = db.implausible_output(conn) & rids

    items, turns, uses = [], {}, {}
    for eid, kind, rid, tool, tuid, is_error, denial, ts, origin in rows:
        if eid == task:
            continue
        ev = {"id": eid, "kind": kind, "tool_name": tool, "ts": ts}
        if kind in ("text", "tool_use"):
            key = rid or f"e{eid}"          # texto sin requestId (p. ej. <synthetic>): su turno
            if key not in turns:
                r = reqs.get(rid)
                turns[key] = {"kind": "turn", "request_id": rid, "ts": ts, "events": [],
                              "model": r.model if r else None,
                              "output": r.tokens.output if r else None,
                              "cost": request_cost(r) if r else None,
                              "implausible": rid in suspect}
                items.append(turns[key])
            turns[key]["events"].append(ev)
            if kind == "tool_use" and tuid:
                uses[tuid] = ev
        elif kind == "tool_result":
            res = {"id": eid, "is_error": bool(is_error), "denial": denial, "ts": ts}
            use = uses.get(tuid)
            if use is not None and "result" not in use:
                use["result"] = res
            else:                            # su tool_use no está en este agente
                items.append({"kind": "system", "event": {**ev, **res}})
        elif kind == "prompt":
            items.append({"kind": "prompt", "event": {**ev, "origin": origin}})
        else:                                # api_error, compact_boundary, notificación, unknown
            items.append({"kind": "system", "event": ev})

    # señales de cada turno: las de cualquiera de sus eventos o de sus resultados
    by_event = defaultdict(list)
    for x in signals.get((session_id, agent_id), []):
        by_event[x["event"]].append({"kind": x["kind"], "alert": x["alert"], "event": x["event"]})
    for it in items:
        evs = it["events"] if it["kind"] == "turn" else [it["event"]]
        ids = [e["id"] for e in evs] + [e["result"]["id"] for e in evs if e.get("result")]
        it["signals"] = [x for i in ids for x in by_event.get(i, [])]

    # los últimos `limit` turnos antes de `before`, con lo que haya entre ellos
    end = len(items) if before is None else max(0, min(before, len(items)))
    turn_at = [i for i, it in enumerate(items[:end]) if it["kind"] == "turn"]
    start = turn_at[-limit] if len(turn_at) > limit else 0
    if start_at is not None:                 # desde ahí hasta el final (refresco tras cargar más)
        start, end = max(0, min(start_at, len(items))), len(items)

    result = None
    if agent_id != "main" and node["state"] == "done":
        # primer plano: lo que recibió el padre; segundo plano, skill fork o agente reanudado
        # después de entregar (el resultado del padre ya no es lo último que hizo): su último
        # texto. Misma regla temporal que "done" (§7.1).
        parent = conn.execute("""SELECT e.id, e.agent_status, e.timestamp FROM events e
                                 JOIN agents a ON a.session_id = e.session_id
                                  AND a.parent_tool_use_id = e.tool_use_id
                                 WHERE a.session_id = ? AND a.agent_id = ?
                                   AND e.kind = 'tool_result' ORDER BY e.id LIMIT 1""",
                              (session_id, agent_id)).fetchone()
        last_ts = conn.execute("""SELECT MAX(timestamp) FROM events WHERE session_id = ?
                                  AND agent_id = ? AND kind <> 'task_notification'""",
                               (session_id, agent_id)).fetchone()[0]
        if parent and parent[1] != "async_launched" and (parent[2] or "") >= (last_ts or ""):
            result = {"id": parent[0], "source": "parent_result"}
        else:
            last = conn.execute("""SELECT MAX(id) FROM events WHERE session_id = ?
                                   AND agent_id = ? AND kind = 'text'""",
                                (session_id, agent_id)).fetchone()[0]
            result = {"id": last, "source": "final_text"} if last else None
    return {"agent": node, "task": task, "result": result,
            "items": items[start:end], "start": start, "total": len(items)}
