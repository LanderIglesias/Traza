"""F2: caché SQLite + watcher (design.md §5, §6.2–6.4). Todo sobre copias en tmp_path."""
import json
import os
from contextlib import closing
import shutil
from pathlib import Path

import pytest

from traza import db
from traza.parser import Tokens, tokens_by_model
from traza.watcher import scan

FIX = Path(__file__).parent / "fixtures"


@pytest.fixture
def root(tmp_path):
    projects = tmp_path / "projects"
    shutil.copytree(FIX, projects / "proj")
    return projects


@pytest.fixture
def conn(tmp_path):
    return db.connect(tmp_path / "traza.db")


def counts(conn):
    return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in ("files", "sessions", "agents", "requests", "request_refs", "events")}


def kinds(conn, file_name):
    return [r[0] for r in conn.execute(
        "SELECT kind FROM events WHERE file_path LIKE ? ORDER BY byte_offset, block",
        (f"%{file_name}",))]


def test_scan_ingesta_la_fixture_y_cuadra_con_el_parser(root, conn):
    scan(conn, root)
    assert counts(conn) == {"files": 3, "sessions": 2, "agents": 3, "requests": 6,
                            "request_refs": 7, "events": 24}
    assert tokens_by_model(db.requests(conn)) == {
        "claude-sonnet-5": Tokens(19, 81, 300, 30, 40),
        "claude-haiku-4-5-20251001": Tokens(10, 14, 0, 0, 0),
    }
    assert db.ignored_counts(conn)["attachment"] == 1
    assert conn.execute("SELECT title FROM sessions WHERE session_id='sess-A'").fetchone()[0] \
        == "Listar ficheros"


def test_scan_dos_veces_no_duplica_ni_relee(root, conn):
    first = scan(conn, root)
    before = counts(conn)
    second = scan(conn, root)
    assert counts(conn) == before
    assert first["files_read"] == 3 and second["files_read"] == 0


def test_linea_a_medias_se_lee_cuando_se_completa(root, conn):
    f = root / "proj" / "live.jsonl"
    full = json.dumps({"type": "user", "uuid": "l2", "timestamp": "2026-01-02T00:00:01.000Z",
                       "message": {"role": "user", "content": "segunda"}})
    first = json.dumps({"type": "user", "uuid": "l1", "timestamp": "2026-01-02T00:00:00.000Z",
                        "message": {"role": "user", "content": "primera"}})
    f.write_text(first + "\n" + full[:20], encoding="utf-8", newline="\n")
    scan(conn, root)
    assert kinds(conn, "live.jsonl") == ["prompt"]          # la línea a medias no entra
    with open(f, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(full[20:] + "\n")
    scan(conn, root)
    assert kinds(conn, "live.jsonl") == ["prompt", "prompt"]  # ni unknown ni duplicado
    uuids = [r[0] for r in conn.execute(
        "SELECT uuid FROM events WHERE file_path LIKE '%live.jsonl' ORDER BY byte_offset")]
    assert uuids == ["l1", "l2"]


def test_truncado_reprocesa_desde_cero(root, conn):
    scan(conn, root)
    f = root / "proj" / "sess-B.jsonl"
    f.write_text(f.read_text(encoding="utf-8").splitlines()[0] + "\n", encoding="utf-8",
                 newline="\n")
    scan(conn, root)
    assert kinds(conn, "sess-B.jsonl") == ["prompt"]
    assert "req_9" not in {r.request_id for r in db.requests(conn)}


def test_borrado_en_cascada_y_reasignacion_de_duena(root, conn):
    scan(conn, root)
    assert db.owners(conn)["req_1"] == "sess-A"
    shutil.rmtree(root / "proj" / "sess-A")
    (root / "proj" / "sess-A.jsonl").unlink()
    scan(conn, root)
    assert kinds(conn, "sess-A.jsonl") == []
    assert [r[0] for r in conn.execute("SELECT session_id FROM sessions")] == ["sess-B"]
    # req_1 sigue viva en la copia y pasa a ser suya; las exclusivas de A desaparecen
    assert db.owners(conn) == {"req_1": "sess-B", "req_9": "sess-B"}


def test_duena_no_depende_del_orden_de_ingesta(root, conn):
    a = root.parent / "aparte"
    shutil.move(root / "proj" / "sess-A.jsonl", a)
    scan(conn, root)                       # primero solo la copia
    assert db.owners(conn)["req_1"] == "sess-B"
    shutil.move(a, root / "proj" / "sess-A.jsonl")
    scan(conn, root)                       # aparece la original, más antigua
    assert db.owners(conn)["req_1"] == "sess-A"
    assert db.inherited(conn, "sess-B") == {"sess-A": 1}
    assert db.inherited(conn, "sess-A") == {}


def test_jerarquia_anidada_y_huerfano(root, conn):
    subs = root / "proj" / "sess-A" / "subagents"
    # nieto: lo lanzó el subagente abc con toolu_s1
    (subs / "agent-def.jsonl").write_text("", encoding="utf-8")
    (subs / "agent-def.meta.json").write_text(json.dumps({"agentType": "x", "toolUseId": "toolu_s1"}))
    # huérfano: su toolUseId no está en ningún fichero de la sesión
    (subs / "agent-zzz.jsonl").write_text("", encoding="utf-8")
    (subs / "agent-zzz.meta.json").write_text(json.dumps({"agentType": "y", "toolUseId": "toolu_nope"}))
    scan(conn, root)
    assert db.agent_parents(conn, "sess-A") == {"main": None, "abc": "main", "def": "abc",
                                                "zzz": None}


def test_cambio_de_parser_version_reconstruye_la_bd(tmp_path, monkeypatch):
    path = tmp_path / "traza.db"

    def gen():  # se cierra la conexión: Windows no deja borrar un fichero abierto
        with closing(db.connect(path)) as c:
            return db.generation(c)
    gen1 = gen()
    assert gen() == gen1                                    # misma versión: se reutiliza
    monkeypatch.setattr(db, "PARSER_VERSION", "otra")
    assert gen() != gen1                                    # otra versión: BD nueva


def test_duena_por_primera_linea_del_fichero_aunque_sea_ignorada(tmp_path, conn):
    # Caso real (7bc000bb / 3416476c): la copia empieza con el mismo prompt y el mismo timestamp
    # que la original; solo la primera línea (ignorada) las distingue. Y el id de la copia va
    # antes alfabéticamente, así que un desempate por id daría la dueña equivocada.
    proj = tmp_path / "projects" / "p"
    proj.mkdir(parents=True)
    prompt = {"type": "user", "uuid": "u", "timestamp": "2026-01-01T10:05:00.000Z",
              "message": {"role": "user", "content": "hola"}}
    answer = {"type": "assistant", "uuid": "a", "requestId": "req_X",
              "timestamp": "2026-01-01T10:05:01.000Z",
              "message": {"model": "claude-sonnet-5", "content": [], "usage": {}}}

    def write(name, first):
        (proj / name).write_text("".join(json.dumps(d) + "\n" for d in (first, prompt, answer)),
                                 encoding="utf-8", newline="\n")
    write("zz-original.jsonl", {"type": "attachment", "timestamp": "2026-01-01T10:00:00.000Z"})
    write("aa-copia.jsonl", {"type": "queue-operation", "timestamp": "2026-01-01T10:04:59.000Z"})
    scan(conn, tmp_path / "projects")
    assert db.owners(conn) == {"req_X": "zz-original"}


def test_valores_de_tipo_raro_no_bloquean_el_watcher(tmp_path, conn):
    # Si SQLite rechazara una fila, el tick entero se desharía y se repetiría para siempre.
    proj = tmp_path / "projects" / "p"
    proj.mkdir(parents=True)
    raras = [
        {"type": "user", "uuid": {"x": 1}, "timestamp": ["t"], "origin": {"kind": {}},
         "message": {"role": "user", "content": "hola"}},
        {"type": "assistant", "uuid": "a", "timestamp": "t", "requestId": "r1",
         "message": {"model": {"m": 1}, "stop_reason": [], "content": [
             {"type": "tool_use", "id": {}, "name": [], "input": {}}],
             "usage": {"input_tokens": 2 ** 70, "output_tokens": "5", "speed": {}}}},
    ]
    normal = {"type": "user", "uuid": "ok", "timestamp": "t", "message": {"content": "sigue"}}
    (proj / "s.jsonl").write_text("".join(json.dumps(d) + "\n" for d in raras + [normal]),
                                  encoding="utf-8", newline="\n")
    scan(conn, tmp_path / "projects")
    assert kinds(conn, "s.jsonl") == ["prompt", "tool_use", "prompt"]
    (req,) = db.requests(conn)
    assert (req.model, req.tokens.input, req.tokens.output) == (None, None, None)


def test_connect_se_niega_a_borrar_lo_que_no_es_su_cache(tmp_path):
    ajeno = tmp_path / "importante.db"
    ajeno.write_bytes(b"no soy una cache de traza")
    with pytest.raises(RuntimeError):
        db.connect(ajeno)
    assert ajeno.read_bytes() == b"no soy una cache de traza"


def test_fichero_sustituido_por_otro_mas_largo_se_reprocesa(root, conn):
    scan(conn, root)
    f = root / "proj" / "sess-B.jsonl"
    old_size = f.stat().st_size
    nuevo = [json.dumps({"type": "user", "uuid": f"n{i}", "timestamp": "2026-02-01T00:00:00.000Z",
                         "message": {"content": "x" * 2000}}) for i in range(5)]
    f.write_text("\n".join(nuevo) + "\n", encoding="utf-8", newline="\n")
    assert f.stat().st_size >= old_size  # si no, sería el caso "truncado", que ya se detecta
    scan(conn, root)
    uuids = [r[0] for r in conn.execute(
        "SELECT uuid FROM events WHERE file_path LIKE '%sess-B.jsonl' ORDER BY byte_offset")]
    assert uuids == [f"n{i}" for i in range(5)]


def _two_sessions(tmp_path, first_a, first_b):
    """Dos ficheros que comparten req_X; cada uno empieza con la línea que se le pase."""
    proj = tmp_path / "projects" / "p"
    proj.mkdir(parents=True)
    ans = {"type": "assistant", "uuid": "a", "requestId": "req_X", "timestamp": "t",
           "message": {"model": "m", "content": [], "usage": {}}}
    for name, first in (("sess-b", first_b), ("sess-a", first_a)):
        (proj / f"{name}.jsonl").write_text(
            "".join(json.dumps(d) + "\n" for d in (first, ans)), encoding="utf-8", newline="\n")
    return tmp_path / "projects"


def test_empate_exacto_desempata_por_session_id(tmp_path, conn):
    # cp manual, backup restaurado: misma primera línea. Gana el session_id menor, siempre,
    # sin depender del orden en que el glob devuelva los ficheros.
    same = {"type": "queue-operation", "timestamp": "2026-01-01T10:00:00.000Z"}
    scan(conn, _two_sessions(tmp_path, same, same))
    assert db.owners(conn) == {"req_X": "sess-a"}


def test_primera_linea_sin_timestamp_usa_la_primera_que_lo_tenga(tmp_path, conn):
    # sess-b empieza con una línea unknown (sin hora); su inicio es la siguiente con hora.
    root = _two_sessions(tmp_path, {"type": "mode", "timestamp": "2026-01-01T11:00:00.000Z"},
                         {"type": "algo-nuevo"})
    scan(conn, root)
    # sess-b: primera hora = "t" del assistant ("t" > "2026-..." en orden de texto → más tarde)
    assert db.owners(conn) == {"req_X": "sess-a"}
    starts = dict(conn.execute("SELECT session_id, first_ts FROM files"))
    assert starts == {"sess-a": "2026-01-01T11:00:00.000Z", "sess-b": "t"}


def test_sesion_sin_ninguna_hora_va_la_ultima(tmp_path, conn):
    proj = tmp_path / "projects" / "p"
    proj.mkdir(parents=True)
    ans = {"type": "assistant", "uuid": "a", "requestId": "req_X",
           "message": {"model": "m", "content": [], "usage": {}}}
    (proj / "aaa.jsonl").write_text(json.dumps(ans) + "\n", encoding="utf-8", newline="\n")
    (proj / "zzz.jsonl").write_text(
        json.dumps({"type": "mode", "timestamp": "2026-01-01T00:00:00.000Z"}) + "\n"
        + json.dumps(ans) + "\n", encoding="utf-8", newline="\n")
    scan(conn, tmp_path / "projects")
    assert db.owners(conn) == {"req_X": "zzz"}  # "aaa" no sabe cuándo empezó: no gana


def test_borrar_la_ultima_referencia_borra_la_peticion(root, conn):
    scan(conn, root)
    (root / "proj" / "sess-B.jsonl").unlink()
    scan(conn, root)
    ids = {r[0] for r in conn.execute("SELECT request_id FROM requests")}
    assert "req_9" not in ids and "req_1" in ids  # req_1 sigue viva en sess-A


def test_linea_a_medias_para_siempre_no_se_relee(tmp_path, conn):
    # Proceso muerto a mitad de escritura: la línea nunca se completa. Coste = un stat, no una
    # lectura por tick.
    proj = tmp_path / "projects" / "p"
    proj.mkdir(parents=True)
    (proj / "s.jsonl").write_text('{"type": "user", "mess', encoding="utf-8")
    assert scan(conn, tmp_path / "projects")["files_read"] == 1
    assert scan(conn, tmp_path / "projects")["files_read"] == 0


def test_dos_clases_de_huerfano(root, conn):
    subs = root / "proj" / "sess-A" / "subagents"
    (subs / "agent-fork.jsonl").write_text("", encoding="utf-8")
    (subs / "agent-fork.meta.json").write_text(json.dumps({"agentType": "general-purpose"}))
    (subs / "agent-lost.jsonl").write_text("", encoding="utf-8")
    (subs / "agent-lost.meta.json").write_text(json.dumps({"toolUseId": "toolu_nope"}))
    scan(conn, root)
    assert db.orphans(conn, "sess-A") == {"fork": "sin_tool_use_id",
                                          "lost": "padre_no_encontrado"}


# --- revisión de d5d87c4 -------------------------------------------------------------------

def test_scan_toma_el_cerrojo_antes_de_leer_offsets(root, conn):
    # Con dos escáneres a la vez (servidor + `traza.scan`), leer offsets sin cerrojo da datos
    # viejos: IntegrityError o ignorados contados dos veces.
    sql = []
    conn.set_trace_callback(sql.append)
    scan(conn, root)
    conn.set_trace_callback(None)
    assert sql[0] == "BEGIN IMMEDIATE"


def test_otro_escritor_espera_en_vez_de_fallar_enseguida(tmp_path):
    with closing(db.connect(tmp_path / "t.db")) as c:
        assert c.execute("PRAGMA busy_timeout").fetchone()[0] >= 30_000


def test_raiz_relativa_o_absoluta_es_la_misma(root, conn, monkeypatch):
    scan(conn, root)
    monkeypatch.chdir(root.parent)
    stats = scan(conn, Path("projects"))
    assert (stats["deleted"], stats["files_read"]) == (0, 0)


def test_meta_json_que_llega_tarde_se_lee_aunque_el_jsonl_no_cambie(root, conn):
    subs = root / "proj" / "sess-A" / "subagents"
    meta = subs / "agent-abc.meta.json"
    body = meta.read_text()
    meta.write_text('{"agentType": "Expl')             # a medio escribir
    scan(conn, root)
    assert db.orphans(conn, "sess-A") == {"abc": "padre_no_encontrado"}  # aún no: no "nunca"
    meta.write_text(body)                              # se completa; el jsonl no cambia
    scan(conn, root)
    assert db.agent_parents(conn, "sess-A")["abc"] == "main"
    assert db.orphans(conn, "sess-A") == {}


def test_primera_creacion_interrumpida_se_puede_reconstruir(tmp_path):
    import sqlite3
    path = tmp_path / "traza.db"
    sqlite3.connect(path).close()                      # BD vacía: lo que deja un Ctrl+C
    with closing(db.connect(path)) as c:
        assert db.generation(c)


def test_truncar_el_principal_olvida_el_titulo_viejo(root, conn):
    scan(conn, root)
    f = root / "proj" / "sess-A.jsonl"
    f.write_text(f.read_text(encoding="utf-8").splitlines()[0] + "\n", encoding="utf-8",
                 newline="\n")
    scan(conn, root)
    assert conn.execute("SELECT title FROM sessions WHERE session_id='sess-A'").fetchone()[0] is None


@pytest.mark.skipif(os.name != "nt", reason="en POSIX se puede borrar un fichero abierto")
def test_cambio_de_version_con_la_bd_abierta_da_un_error_claro(tmp_path, monkeypatch):
    path = tmp_path / "traza.db"
    with closing(db.connect(path)):
        monkeypatch.setattr(db, "PARSER_VERSION", "otra")
        with pytest.raises(RuntimeError, match="en uso"):
            db.connect(path)


@pytest.mark.skipif(os.name != "nt", reason="solo Windows ignora mayúsculas en rutas")
def test_raiz_con_otras_mayusculas_es_la_misma(root, conn):
    scan(conn, root)
    stats = scan(conn, Path(str(root).upper()))
    assert (stats["deleted"], stats["files_read"]) == (0, 0)


def test_output_que_crece_en_otro_tick_actualiza_la_peticion(root, conn):
    # La línea final de una respuesta (output completo) puede llegar en un tick posterior.
    f = root / "proj" / "live.jsonl"
    def line(out):
        return json.dumps({"type": "assistant", "uuid": f"a{out}", "requestId": "req_live",
                           "timestamp": "2026-01-02T00:00:00.000Z", "message": {
                               "model": "claude-sonnet-5", "content": [{"type": "text", "text": "x"}],
                               "stop_reason": None if out < 100 else "tool_use",
                               "usage": {"input_tokens": 2, "output_tokens": out,
                                         "cache_read_input_tokens": 0, "cache_creation": {
                                             "ephemeral_5m_input_tokens": 0,
                                             "ephemeral_1h_input_tokens": 0}}}}) + "\n"
    f.write_text(line(8), encoding="utf-8", newline="\n")
    scan(conn, root)
    with open(f, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(line(257))
    scan(conn, root)
    got = {r.request_id: r for r in db.requests(conn)}["req_live"]
    assert got.tokens.output == 257
    assert got.stop_reason == "tool_use"      # el último no nulo, llegado en el segundo tick


@pytest.mark.parametrize("output, text, flagged", [
    (1, "x" * 2000, True),     # 1 token para 2.000 caracteres: mal escrito por Claude Code
    (300, "x" * 2000, False),
    (1, "Ok.", False),          # respuesta corta de verdad: no se marca
    (None, "x" * 2000, False),  # sin usage: ya es "?", no "sospechosa"
], ids=["1-para-2000", "300-para-2000", "respuesta-corta", "sin-usage"])
def test_output_implausible_se_cuenta_no_se_corrige(root, conn, output, text, flagged):
    # 373 peticiones en disco declaran < 1 token por cada 40 caracteres escritos (p. ej. 31 tokens
    # para 10.316 caracteres de JSON). traza no puede corregir el número: lo señala.
    usage = {} if output is None else {"usage": {
        "input_tokens": 1, "output_tokens": output, "cache_read_input_tokens": 0,
        "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 0}}}
    (root / "proj" / "p.jsonl").write_text(json.dumps({
        "type": "assistant", "uuid": "a", "requestId": "req_p", "timestamp": "2026-01-02T00:00:00Z",
        "message": {"model": "claude-sonnet-5", "content": [{"type": "text", "text": text}],
                    **usage}}) + "\n", encoding="utf-8")
    scan(conn, root)
    assert ("req_p" in db.implausible_output(conn)) is flagged
    assert db.implausible_output(conn) <= {"req_p"}     # la fixture no tiene ninguna
