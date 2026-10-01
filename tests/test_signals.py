"""F6: señales automáticas (design.md §7.2). Funciones puras sobre los eventos de un agente."""
from datetime import datetime, timezone

from traza import signals

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc).timestamp()
_ids = iter(range(1, 10_000))


def ts(minutes_ago: float) -> str:
    return datetime.fromtimestamp(NOW - minutes_ago * 60, timezone.utc).isoformat().replace(
        "+00:00", "Z")


def call(tool, args="a", result="ok", ago=30, req=None):
    """Un tool_use y (si result no es None) su tool_result: "ok", "error" o "blocked"."""
    uid, tid = next(_ids), f"t{next(_ids)}"
    use = {"id": uid, "kind": "tool_use", "tool_name": tool, "tool_use_id": tid,
           "input_hash": f"h-{tool}-{args}", "is_error": None, "denial": None,
           "ts": ts(ago), "request_id": req or f"r{uid}"}
    if result is None:
        return [use]
    res = {"id": next(_ids), "kind": "tool_result", "tool_name": None, "tool_use_id": tid,
           "input_hash": None, "is_error": result != "ok",
           "denial": "permission-rule" if result == "blocked" else None,
           "ts": ts(ago), "request_id": None}
    return [use, res]


def ev(kind, ago=30, **kw):
    return {"id": next(_ids), "kind": kind, "tool_name": None, "tool_use_id": None,
            "input_hash": None, "is_error": None, "denial": None, "ts": ts(ago),
            "request_id": None, **kw}


def kinds(events, **kw):
    return [(s["kind"], s["alert"]) for s in signals.agent_signals(events, NOW, **kw)]


# --- una señal por evento --------------------------------------------------------------------

def test_error_bloqueo_api_error_y_compactacion():
    events = [*call("Bash", result="error"), *call("Edit", result="blocked"),
              ev("api_error"), ev("compact_boundary")]
    assert kinds(events) == [("error", True), ("blocked", False), ("api_error", True),
                             ("compaction", False)]


def test_la_senal_apunta_al_evento_que_la_causa():
    use, res = call("Bash", result="error")
    (s,) = signals.agent_signals([use, res], NOW)
    assert s["event"] == res["id"] and s["tool"] == "Bash"


# --- bucle: misma llamada 3 veces SEGUIDAS, sin error ------------------------------------------
# En disco, "3 veces en cualquier sitio" daba 90 alertas (relecturas, pytest repetidos,
# capturas); "3 veces seguidas" da 2, y las dos son bucles de verdad (findings.md §F6).

def test_bucle_tres_llamadas_identicas_seguidas():
    events = [*call("Read"), *call("Read"), *call("Read")]
    sig = signals.agent_signals(events, NOW)
    assert [(s["kind"], s["alert"]) for s in sig] == [("loop", True)]
    assert sig[0]["event"] == events[4]["id"]          # la tercera llamada
    # cuatro seguidas: sigue siendo UN bucle, no dos
    assert kinds([*call("Read"), *call("Read"), *call("Read"), *call("Read")]) == [("loop", True)]


def test_releer_con_otra_cosa_entre_medias_no_es_bucle():
    assert kinds([*call("Read"), *call("Edit"), *call("Read"), *call("Bash"), *call("Read")]) == []
    assert kinds([*call("Read", "a"), *call("Read", "b"), *call("Read", "a")]) == []


def test_bucle_sin_error_intermedio():
    # si una de las tres falla no es un bucle: es un reintento (otra señal)
    assert ("loop", True) not in kinds([*call("Bash"), *call("Bash", result="error"),
                                        *call("Bash")])


# --- reintentos --------------------------------------------------------------------------------

def test_reintento_que_tambien_fallo_alerta():
    events = [*call("Bash", "x", "error"), *call("Read"), *call("Bash", "y", "error")]
    assert kinds(events) == [("error", True), ("error", True), ("retry_failed", True)]


def test_reintento_que_funciono_sin_alerta():
    events = [*call("Bash", "x", "error"), *call("Bash", "y", "ok")]
    assert kinds(events) == [("error", True), ("retry_ok", False)]


def test_bloqueo_reintentado_no_es_reintento_fallido():
    # bloquear y repetir es el flujo normal de un hook (GateGuard): no es "atascado"
    events = [*call("Edit", result="blocked"), *call("Edit", result="blocked"), *call("Edit")]
    assert kinds(events) == [("blocked", False), ("blocked", False)]


# --- herramienta colgada ---------------------------------------------------------------------

def test_herramienta_sin_resultado_mas_de_10_min():
    assert kinds(call("Bash", result=None, ago=11)) == [("hung", True)]
    assert kinds(call("Bash", result=None, ago=5)) == []          # aún dentro de lo normal
    assert kinds(call("Bash", result="ok", ago=60)) == []         # terminó


def test_subagente_de_larga_duracion_no_esta_colgado():
    # el Agent lleva 40 min sin resultado, pero su hijo sigue escribiendo: está trabajando
    (use,) = call("Agent", result=None, ago=40)
    assert kinds([use], live_children={use["tool_use_id"]}) == []
    assert kinds([use]) == [("hung", True)]                        # hijo parado: sí


def test_herramienta_abandonada_no_esta_colgada():
    # §7.1: solo cuenta la ÚLTIMA respuesta sin prompt posterior; una herramienta interrumpida
    # hace días, tras la que el agente siguió, no está "colgada"
    old = call("Bash", result=None, ago=600)
    assert kinds([*old, ev("prompt", ago=500)]) == []
    assert kinds([*old, *call("Read", ago=400)]) == []


def test_una_respuesta_con_varias_herramientas_en_vuelo():
    a = call("Read", "1", None, ago=20, req="r")
    b = call("Read", "2", None, ago=20, req="r")
    assert kinds([*a, *b]) == [("hung", True), ("hung", True)]


def test_llamadas_en_paralelo_no_son_reintentos():
    # Dos Bash en la MISMA respuesta: si uno falla, Claude Code cancela el otro y lo registra
    # también como error. El modelo no reintentó nada (revisión de F6).
    a = call("Bash", "1", "error", req="r1")
    b = call("Bash", "2", "error", req="r1")
    assert kinds([a[0], b[0], a[1], b[1]]) == [("error", True), ("error", True)]
    # el siguiente uso, ya en otra respuesta, sí es un reintento
    assert kinds([a[0], b[0], a[1], b[1], *call("Bash", "3", "error", req="r2")])[-1] == (
        "retry_failed", True)


def test_agente_terminado_no_tiene_herramientas_colgadas():
    # §7.1: "terminado" manda; un tool_use sin resultado de un agente que ya terminó está
    # abandonado, no colgado (revisión de F6)
    assert kinds(call("Bash", result=None, ago=60), finished=True) == []
