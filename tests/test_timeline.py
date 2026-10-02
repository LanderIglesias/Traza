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
    assert got["tool"] == 0 and got["model"] == pytest.approx(10 + 5.013)


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
        for b in a["bars"]:
            assert 0 <= b["start"] <= b["model_end"] <= total + 1e-9, (aid, b)
            assert b["event"] is not None                  # pulsar la barra lleva al turno
    # el subagente corre DENTRO del tiempo del principal: mismo eje
    main = t["agents"]["main"]["bars"]
    (sub,) = t["agents"]["abc"]["bars"]
    assert main[0]["start"] <= sub["start"] <= main[-1]["model_end"]
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
