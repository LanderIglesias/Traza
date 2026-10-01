"""F5: vista de juicio (design.md §7) y contenido bajo demanda (§6.5)."""
import json
import os
import shutil
from pathlib import Path

import pytest

from traza import db, views
from traza.watcher import scan

FIX = Path(__file__).parent / "fixtures"
SCRIPT = "<script>alert(1)</script>"


def _append(path, obj):
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(obj) + "\n")


def _usage(out):
    return {"input_tokens": 3, "output_tokens": out, "cache_read_input_tokens": 0,
            "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 0}}


@pytest.fixture
def root(tmp_path):
    """La fixture + un subagente abc completo: encargo, Grep cuyo resultado lleva un <script>,
    respuesta final y la notificación de fin en el padre (segundo plano)."""
    root = tmp_path / "projects"
    shutil.copytree(FIX, root / "proj")
    sub = root / "proj" / "sess-A" / "subagents" / "agent-abc.jsonl"
    grep = sub.read_text(encoding="utf-8")
    sub.write_text(json.dumps({
        "type": "user", "uuid": "p0", "timestamp": "2026-01-01T10:00:14.000Z",
        "message": {"role": "user", "content": "Busca x en el repo y dime dónde está"}}) + "\n"
        + grep, encoding="utf-8", newline="\n")
    _append(sub, {"type": "user", "uuid": "r1", "timestamp": "2026-01-01T10:00:16.000Z",
                  "message": {"role": "user", "content": [
                      {"type": "tool_result", "tool_use_id": "toolu_s1", "content": SCRIPT}]}})
    _append(sub, {"type": "assistant", "uuid": "a2", "requestId": "req_s2",
                  "timestamp": "2026-01-01T10:00:17.000Z", "message": {
                      "model": "claude-haiku-4-5-20251001", "stop_reason": "end_turn",
                      "content": [{"type": "text", "text": "x no aparece en el repo."}],
                      "usage": _usage(9)}})
    _append(root / "proj" / "sess-A.jsonl", {
        "type": "user", "uuid": "n1", "timestamp": "2026-01-01T10:00:30.000Z",
        "origin": {"kind": "task-notification"}, "message": {"role": "user", "content":
            "<task-notification><task-id>abc</task-id><status>completed</status>"}})
    for f in root.rglob("*.jsonl"):
        os.utime(f)
    return root


@pytest.fixture
def conn(tmp_path, root):
    c = db.connect(tmp_path / "traza.db")
    scan(c, root)
    return c


def _content(conn, root, ids):
    return views.read_content(conn, ids, root)


# --- vista de juicio -----------------------------------------------------------------------------

def test_encargo_timeline_y_resultado_de_un_subagente(conn, root):
    j = views.agent_view(conn, "sess-A", "abc", root=root)
    assert j["agent"]["id"] == "abc" and j["agent"]["state"] == "done"
    # encargo = su primer prompt, fuera de la timeline
    task = _content(conn, root, [j["task"]])[j["task"]]
    assert task == {"ok": True, "text": "Busca x en el repo y dime dónde está", "truncated": False}
    # dos turnos: Grep (con su resultado emparejado) y la respuesta final
    turns = [i for i in j["items"] if i["kind"] == "turn"]
    assert [[e["kind"] for e in t["events"]] for t in turns] == [["tool_use"], ["text"]]
    grep = turns[0]["events"][0]
    assert (grep["tool_name"], grep["result"]["is_error"]) == ("Grep", False)
    assert turns[1]["output"] == 9 and turns[1]["cost"] is not None
    # resultado: segundo plano → su último texto (el padre solo recibió "async_launched")
    assert j["result"]["source"] == "final_text"
    assert _content(conn, root, [j["result"]["id"]])[j["result"]["id"]]["text"] == \
        "x no aparece en el repo."


def test_resultado_de_primer_plano_es_lo_que_recibio_el_padre(tmp_path, root):
    main = root / "proj" / "sess-A.jsonl"
    main.write_text(main.read_text(encoding="utf-8").replace(
        '"toolUseResult":{"isAsync":true,"status":"async_launched","agentId":"abc",'
        '"description":"Buscar x"},', ""), encoding="utf-8", newline="\n")
    c = db.connect(tmp_path / "t2.db")
    scan(c, root)
    j = views.agent_view(c, "sess-A", "abc", root=root)
    assert j["result"]["source"] == "parent_result"
    assert _content(c, root, [j["result"]["id"]])[j["result"]["id"]]["text"] == "hecho"


def test_subagente_trabajando_no_tiene_resultado(tmp_path):
    root = tmp_path / "projects"
    shutil.copytree(FIX, root / "proj")
    c = db.connect(tmp_path / "t.db")
    scan(c, root)
    assert views.agent_view(c, "sess-A", "abc", root=root)["result"] is None


def test_timeline_del_principal_empareja_resultados_y_marca_errores(conn, root):
    j = views.agent_view(conn, "sess-A", "main", root=root)
    assert j["task"] is None                      # el principal no tiene "encargo"
    kinds = [i["kind"] for i in j["items"]]
    assert kinds[0] == "prompt" and "system" in kinds
    reads = [e for i in j["items"] if i["kind"] == "turn" for e in i["events"]
             if e["tool_name"] == "Read"]
    # el bloqueo (toolDenialKind) y el resultado correcto, cada uno con su tool_use
    assert [(e["result"]["is_error"], e["result"]["denial"]) for e in reads] == [
        (True, "permission-rule"), (False, None)]


def test_ultimos_turnos_y_cargar_anteriores(conn, root):
    full = views.agent_view(conn, "sess-A", "main", root=root)
    last2 = views.agent_view(conn, "sess-A", "main", root=root, limit=2)
    assert [i["kind"] for i in last2["items"]].count("turn") == 2
    assert last2["items"] == full["items"][last2["start"]:]
    earlier = views.agent_view(conn, "sess-A", "main", root=root, limit=2,
                               before=last2["start"])
    assert earlier["items"] == full["items"][earlier["start"]:last2["start"]]
    assert full["start"] == 0 and full["total"] == len(full["items"])


def test_agente_inexistente(conn, root):
    assert views.agent_view(conn, "sess-A", "no-existe", root=root) is None


# --- contenido bajo demanda ----------------------------------------------------------------------

def _result_id(conn):
    return conn.execute("SELECT id FROM events WHERE kind = 'tool_result' "
                        "AND tool_use_id = 'toolu_s1'").fetchone()[0]


def test_salida_de_herramienta_se_devuelve_como_texto_literal(conn, root):
    rid = _result_id(conn)
    assert _content(conn, root, [rid]) == {rid: {"ok": True, "text": SCRIPT, "truncated": False}}


def test_uuid_que_no_coincide_es_contenido_no_disponible(conn, root):
    rid = _result_id(conn)
    conn.execute("UPDATE events SET uuid = 'otro' WHERE id = ?", (rid,))
    assert _content(conn, root, [rid]) == {rid: {"ok": False}}


def test_fichero_desaparecido_es_contenido_no_disponible(conn, root):
    rid = _result_id(conn)
    (root / "proj" / "sess-A" / "subagents" / "agent-abc.jsonl").unlink()
    assert _content(conn, root, [rid]) == {rid: {"ok": False}}


def test_ruta_fuera_de_la_raiz_no_se_lee(conn, root, tmp_path):
    # defensa en profundidad: aunque la caché dijera otra ruta, solo se lee bajo la raíz
    rid = _result_id(conn)
    outside = tmp_path / "fuera.jsonl"
    shutil.copy(root / "proj" / "sess-A" / "subagents" / "agent-abc.jsonl", outside)
    conn.execute("UPDATE events SET file_path = ? WHERE id = ?", (str(outside), rid))
    assert _content(conn, root, [rid]) == {rid: {"ok": False}}


def test_id_desconocido_y_contenido_largo(conn, root):
    assert _content(conn, root, [999999]) == {999999: {"ok": False}}
    sub = root / "proj" / "sess-A" / "subagents" / "agent-abc.jsonl"
    _append(sub, {"type": "user", "uuid": "big", "timestamp": "2026-01-01T10:00:18.000Z",
                  "message": {"role": "user", "content": [{"type": "tool_result",
                              "tool_use_id": "t_big", "content": "y" * 50_000}]}})
    scan(conn, root)
    big = conn.execute("SELECT id FROM events WHERE uuid = 'big'").fetchone()[0]
    got = _content(conn, root, [big])[big]
    assert got["truncated"] and len(got["text"]) == views.CONTENT_MAX_CHARS


def test_tool_use_muestra_nombre_y_argumentos(conn, root):
    tid = conn.execute("SELECT id FROM events WHERE kind = 'tool_use' "
                       "AND tool_use_id = 'toolu_s1'").fetchone()[0]
    got = _content(conn, root, [tid])[tid]
    assert got["ok"] and got["text"].startswith("Grep\n")


def test_desde_una_posicion_hasta_el_final(conn, root):
    # Tras "cargar anteriores", cada tick pide desde la posición más antigua cargada: lo ya
    # cargado no se pierde y lo nuevo llega por el final (la timeline solo crece por el final).
    full = views.agent_view(conn, "sess-A", "main", root=root)
    got = views.agent_view(conn, "sess-A", "main", root=root, limit=2, start_at=1)
    assert got["start"] == 1 and got["items"] == full["items"][1:]


def test_agente_reanudado_devuelve_lo_ultimo_no_lo_primero(tmp_path, root):
    # Primer plano: el padre recibió "hecho" (10:00:20). Luego se reanudó (SendMessage), volvió a
    # trabajar y terminó (notificación): lo devuelto es su último texto, no el resultado viejo.
    main = root / "proj" / "sess-A.jsonl"
    main.write_text(main.read_text(encoding="utf-8").replace(
        '"toolUseResult":{"isAsync":true,"status":"async_launched","agentId":"abc",'
        '"description":"Buscar x"},', ""), encoding="utf-8", newline="\n")
    sub = root / "proj" / "sess-A" / "subagents" / "agent-abc.jsonl"
    _append(sub, {"type": "user", "uuid": "p9", "timestamp": "2026-01-01T10:00:25.000Z",
                  "message": {"role": "user", "content": "y ahora busca y"}})
    _append(sub, {"type": "assistant", "uuid": "a9", "requestId": "req_s9",
                  "timestamp": "2026-01-01T10:00:26.000Z", "message": {
                      "model": "claude-haiku-4-5-20251001", "stop_reason": "end_turn",
                      "content": [{"type": "text", "text": "y tampoco aparece."}],
                      "usage": _usage(5)}})
    c = db.connect(tmp_path / "t3.db")
    scan(c, root)
    j = views.agent_view(c, "sess-A", "abc", root=root)
    assert j["agent"]["state"] == "done"            # la notificación (10:00:30) es posterior
    assert j["result"]["source"] == "final_text"
    assert _content(c, root, [j["result"]["id"]])[j["result"]["id"]]["text"] == "y tampoco aparece."


def test_un_turno_es_exactamente_una_peticion(conn, root):
    # Definición de "turno" (design.md §7.3), la misma para la vista de juicio y para F6:
    # un requestId = un turno; sus text/tool_use dentro; cada tool_result con su tool_use.
    for aid in ("main", "abc"):
        j = views.agent_view(conn, "sess-A", aid, root=root)
        turns = [i for i in j["items"] if i["kind"] == "turn"]
        rids = [t["request_id"] for t in turns if t["request_id"]]
        assert len(rids) == len(set(rids))                         # ninguna petición partida
        expected = {r for r, in conn.execute(
            "SELECT DISTINCT request_id FROM events WHERE session_id = 'sess-A' AND agent_id = ? "
            "AND kind IN ('text', 'tool_use') AND request_id IS NOT NULL", (aid,))}
        assert set(rids) == expected                               # ni dos juntas, ni ninguna fuera
        for t in turns:
            for e in t["events"]:
                rid, tuid = conn.execute("SELECT request_id, tool_use_id FROM events WHERE id = ?",
                                         (e["id"],)).fetchone()
                assert rid == t["request_id"]
                if e.get("result"):
                    assert conn.execute("SELECT tool_use_id FROM events WHERE id = ?",
                                        (e["result"]["id"],)).fetchone()[0] == tuid
