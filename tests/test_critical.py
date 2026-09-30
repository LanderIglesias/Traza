"""Los dos tests críticos de F1 (plan.md): si fallan, todo lo demás miente."""
from pathlib import Path

from traza.parser import Tokens, dedupe_requests, input_hash, parse_line, tokens_by_model

FIX = Path(__file__).parent / "fixtures"


def parse_file(name):
    with open(FIX / name, encoding="utf-8") as f:
        return [parse_line(line) for line in f]


def requests_of(*names):
    return dedupe_requests(p.request for n in names for p in parse_file(n) if p.request)


# --- 1. Deduplicación por requestId -------------------------------------------------------

def test_tokens_se_cuentan_una_vez_por_request_id():
    # req_1 ocupa 3 líneas con el usage repetido: sumarlas línea a línea lo triplicaría.
    reqs = requests_of("sess-A.jsonl")
    assert sorted(reqs) == ["req_1", "req_2", "req_3", "req_4"]
    assert tokens_by_model(reqs.values()) == {
        "claude-sonnet-5": Tokens(input=16, output=77, cache_read=300, cache_write_5m=30, cache_write_1h=40),
        "claude-haiku-4-5-20251001": Tokens(input=5, output=7, cache_read=0, cache_write_5m=0, cache_write_1h=0),
    }


def test_peticion_heredada_por_otra_sesion_cuenta_una_vez():
    # sess-B comparte req_1 con sess-A (copia): el total cuenta req_1 una sola vez.
    reqs = requests_of("sess-A.jsonl", "sess-B.jsonl")
    assert sorted(reqs) == ["req_1", "req_2", "req_3", "req_4", "req_9"]
    assert tokens_by_model(reqs.values())["claude-sonnet-5"] == Tokens(
        input=19, output=81, cache_read=300, cache_write_5m=30, cache_write_1h=40)


# --- 2. Parser sobre fixture con la forma real ---------------------------------------------

def test_parser_sobre_fixture_con_forma_real():
    parsed = parse_file("sess-A.jsonl")
    events = [(i, e.kind, e.block, e.tool_name, e.tool_use_id, e.is_error)
              for i, p in enumerate(parsed) for e in p.events]
    assert events == [
        (1, "prompt", 0, None, None, None),
        (4, "text", 0, None, None, None),
        (5, "tool_use", 0, "Bash", "toolu_1", None),
        (6, "tool_result", 0, None, "toolu_1", False),
        (7, "text", 0, None, None, None),          # block = índice en message.content
        (7, "text", 1, None, None, None),
        (7, "tool_use", 2, "Read", "toolu_2", None),
        (7, "tool_use", 3, "Read", "toolu_3", None),
        (8, "tool_result", 0, None, "toolu_2", True),   # un evento por tool_result
        (8, "tool_result", 1, None, "toolu_3", False),
        (9, "api_error", 0, None, None, None),
        (11, "compact_boundary", 0, None, None, None),
        (13, "text", 0, None, None, None),         # <synthetic>
        (14, "text", 0, None, None, None),         # assistant sin requestId
        (15, "prompt", 0, None, None, None),       # texto + imagen = un prompt
        (16, "unknown", 0, None, None, None),
        (18, "tool_use", 0, "Agent", "toolu_9", None),
        (19, "tool_result", 0, None, "toolu_9", False),
        (20, "text", 0, None, None, None),
    ]
    assert {i: p.ignored for i, p in enumerate(parsed) if p.ignored} == {
        0: "queue-operation", 2: "attachment", 10: "system:stop_hook_summary", 17: "cost-state"}
    assert [(i, p.title) for i, p in enumerate(parsed) if p.title] == [(12, "Listar ficheros")]
    # thinking no crea evento ni se cuenta como ignorado, pero su línea sí aporta la petición
    assert parsed[3].events == [] and parsed[3].request.request_id == "req_1"
    # sin requestId → sin fila en requests
    assert parsed[13].request is None and parsed[14].request is None
    # los eventos llevan lo necesario para casar y verificar contenido
    ev = parsed[5].events[0]
    assert (ev.uuid, ev.timestamp, ev.request_id) == ("a3", "2026-01-01T10:00:03.000Z", "req_1")
    assert ev.input_hash == input_hash({"description": "List", "command": "ls"})
    req = parsed[7].request
    assert (req.model, req.speed, req.inference_geo, req.web_search_requests, req.stop_reason) == (
        "claude-sonnet-5", "standard", "not_available", 0, "tool_use")


# --- 3. output_tokens crece dentro de una misma petición (findings.md §F4) --------------------
# El oráculo compara traza con otro camino que lee el mismo JSONL: si los dos interpretan igual
# de mal, coinciden (design.md §8). Esta regla falló desde F1 porque la fixture repetía el mismo
# output en todas las líneas: con valores CONSTANTES "la primera" y "la mayor" son lo mismo.
# Aquí los valores crecen y llegan en todos los órdenes, así que solo "la mayor" pasa.
import itertools
import json

import pytest

from traza import db
from traza.watcher import scan

ORDERS = list(itertools.permutations([100, 200, 300]))


def _line(out: int) -> str:
    return json.dumps({"type": "assistant", "uuid": f"u{out}", "requestId": "req_grow",
                       "timestamp": "2026-01-02T00:00:00.000Z", "message": {
                           "model": "claude-sonnet-5", "content": [{"type": "text", "text": "x"}],
                           "usage": {"input_tokens": 3, "output_tokens": out,
                                     "cache_read_input_tokens": 0, "cache_creation": {
                                         "ephemeral_5m_input_tokens": 0,
                                         "ephemeral_1h_input_tokens": 0}}}}) + "\n"


@pytest.mark.parametrize("order", ORDERS)
def test_output_es_el_mayor_en_el_parser(order):
    reqs = dedupe_requests(parse_line(_line(o)).request for o in order)
    assert reqs["req_grow"].tokens == Tokens(3, 300, 0, 0, 0)


@pytest.mark.parametrize("order", ORDERS)
@pytest.mark.parametrize("ticks", ["un tick", "un tick por línea"])
def test_output_es_el_mayor_en_la_cache(tmp_path, order, ticks):
    f = tmp_path / "projects" / "proj" / "grow.jsonl"
    f.parent.mkdir(parents=True)
    f.write_text("", encoding="utf-8")
    conn = db.connect(tmp_path / "traza.db")
    for o in order:
        with open(f, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(_line(o))
        if ticks == "un tick por línea":
            scan(conn, tmp_path / "projects")
    scan(conn, tmp_path / "projects")
    assert [r.tokens.output for r in db.requests(conn)] == [300]
