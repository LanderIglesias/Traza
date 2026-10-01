"""Señales automáticas (design.md §7.2): funciones puras sobre los eventos de UN agente, en orden.

Una señal es una pista para mirar, no un veredicto: cada una lleva al evento que la causa.
Calibradas contra los datos reales (findings.md §F6), no solo contra la especificación.
"""
from datetime import datetime, timezone

# --- umbrales (un solo bloque, §7.2) ---------------------------------------------------------
LOOP_REPEATS = 3     # la misma llamada (herramienta + hash del input) SEGUIDAS y sin error.
                     # "3 veces en cualquier sitio" daba 90 alertas en disco (relecturas, pytest
                     # repetidos); "seguidas" da 2, las dos bucles de verdad.
HUNG_AFTER_S = 600   # herramienta sin resultado: el mismo umbral que "agente parado" (§7.1)

# Las que cuentan como alerta; el resto se muestra en gris (bloqueo, compactación, reintento
# que funcionó): informan, no piden mirar.
ALERTS = frozenset({"error", "api_error", "loop", "retry_failed", "hung"})


def parse_ts(ts: str | None) -> datetime | None:
    """Timestamp ISO-8601 del JSONL ("…Z") a datetime con zona; None si falta o no se entiende.
    Sin zona se toma como UTC (los JSONL escriben UTC)."""
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _status(result: dict | None) -> str | None:
    if result is None:
        return None
    if result["is_error"]:
        return "blocked" if result["denial"] else "error"
    return "ok"


def agent_signals(events: list[dict], now: float, live_children=frozenset()) -> list[dict]:
    """[{kind, alert, event, tool}] de un agente. `events`: sus eventos en orden (id, kind,
    tool_name, tool_use_id, input_hash, is_error, denial, ts, request_id). `live_children`:
    tool_use_ids que lanzaron un subagente que sigue escribiendo (no está colgado: trabaja)."""
    uses = {e["tool_use_id"]: e for e in events if e["kind"] == "tool_use" and e["tool_use_id"]}
    results = {e["tool_use_id"]: e for e in events
               if e["kind"] == "tool_result" and e["tool_use_id"] in uses}
    pos = {e["id"]: i for i, e in enumerate(events)}
    out = []

    def add(kind, event_id, tool=None):
        out.append({"kind": kind, "alert": kind in ALERTS, "event": event_id, "tool": tool})

    # 1. una señal por evento, y reintentos (error real → siguiente uso de la misma herramienta)
    last = {}   # herramienta → estado de su último uso con resultado
    for e in events:
        if e["kind"] == "api_error":
            add("api_error", e["id"])
        elif e["kind"] == "compact_boundary":
            add("compaction", e["id"])
        elif e["kind"] == "tool_result" and e["tool_use_id"] in uses:
            tool = uses[e["tool_use_id"]]["tool_name"]
            st = _status(e)
            if st in ("error", "blocked"):
                add(st, e["id"], tool)
            # un bloqueo no es un fallo: bloquear y repetir es el flujo normal de un hook
            if last.get(tool) == "error" and st in ("error", "ok"):
                add("retry_failed" if st == "error" else "retry_ok", e["id"], tool)
            last[tool] = st

    # 2. bucle: LOOP_REPEATS llamadas idénticas seguidas, todas sin error
    run_key, run = None, 0
    for e in events:
        if e["kind"] != "tool_use":
            continue
        key = (e["tool_name"], e["input_hash"])
        ok = _status(results.get(e["tool_use_id"])) == "ok"
        run = run + 1 if ok and key == run_key else (1 if ok else 0)
        run_key = key
        if run == LOOP_REPEATS:            # se avisa una vez por racha, en la llamada que la cierra
            add("loop", e["id"], e["tool_name"])

    # 3. colgada: herramienta en vuelo (§7.1: de la ÚLTIMA respuesta, sin resultado y sin prompt
    #    posterior) desde hace más de HUNG_AFTER_S, salvo un subagente que sigue trabajando
    last_req = next((e["request_id"] for e in reversed(events) if e["request_id"]), None)
    last_prompt = max((pos[e["id"]] for e in events if e["kind"] == "prompt"), default=-1)
    for e in events:
        if (e["kind"] == "tool_use" and e["request_id"] == last_req and pos[e["id"]] > last_prompt
                and e["tool_use_id"] not in results and e["tool_use_id"] not in live_children):
            started = parse_ts(e["ts"])
            if started is not None and now - started.timestamp() > HUNG_AFTER_S:
                add("hung", e["id"], e["tool_name"])

    # en el orden en que ocurrieron (estable: un error va antes que su reintento)
    return sorted(out, key=lambda s: pos[s["event"]])
