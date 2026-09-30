"""Casos límite del parser (design.md §6.3)."""
import json

from traza.parser import Tokens, parse_line, parse_meta, tokens_by_model


def line(**d):
    return json.dumps(d)


def test_assistant_sin_usage_da_tokens_none_no_cero():
    p = parse_line(line(type="assistant", requestId="r", uuid="u", timestamp="t",
                        message={"model": "claude-sonnet-5", "content": []}))
    assert p.request.tokens == Tokens(None, None, None, None, None)
    # y la suma por modelo tampoco se inventa un 0
    assert tokens_by_model([p.request])["claude-sonnet-5"].output is None


def test_json_roto_es_unknown_visible():
    p = parse_line('{"type": "assistant", "mess')
    assert [e.kind for e in p.events] == ["unknown"]


def test_tipo_desconocido_dentro_de_assistant_es_unknown():
    p = parse_line(line(type="assistant", uuid="u", timestamp="t",
                        message={"content": [{"type": "server_tool_use"}]}))
    assert [(e.kind, e.block) for e in p.events] == [("unknown", 0)]


def test_prompt_guarda_su_origen():
    def origin(**extra):
        p = parse_line(line(type="user", uuid="u", timestamp="t",
                            message={"role": "user", "content": "x"}, **extra))
        return p.events[0].origin
    assert origin(origin={"kind": "human"}) == "human"
    assert origin(origin={"kind": "task-notification"}) == "task-notification"
    assert origin(isMeta=True) == "meta"            # texto que inyecta Claude Code
    assert origin(isMeta=True, origin={"kind": "peer"}) == "meta"
    assert origin(origin={"kind": "peer"}) == "peer"
    # Sin origin ni isMeta → NULL. En disco: encargo de subagente, prompt del SDK,
    # "[Request interrupted by user]" o resumen de compactación. Ninguno es "human".
    assert origin() is None
    assert origin(promptSource="sdk") is None


def test_custom_title():
    assert parse_line(line(type="custom-title", customTitle="Mío")).title == "Mío"


def test_meta_json_de_subagente():
    m = parse_meta(json.dumps({"agentType": "Explore", "description": "Buscar x",
                               "toolUseId": "toolu_9", "spawnDepth": 2, "otro": 1}))
    assert (m.agent_type, m.description, m.parent_tool_use_id, m.spawn_depth) == (
        "Explore", "Buscar x", "toolu_9", 2)


def test_meta_json_roto_no_rompe():
    m = parse_meta("{no es json")
    assert (m.agent_type, m.parent_tool_use_id) == (None, None)


def test_bloque_que_no_es_objeto_no_rompe_el_parser():
    # Una sola línea rara no puede tumbar al watcher: se ve como unknown.
    u = parse_line(line(type="user", uuid="u", timestamp="t", message={"content": ["suelto"]}))
    a = parse_line(line(type="assistant", uuid="u", timestamp="t", message={"content": [7]}))
    assert [e.kind for e in u.events] == ["prompt"]
    assert [(e.kind, e.block) for e in a.events] == [("unknown", 0)]


def test_json_valido_con_forma_rara_es_unknown_nunca_excepcion():
    raras = [
        '{"type": []}',
        '{"type": "system", "subtype": {}}',
        line(type="assistant", requestId="r", message={"content": [], "usage": {"cache_creation": 5}}),
        line(type="assistant", requestId="r", message={"content": [], "usage": {"server_tool_use": 1}}),
        line(type="assistant", requestId=["r"], message={"content": []}),
        "[" * 100_000 + "]" * 100_000,
    ]
    for raw in raras:
        p = parse_line(raw)
        assert [e.kind for e in p.events] == ["unknown"] and p.request is None, raw[:60]


def test_lineas_sin_lo_esencial_son_unknown_no_desaparecen():
    for raw in [line(type="user", uuid="u"),                       # user sin message
                line(type="assistant", uuid="u", message={"content": "texto"}),
                line(type="ai-title"), line(type="custom-title", customTitle=3)]:
        assert [e.kind for e in parse_line(raw).events] == ["unknown"], raw
