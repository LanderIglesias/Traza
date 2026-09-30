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
