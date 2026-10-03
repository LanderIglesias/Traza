"""F6.5: eje de tiempo activo, desglose modelo / herramientas / tú y barra de valor (plan.md F6.5)."""
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from traza import timeline
from traza.pricing import request_cost, request_cost_parts
from traza.report import parse_file

FIX = Path(__file__).parent / "fixtures"
T0 = datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc)


def at(seconds: float) -> str:
    return (T0 + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


def ev(kind, s, req=None, tool=None, origin=None):
    return {"kind": kind, "ts": at(s), "request_id": req, "tool_name": tool, "origin": origin}


# --- barra de valor: sale de la misma función que el valor ------------------------------------

def test_las_partes_del_valor_suman_exactamente_el_valor():
    reqs = [p.request for f in ("sess-A.jsonl", "sess-B.jsonl") for p in parse_file(FIX / f)
            if p.request]
    assert reqs
    for r in reqs:
        parts, total = request_cost_parts(r), request_cost(r)
        if total is None:
            assert parts is None                     # sin precio: no se inventa un reparto
        else:
            assert set(parts) == {"input", "output", "cache_read", "cache_write", "web_search"}
            assert sum(parts.values()) == pytest.approx(total, abs=1e-12)


def test_valor_agregado_con_precios_desconocidos():
    # política de F6: cada tramo suma lo que tiene precio; "+" si falta alguna; "?" si ninguna
    a = {"input": 1.0, "output": 2.0, "cache_read": 0.5, "cache_write": 0.0, "web_search": 0.0}
    got = timeline.value_split([a, None])
    assert got["parts"]["output"] == 2.0 and got["cost"] == 3.5 and got["unpriced"] == 1
    assert timeline.value_split([None, None]) == {"parts": None, "cost": None, "unpriced": 2}


# --- eje de tiempo activo: los huecos de inactividad desaparecen --------------------------------

def test_eje_activo_elimina_los_huecos_de_inactividad():
    ts = [at(0), at(120), at(3 * 3600), at(3 * 3600 + 60)]
    axis = timeline.ActiveAxis(ts)
    # 2 min de trabajo cuentan a escala; las 3 h de pausa no ocupan nada
    assert [axis.offset(t) for t in ts] == [0, 120, 120, 180]
    assert axis.total == 180
    assert axis.breaks == [{"at": 120, "idle_s": 3 * 3600 - 120}]


def test_eje_activo_de_varios_agentes_es_comun():
    # el principal y un subagente en paralelo: sus eventos caen en el mismo tramo del eje
    main = [at(0), at(100)]
    sub = [at(30), at(60)]
    axis = timeline.ActiveAxis(main + sub)
    assert [axis.offset(t) for t in sub] == [30, 60]


# --- desglose modelo / herramientas / tú: un test por caso raro ---------------------------------

def test_desglose_basico():
    events = [ev("prompt", 0, origin="human"), ev("text", 10, "r1"),          # modelo 10
              ev("prompt", 40, origin="human"),                              # tú 30
              ev("tool_use", 45, "r2", "Bash"), ev("tool_result", 65),       # modelo 5, herr. 20
              ev("text", 70, "r3")]                                          # modelo 5
    assert timeline.breakdown(events) == {"model": 20, "tool": 20, "user": 30, "idle": 0}


def test_herramienta_de_cero_segundos():
    events = [ev("prompt", 0, origin="human"), ev("tool_use", 5, "r1", "Read"),
              ev("tool_result", 5), ev("text", 9, "r2")]
    assert timeline.breakdown(events) == {"model": 9, "tool": 0, "user": 0, "idle": 0}


def test_timestamps_desordenados_no_restan():
    # en disco el resultado puede llevar una hora unos ms anterior a la de su llamada
    events = [ev("prompt", 0, origin="human"), ev("tool_use", 10, "r1", "Bash"),
              ev("tool_result", 9.987), ev("text", 15, "r2")]
    got = timeline.breakdown(events)
    # 0 → 15 de reloj: 15 s, no 10 + 5,013 (los 13 ms ya estaban contados; ver el test de abajo)
    assert got["tool"] == 0 and got["model"] == pytest.approx(15)


def test_texto_seguido_de_herramienta_de_la_misma_respuesta_es_modelo():
    # el modelo escribe el texto y sigue generando la llamada: no es tiempo tuyo
    events = [ev("prompt", 0, origin="human"), ev("text", 4, "r1"),
              ev("tool_use", 10, "r1", "Grep"), ev("tool_result", 12), ev("text", 13, "r2")]
    assert timeline.breakdown(events) == {"model": 4 + 6 + 1, "tool": 2, "user": 0, "idle": 0}


def test_texto_final_es_tuyo_hasta_tu_siguiente_prompt():
    events = [ev("text", 0, "r1"), ev("prompt", 50, origin="human")]
    assert timeline.breakdown(events)["user"] == 50


def test_espera_a_un_subagente_en_segundo_plano_no_es_tuya():
    # el principal terminó y lo siguiente es la notificación de un subagente: estaba esperando
    # a una herramienta, no a ti
    events = [ev("text", 0, "r1"), ev("prompt", 40, origin="task-notification")]
    assert timeline.breakdown(events) == {"model": 0, "tool": 40, "user": 0, "idle": 0}


def test_prompt_que_el_modelo_no_contesta_deja_el_turno_al_usuario():
    for origin in ("interrupted", "local-command"):
        events = [ev("prompt", 0, origin=origin), ev("prompt", 30, origin="human")]
        assert timeline.breakdown(events)["user"] == 30


def test_herramientas_que_esperan_al_usuario_son_tuyas():
    for tool in ("AskUserQuestion", "ExitPlanMode"):
        events = [ev("tool_use", 0, "r1", tool), ev("tool_result", 90)]
        assert timeline.breakdown(events) == {"model": 0, "tool": 0, "user": 90, "idle": 0}


def test_hueco_largo_es_inactividad_y_no_cuenta():
    events = [ev("text", 0, "r1"), ev("prompt", 3 * 3600, origin="human"), ev("text", 3 * 3600 + 8, "r2")]
    assert timeline.breakdown(events) == {"model": 8, "tool": 0, "user": 0, "idle": 3 * 3600}


def test_agent_de_primer_plano_cuenta_como_herramienta_del_principal():
    # el trabajo del subagente se ve en su propia fila; al principal le cuenta como herramienta
    events = [ev("tool_use", 0, "r1", "Agent"), ev("tool_result", 120), ev("text", 125, "r2")]
    assert timeline.breakdown(events) == {"model": 5, "tool": 120, "user": 0, "idle": 0}


def test_resultados_en_paralelo_siguen_siendo_herramienta():
    # dos herramientas de la misma respuesta: entre el primer y el segundo resultado sigue
    # corriendo la segunda
    events = [ev("tool_use", 0, "r1", "Bash"), ev("tool_use", 1, "r1", "Bash"),
              ev("tool_result", 10), ev("tool_result", 30), ev("text", 32, "r2")]
    assert timeline.breakdown(events) == {"model": 1 + 2, "tool": 9 + 20, "user": 0, "idle": 0}


# --- integración con la caché (fixture) ----------------------------------------------------------
import os
import shutil

from traza import db, views
from traza.watcher import scan


@pytest.fixture
def conn(tmp_path):
    root = tmp_path / "projects"
    shutil.copytree(FIX, root / "proj")
    for f in root.rglob("*.jsonl"):
        os.utime(f)
    c = db.connect(tmp_path / "traza.db")
    scan(c, root)
    return c


def test_timeline_de_sesion_con_eje_comun(conn):
    t = views.session_timeline(conn, "sess-A")
    assert set(t["agents"]) == {"main", "abc"}
    total = t["axis"]["total"]
    for aid, a in t["agents"].items():
        for start, end, kind, event in a["segments"]:
            assert 0 <= start < end <= total + 1e-9, (aid, start, end)
            assert kind in ("model", "tool", "user") and event is not None   # pulsar lleva ahí
    # el subagente corre DENTRO del tiempo del principal: mismo eje
    main, sub = t["agents"]["main"]["segments"], t["agents"]["abc"]["segments"]
    assert main[0][0] <= sub[0][0] <= sub[-1][1] <= main[-1][1]
    assert views.session_timeline(conn, "no-existe") is None


def test_resumen_del_agente_cuadra(conn):
    j = views.agent_view(conn, "sess-A", "main")
    s = j["summary"]
    b = s["breakdown"]
    assert b["model"] + b["tool"] + b["user"] == pytest.approx(s["active_s"])
    # la barra de valor suma exactamente el valor propio del agente (misma fórmula)
    assert sum(s["value"]["parts"].values()) == pytest.approx(j["agent"]["cost"])
    assert s["tokens"]["output"] == j["agent"]["output"]
    assert s["wall_start"] <= s["wall_end"]


# --- revisión de F6.5: una herramienta en marcha no es inactividad -----------------------------

def test_esperar_a_una_herramienta_larga_no_es_inactividad():
    # el principal espera 20 min a un subagente de primer plano (en disco, hasta 64 min)
    events = [dict(ev("tool_use", 0, "r1", "Agent"), tool_use_id="t"),
              dict(ev("tool_result", 1200), tool_use_id="t"), ev("text", 1205, "r2")]
    assert timeline.breakdown(events) == {"model": 5, "tool": 1200, "user": 0, "idle": 0}


def test_herramienta_sin_resultado_no_cuenta_horas_de_trabajo():
    # una llamada interrumpida (nunca llega su resultado) y el siguiente prompt 3 h después: no
    # es una herramienta en marcha 3 h. Antes el desglose lo contaba como herramienta y sumaba
    # 10 h en una sesión de 5 h activas (el eje ya lo cortaba). Misma regla que el eje.
    events = [dict(ev("tool_use", 0, "r1", "Bash"), tool_use_id="t"),
              ev("prompt", 3 * 3600, None, None, "human"), ev("text", 3 * 3600 + 4, "r2")]
    assert timeline.breakdown(events) == {"model": 4, "tool": 0, "user": 0, "idle": 3 * 3600}


def test_esperar_al_usuario_mucho_rato_si_es_inactividad():
    # AskUserQuestion durante 2 h: probablemente te fuiste; no es trabajo
    events = [ev("tool_use", 0, "r1", "AskUserQuestion"), ev("tool_result", 7200)]
    assert timeline.breakdown(events) == {"model": 0, "tool": 0, "user": 0, "idle": 7200}


def test_eje_activo_no_corta_una_herramienta_en_marcha():
    # 15 min sin eventos, pero una herramienta los cubre entera: es trabajo, no pausa
    axis = timeline.ActiveAxis([at(0), at(900), at(910)], busy=[(at(0), at(900))])
    assert axis.total == 910 and axis.breaks == []
    # sin herramienta que lo cubra, la misma pausa sí se elimina
    assert timeline.ActiveAxis([at(0), at(900), at(910)]).total == 10


def test_tramo_de_herramientas_en_paralelo_llega_al_ultimo_resultado():
    evs = [dict(ev(*a[:5]), id=i, tool_use_id=a[5]) for i, a in enumerate([
           ("prompt", 0, None, None, "human", None), ("tool_use", 2, "r", "Read", None, "a"),
           ("tool_use", 3, "r", "Bash", None, "b"), ("tool_result", 4, None, None, None, "a"),
           ("tool_result", 60, None, None, None, "b")])]
    segs = timeline.segments(evs, timeline.ActiveAxis(e["ts"] for e in evs))
    assert segs[-1][1:3] == [60, "tool"]               # el Bash largo, no el Read
    # con el Bash aún en marcha, el tramo de herramienta sigue abierto hasta el final del eje
    axis30 = timeline.ActiveAxis([at(0), at(2), at(3), at(4), at(30)])
    segs = timeline.segments(evs[:4], axis30, open_tool=True)
    assert segs[-1][1:3] == [30, "tool"]
    # /code-review de F7: si el agente ya no tiene esa herramienta en vuelo (terminó, o se paró y
    # el eje lo alarga otro agente), no se inventa tiempo de herramienta hasta el final
    segs = timeline.segments(evs[:4], axis30)
    assert segs[-1][1] == 4


# --- revisión de F6.5 (antes de F7): una barra continua por agente, no una marca por turno ---------

def test_barra_continua_cuadra_con_el_desglose_y_funde_tramos():
    # turnos de segundos en sesiones de horas eran rayas sub-píxel: ahora cada agente es una
    # barra desde su primera hasta su última actividad, coloreada por quién tenía el turno
    evs = [dict(ev(*a), id=i) for i, a in enumerate([
        ("prompt", 0, None, None, "human"), ("text", 1, "r1"), ("tool_use", 2, "r1", "Read"),
        ("tool_result", 10), ("text", 12, "r2"), ("prompt", 100, None, None, "human"),
        ("text", 105, "r3"), ("prompt", 2000, None, None, "human"), ("text", 2003, "r4")])]
    axis = timeline.ActiveAxis(e["ts"] for e in evs)
    segs = timeline.segments(evs, axis)
    # prompt→texto→llamada de la misma respuesta: un solo tramo de modelo, desde el evento 0
    assert segs == [[0, 2, "model", 0], [2, 10, "tool", 2], [10, 12, "model", 3],
                    [12, 100, "user", 4], [100, 105, "model", 5], [105, 108, "model", 7]]
    # la pausa de 31 min (105 → 2000) no se dibuja: el eje la quitó y la barra la salta
    assert axis.total == 108
    b = timeline.breakdown(evs)
    for k in ("model", "tool", "user"):
        assert sum(e - s for s, e, kind, _ in segs if kind == k) == pytest.approx(b[k])


def test_eventos_que_retroceden_en_el_tiempo_no_cuentan_dos_veces():
    # en disco, el orden del fichero no siempre es cronológico (escrituras en paralelo): con
    # 100 → 50 → 120 se contaba 50→120 entero (70 s) aunque 50–100 ya estaba contado. El
    # desglose de la sesión real sumaba 6 h de trabajo en 5,3 h activas.
    events = [dict(ev(*a), id=i) for i, a in enumerate([
        ("prompt", 0, None, None, "human"), ("text", 100, "r1"), ("text", 50, "r1"), ("text", 120, "r1")])]
    b = timeline.breakdown(events)
    assert b["model"] + b["tool"] + b["user"] == 120
    axis = timeline.ActiveAxis(e["ts"] for e in events)
    segs = timeline.segments(events, axis)
    assert sum(e - s for s, e, _, _ in segs) == pytest.approx(axis.total) == 120


def test_herramienta_sin_resultado_no_se_alarga_si_el_agente_siguio():
    # /code-review antes de F7: un Bash sin resultado a los 10 s, el agente responde otra vez a
    # los 30 s y un subagente alarga el eje a 1000 s. La fila del principal pintaba 970 s de
    # herramienta. Solo sigue abierta si es de la ÚLTIMA respuesta y sin prompt posterior (§7.1).
    evs = [dict(ev(*a[:5]), id=i, tool_use_id=a[5]) for i, a in enumerate([
        ("prompt", 0, None, None, "human", None), ("tool_use", 10, "r1", "Bash", None, "t"),
        ("text", 30, "r2", None, None, None)])]
    sub = [at(s) for s in range(100, 1001, 100)]          # un subagente escribiendo hasta 1000 s
    axis = timeline.ActiveAxis([*(e["ts"] for e in evs), *sub])
    assert axis.total == 1000
    segs = timeline.segments(evs, axis)
    assert segs[-1][1] == 30
    b = timeline.breakdown(evs)
    assert sum(e - s for s, e, _, _ in segs) == pytest.approx(b["model"] + b["tool"] + b["user"])
