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
    assert b["inherits"] == [{"from": "sess-A", "requests": 1, "cost": pytest.approx(0.000315)}]
    assert a["inherits"] == []
    # señales de F3: errores de herramienta + api_error, separadas del estado
    assert (a["errors"], b["errors"]) == (2, 0)
    # la ventana suma cada petición una vez (req_1 no se cuenta dos veces)
    assert ov["window"] == {"cost": pytest.approx(0.001223), "unpriced": 0}
    assert ov["counts"] == {"live_sessions": 1, "active_agents": 1, "sessions": 2}
    assert ov["health"] == {"unknown": 1, "ignored": 4, "ignored_types": 4}
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
    assert (b["cost"], b["unpriced"]) == (0, 1)       # la UI pinta "+" y "1 unpriced"
    assert ov["window"]["unpriced"] == 1


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
