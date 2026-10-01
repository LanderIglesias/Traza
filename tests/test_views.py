"""F3: lo que ve la pantalla (design.md §7 y §7.1), calculado desde la caché."""
import os
import shutil
import time
from pathlib import Path

import pytest

from traza import db, views
from traza.watcher import scan

FIX = Path(__file__).parent / "fixtures"


@pytest.fixture
def conn(tmp_path):
    root = tmp_path / "projects"
    shutil.copytree(FIX, root / "proj")
    for f in root.rglob("*.jsonl"):   # copytree conserva el mtime viejo: la sesión estaría "parada"
        os.utime(f)
    c = db.connect(tmp_path / "traza.db")
    scan(c, root)
    return c


# --- autómata de estado (§7.1) ---------------------------------------------------------------

@pytest.mark.parametrize("kind, inflight, expected", [
    ("tool_use", True, "tool"),           # herramienta pedida, sin resultado aún
    ("tool_result", False, "thinking"),   # resultado entregado: turno del modelo
    ("prompt", False, "thinking"),        # prompt enviado: el modelo aún no escribe
    ("api_error", False, "thinking"),     # reintentando
    # Última línea = texto del modelo → turno terminado. NO se mira stop_reason: las versiones
    # 2.1.24–2.1.27 lo escriben null incluso en la respuesta final (42 ficheros en disco).
    ("text", False, "idle"),
    ("text", True, "tool"),               # cualquier herramienta en vuelo manda
])
def test_estado_de_un_agente_por_su_ultima_linea(kind, inflight, expected):
    assert views.agent_state(kind, inflight) == expected


def test_estado_de_sesion_es_el_del_agente_mas_activo_y_caduca():
    assert views.session_state(["idle", "thinking", "tool"], idle_for_s=5) == "tool"
    assert views.session_state(["idle", "thinking"], idle_for_s=5) == "thinking"
    assert views.session_state(["idle"], idle_for_s=5) == "idle"
    # modelo que tarda 3 min en contestar: sigue vivo (no depende de "modificado hace < 2 min")
    assert views.session_state(["thinking"], idle_for_s=180) == "thinking"
    # > 10 min sin tocar ningún fichero: cerrada a mitad de turno, no "pensando" para siempre
    assert views.session_state(["thinking"], idle_for_s=601) == "idle"


# --- overview: lo que pinta la pantalla ------------------------------------------------------

def test_overview_sobre_la_fixture(conn):
    ov = views.overview(conn, now=time.time())
    by_id = {s["id"]: s for s in ov["sessions"]}
    a, b = by_id["sess-A"], by_id["sess-B"]
    # sess-A: el subagente abc tiene un tool_use (Grep) sin resultado → tool, y está viva
    assert (a["state"], a["live"], a["title"], a["project"], a["agents"]) == (
        "tool", True, "Listar ficheros", "proj", 2)
    assert (b["state"], b["live"], b["title"]) == ("idle", False, None)
    # para la tarjeta "Live now": qué herramienta corre, en qué agente y desde cuándo
    assert a["activity"] == {"agent": "Explore", "tool": "Grep",
                             "since": "2026-01-01T10:00:15.000Z"}
    assert b["activity"] is None
    # coste propio (µ$ calculados a mano: ver test) y heredado, opción (a) de §7
    assert a["cost"] == pytest.approx(0.001177) and a["unpriced"] == 0
    assert b["cost"] == pytest.approx(0.000046)
    assert b["inherits"] == [{"from": "sess-A", "requests": 1, "cost": pytest.approx(0.000315),
                              "unpriced": 0}]
    assert a["inherits"] == []
    # señales, separadas del estado: errores reales (herramienta que falló + api_error) y
    # bloqueos (toolDenialKind: la herramienta no llegó a ejecutarse; en disco 58 % son hooks)
    assert (a["errors"], a["blocked"], b["errors"], b["blocked"]) == (1, 1, 0, 0)
    # la ventana suma cada petición una vez (req_1 no se cuenta dos veces)
    assert ov["window"] == {"cost": pytest.approx(0.001223), "unpriced": 0}
    assert ov["counts"] == {"live_sessions": 1, "active_agents": 1, "sessions": 2}
    assert ov["health"] == {"unknown": 1, "ignored": 4, "ignored_types": 4, "implausible": 0}
    assert ov["generation"] == db.generation(conn)
    # orden: última actividad primero (ISO-8601, comparable como texto)
    acts = [s["last_activity"] for s in ov["sessions"]]
    assert acts == sorted(acts, reverse=True)


def test_overview_mas_de_10_min_despues_nada_esta_vivo(conn):
    ov = views.overview(conn, now=time.time() + 3600)
    assert ov["counts"]["live_sessions"] == 0
    assert {s["state"] for s in ov["sessions"]} == {"idle"}


def test_peticion_sin_precio_no_se_suma_como_cero(conn):
    conn.execute("UPDATE requests SET model = 'claude-futuro-9' WHERE request_id = 'req_9'")
    ov = views.overview(conn, now=time.time())
    b = next(s for s in ov["sessions"] if s["id"] == "sess-B")
    # su única petición propia no tiene precio: "?" (None), nunca "$0.00+" (§6.6)
    assert (b["cost"], b["unpriced"]) == (None, 1)
    assert ov["window"]["unpriced"] == 1 and ov["window"]["cost"] is not None   # parcial: "$X+"


@pytest.mark.parametrize("model_for_all, expected", [
    ("claude-sonnet-5", None),          # (sin cambiar nada) todo con precio: "$X"
    ("claude-futuro-9", "?"),            # nada con precio: "?"
])
def test_tres_casos_de_una_suma_de_coste(conn, model_for_all, expected):
    # §6.6 aplicado a TODAS las sumas: completa → $X, parcial → $X+, sin nada con precio → ?
    if expected == "?":
        conn.execute("UPDATE requests SET model = ?", (model_for_all,))
    ov = views.overview(conn, now=time.time())
    a = next(s for s in ov["sessions"] if s["id"] == "sess-A")
    b = next(s for s in ov["sessions"] if s["id"] == "sess-B")
    for money in (ov["window"], a, b, b["inherits"][0]):
        if expected == "?":
            assert money["cost"] is None and money["unpriced"] > 0
        else:
            assert money["cost"] > 0 and money["unpriced"] == 0


def test_herencia_parcialmente_sin_precio(conn):
    conn.execute("UPDATE requests SET model = 'claude-futuro-9' WHERE request_id = 'req_1'")
    b = next(s for s in views.overview(conn, now=time.time())["sessions"] if s["id"] == "sess-B")
    assert b["inherits"] == [{"from": "sess-A", "requests": 1, "cost": None, "unpriced": 1}]


def test_resumen_de_sesion(conn):
    s = views.session_summary(conn, "sess-A")
    assert s["models"] == {"claude-sonnet-5": 3, "claude-haiku-4-5-20251001": 2}
    assert (s["title"], s["agents"], s["requests"]) == ("Listar ficheros", 2, 5)
    assert s["started"] == "2026-01-01T10:00:00.000Z"
    assert views.session_summary(conn, "no-existe") is None


@pytest.mark.parametrize("origin, expected", [
    ("interrupted", "idle"), ("local-command", "idle"),   # el modelo no contesta
    ("human", "thinking"), ("meta", "thinking"), (None, "thinking"),
])
def test_prompt_segun_su_origen(origin, expected):
    assert views.agent_state("prompt", False, origin) == expected


def test_subagente_interrumpido_no_deja_la_sesion_en_tool_para_siempre(conn, tmp_path):
    # abc tiene un Grep sin resultado; si su fichero lleva > 10 min quieto aunque el principal
    # siga activo, abc no cuenta como trabajando.
    sub = next(tmp_path.rglob("agent-abc.jsonl"))
    old = time.time() - 3600
    os.utime(sub, (old, old))
    scan(conn, tmp_path / "projects")
    a = next(s for s in views.overview(conn)["sessions"] if s["id"] == "sess-A")
    assert (a["state"], a["activity"]) == ("idle", None)


def test_coste_de_hoy_en_hora_local(conn):
    # Tarjeta "Cost today" (sustituye a "Live sessions", que repetía la tarjeta coral).
    # Las peticiones de la fixture son del 2026-01-01 ~10:00Z; "hoy" = el día local de `now`.
    from datetime import datetime, timezone
    same_day = datetime(2026, 1, 1, 10, 30, tzinfo=timezone.utc).timestamp()
    ov = views.overview(conn, now=same_day)
    assert ov["today"] == {"cost": pytest.approx(0.001223), "unpriced": 0}
    # cada petición UNA vez, en su dueña: la copia sess-B hereda req_1 de sess-A y no se suma dos
    # veces (sumar por sesión daría 0.001223 + 0.000315)
    own = sum(s["cost"] for s in ov["sessions"])
    assert ov["today"]["cost"] == pytest.approx(own)
    assert ov["sessions"][0]["inherits"] or ov["sessions"][1]["inherits"]   # sí hay herencia
    next_day = same_day + 86400
    assert views.overview(conn, now=next_day)["today"] == {"cost": 0, "unpriced": 0}
    # hoy, pero sin nada con precio: "?", no "$0.00+"
    conn.execute("UPDATE requests SET model = 'claude-futuro-9'")
    assert views.overview(conn, now=same_day)["today"] == {"cost": None, "unpriced": 5 + 1}


def test_medianoche_local_en_dia_de_cambio_de_hora():
    # 25-10-2026 Madrid: a las 03:00 se vuelve a +01:00, pero la medianoche aún era +02:00.
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo
    madrid = ZoneInfo("Europe/Madrid")
    noon = datetime(2026, 10, 25, 12, 0, tzinfo=madrid).timestamp()
    assert views._local_midnight(noon, madrid) == datetime(2026, 10, 24, 22, 0, tzinfo=timezone.utc)


def test_timestamp_sin_zona_no_tumba_el_panel(conn):
    # El parser guarda el texto del JSONL tal cual; uno sin zona no puede dar un 500.
    conn.execute("UPDATE requests SET timestamp = '2026-01-01T10:00:02' WHERE request_id = 'req_1'")
    conn.execute("UPDATE requests SET timestamp = 'basura' WHERE request_id = 'req_2'")
    from datetime import datetime, timezone
    same_day = datetime(2026, 1, 1, 10, 30, tzinfo=timezone.utc).timestamp()
    today = views.overview(conn, now=same_day)["today"]
    # sin zona = UTC (los JSONL escriben UTC); ilegible = no se cuenta como de hoy
    assert today["cost"] == pytest.approx(0.001223 - 0.000702)


# --- F4: árbol de agentes ----------------------------------------------------------------------

def _copy(tmp_path, edit=None):
    """Copia de la fixture, `edit(ruta_raíz_de_sess-A)` para variarla, y la caché ya escaneada."""
    root = tmp_path / "projects"
    shutil.copytree(FIX, root / "proj")
    if edit:
        edit(root / "proj")
    for f in root.rglob("*.jsonl"):
        os.utime(f)
    c = db.connect(tmp_path / "traza.db")
    scan(c, root)
    return c


def _nodes(conn, sid="sess-A"):
    return {n["id"]: n for n in views.agent_tree(conn, sid, now=time.time())}


def _append(path, obj):
    import json
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj) + "\n")


def test_arbol_de_la_fixture(conn):
    n = _nodes(conn)
    assert set(n) == {"main", "abc"}
    assert (n["main"]["parent"], n["main"]["orphan"]) == (None, None)
    assert (n["abc"]["parent"], n["abc"]["orphan"], n["abc"]["type"], n["abc"]["description"]) == (
        "main", None, "Explore", "Buscar x")
    # abc se lanzó en segundo plano: su tool_result ("async_launched") NO es que haya terminado
    assert (n["main"]["state"], n["abc"]["state"]) == ("idle", "tool")
    assert (n["main"]["errors"], n["main"]["blocked"], n["abc"]["errors"]) == (1, 1, 0)
    assert n["abc"]["output"] == 7 and n["abc"]["requests"] == 1


def test_coste_propio_y_acumulado_cuadran_con_report(conn):
    # traza.report lee los ficheros directamente (camino independiente de la caché)
    from traza.pricing import request_cost
    from traza.report import session_agents
    rep = {aid: views._money([request_cost(r) for r in reqs.values()])
           for aid, _, reqs in session_agents(FIX / "sess-A.jsonl")}
    n = _nodes(conn)
    for aid in ("main", "abc"):
        assert n[aid]["cost"] == pytest.approx(rep[aid]["cost"]) and n[aid]["unpriced"] == 0
    assert n["abc"]["total"]["cost"] == pytest.approx(rep["abc"]["cost"])
    assert n["main"]["total"]["cost"] == pytest.approx(rep["main"]["cost"] + rep["abc"]["cost"])
    # y el árbol entero es el coste de la sesión (no es una copia: no hereda nada)
    a = next(s for s in views.overview(conn, now=time.time())["sessions"] if s["id"] == "sess-A")
    assert n["main"]["total"]["cost"] == pytest.approx(a["cost"])


def test_subagente_terminado_por_notificacion(tmp_path):
    def done(proj):
        _append(proj / "sess-A.jsonl", {
            "type": "user", "uuid": "u9", "timestamp": "2026-01-01T10:00:30.000Z",
            "origin": {"kind": "task-notification"},
            "message": {"role": "user", "content":
                        "<task-notification><task-id>abc</task-id><status>completed</status>"}})
    n = _nodes(_copy(tmp_path, done))
    # terminado aunque su último Grep no tenga resultado; y deja de contar como trabajando
    assert n["abc"]["state"] == "done"
    assert views.session_state(["done", "idle"], idle_for_s=5) == "idle"


def _with_task(proj, meta):
    """abc con ese meta.json y un primer prompt (su encargo) delante de su línea."""
    import json
    sub = proj / "sess-A" / "subagents"
    (sub / "agent-abc.meta.json").write_text(meta)
    body = (sub / "agent-abc.jsonl").read_text(encoding="utf-8")
    prompt = {"type": "user", "uuid": "p0", "timestamp": "2026-01-01T10:00:14.000Z",
              "isMeta": True, "message": {"role": "user", "content": [
                  {"type": "text", "text": "  Review target: los cambios de F3 " + "x" * 300}]}}
    (sub / "agent-abc.jsonl").write_text(json.dumps(prompt) + "\n" + body, encoding="utf-8")


def test_huerfano_skill_fork_muestra_el_principio_del_encargo(tmp_path):
    fork = lambda proj: _with_task(proj, '{"agentType": "general-purpose", "spawnDepth": 1}')
    n = _nodes(_copy(tmp_path, fork))
    assert (n["abc"]["parent"], n["abc"]["orphan"]) == (None, "sin_tool_use_id")
    assert n["abc"]["task"].startswith("Review target: los cambios de F3")
    assert len(n["abc"]["task"]) <= 160


def test_huerfano_con_padre_desconocido_no_es_skill_fork(tmp_path):
    # toolUseId presente pero ese tool_use no está en la sesión: "padre desconocido", nunca
    # la etiqueta (ni el texto) de skill fork
    # con encargo en su JSONL: si la regla mirase solo "es huérfano", aquí saldría el texto
    lost = lambda proj: _with_task(
        proj, '{"agentType": "Explore", "toolUseId": "toolu_no_esta", "spawnDepth": 1}')
    n = _nodes(_copy(tmp_path, lost))
    assert (n["abc"]["parent"], n["abc"]["orphan"], n["abc"]["task"]) == (
        None, "padre_no_encontrado", None)


def test_subagente_anidado_y_acumulado(tmp_path):
    # def lo lanzó abc con su tool_use toolu_s1; el acumulado de main incluye a los dos
    def nested(proj):
        sub = proj / "sess-A" / "subagents"
        (sub / "agent-def.meta.json").write_text(
            '{"agentType": "Plan", "toolUseId": "toolu_s1", "spawnDepth": 2}')
        _append(sub / "agent-def.jsonl", {
            "type": "assistant", "uuid": "d1", "timestamp": "2026-01-01T10:00:16.000Z",
            "requestId": "req_d1", "message": {"model": "claude-sonnet-5", "content": [
                {"type": "text", "text": "hecho"}], "stop_reason": "end_turn", "usage": {
                    "input_tokens": 10, "output_tokens": 100, "cache_read_input_tokens": 0,
                    "cache_creation": {"ephemeral_5m_input_tokens": 0,
                                       "ephemeral_1h_input_tokens": 0}}}})
    n = _nodes(_copy(tmp_path, nested))
    assert (n["def"]["parent"], n["abc"]["parent"]) == ("abc", "main")
    assert n["abc"]["total"]["cost"] == pytest.approx(n["abc"]["cost"] + n["def"]["cost"])
    assert n["main"]["total"]["cost"] == pytest.approx(
        n["main"]["cost"] + n["abc"]["cost"] + n["def"]["cost"])
    assert n["def"]["state"] == "idle"          # terminó con texto y end_turn


def test_arbol_de_sesion_inexistente(conn):
    assert views.agent_tree(conn, "no-existe") == []


@pytest.mark.parametrize("stop_reason, quiet_s, expected", [
    # 2.1.284: el hilo principal escribe "tool_use" en la línea de texto que precede a una
    # herramienta (159/159); los subagentes escriben la respuesta mientras se genera y esa línea
    # lleva None (6/6). La línea FINAL de texto lleva end_turn (47/47). findings.md §F4.
    ("tool_use", 0, "thinking"),
    ("end_turn", 0, "idle"),
    (None, 2, "thinking"),       # respuesta a medias: la herramienta aún no se ha escrito
    # None final solo en versiones ≤ 2.1.268 (44 casos): se ve "idle" con 30 s de retraso
    (None, 31, "idle"),
])
def test_texto_sin_fin_de_turno_explicito(stop_reason, quiet_s, expected):
    assert views.agent_state("text", False, None, stop_reason, quiet_s) == expected


def test_subagente_de_primer_plano_termina_con_su_resultado(tmp_path):
    # Primer plano: el tool_result del padre llega cuando termina, sin toolUseResult (así lo
    # escribe 2.1.284 dentro de un subagente). Solo "async_launched" NO es terminar.
    def foreground(proj):
        f = proj / "sess-A.jsonl"
        f.write_text(f.read_text(encoding="utf-8").replace(
            '"toolUseResult":{"isAsync":true,"status":"async_launched","agentId":"abc","description":"Buscar x"},', ""),
            encoding="utf-8")
    assert _nodes(_copy(tmp_path, foreground))["abc"]["state"] == "done"


def test_notificacion_encolada_termina_al_subagente_sin_cambiar_el_turno(tmp_path):
    def queued(proj):
        _append(proj / "sess-A.jsonl", {
            "type": "attachment", "uuid": "q1", "timestamp": "2026-01-01T10:00:30.000Z",
            "attachment": {"type": "queued_command", "commandMode": "task-notification",
                           "prompt": "<task-notification><task-id>abc</task-id><status>completed</status>"}})
    n = _nodes(_copy(tmp_path, queued))
    assert n["abc"]["state"] == "done"
    assert n["main"]["state"] == "idle"   # la notificación no es "la última línea" de main


def test_resultado_anterior_al_trabajo_del_hijo_no_es_su_fin(tmp_path):
    # Un resultado sin toolUseResult que llega ANTES de las líneas del hijo es un lanzamiento
    # (p. ej. segundo plano desde un subagente), no un fin: el fin va después de su última línea.
    def early(proj):
        f = proj / "sess-A.jsonl"
        f.write_text(f.read_text(encoding="utf-8").replace(
            '"toolUseResult":{"isAsync":true,"status":"async_launched","agentId":"abc","description":"Buscar x"},', "")
            .replace('"uuid":"u5","timestamp":"2026-01-01T10:00:20.000Z"', '"uuid":"u5","timestamp":"2026-01-01T10:00:10.000Z"'),
            encoding="utf-8")
    assert _nodes(_copy(tmp_path, early))["abc"]["state"] == "tool"


def test_subagente_reanudado_deja_de_estar_terminado(tmp_path):
    # "the same task-id may notify more than once": tras un fin, líneas nuevas = trabaja otra vez
    def resumed(proj):
        _append(proj / "sess-A.jsonl", {
            "type": "user", "uuid": "u9", "timestamp": "2026-01-01T10:00:30.000Z",
            "origin": {"kind": "task-notification"},
            "message": {"role": "user", "content":
                        "<task-notification><task-id>abc</task-id><status>completed</status>"}})
        _append(proj / "sess-A" / "subagents" / "agent-abc.jsonl", {
            "type": "user", "uuid": "r1", "timestamp": "2026-01-01T10:00:40.000Z",
            "message": {"role": "user", "content": "sigue, por favor"}})
    assert _nodes(_copy(tmp_path, resumed))["abc"]["state"] == "thinking"


def test_output_implausible_se_ve_en_salud_y_en_el_nodo(conn):
    # Política de "?" al agregar (design.md §8): como en las sumas de coste, la suma solo lleva
    # "?" si ELLA es implausible, no si lo es algún sumando. abc tiene una sola petición.
    assert views.overview(conn, now=time.time())["health"]["implausible"] == 0
    conn.execute("UPDATE events SET chars = 5000 WHERE request_id = 'req_s1'")   # 7 tokens
    assert views.overview(conn, now=time.time())["health"]["implausible"] == 1
    n = _nodes(conn)
    assert (n["abc"]["implausible"], n["abc"]["implausible_requests"]) == (True, 1)
    assert (n["main"]["implausible"], n["main"]["implausible_requests"]) == (False, 0)


def test_una_peticion_implausible_no_marca_al_agente_entero(conn):
    # main: req_1 declara 20 tokens para 2.000 caracteres (sospechosa: 20 × 40 < 2.000), pero el
    # agente entero declara 84 tokens (× 40 = 3.360) para ~2.000: su suma es plausible.
    conn.execute("UPDATE events SET chars = 2000 WHERE request_id = 'req_1' AND kind = 'text'")
    n = _nodes(conn)
    assert (n["main"]["implausible"], n["main"]["implausible_requests"]) == (False, 1)
    # y si casi todo lo que escribió está mal contado, sí
    conn.execute("UPDATE events SET chars = 9000 WHERE request_id = 'req_2' AND kind = 'text'")
    assert _nodes(conn)["main"]["implausible"] is True
