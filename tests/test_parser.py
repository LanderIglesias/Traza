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
    assert parse_meta("{no es json") is None   # a medio escribir: se reintenta
    assert parse_meta("[1, 2]") is None


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


def test_prompts_que_el_modelo_nunca_contesta_tienen_su_origen():
    # En disco: el modelo contesta 0 de 21 salidas de comandos locales y 1 de 9 interrupciones.
    def origin(content, **extra):
        return parse_line(line(type="user", uuid="u", timestamp="t",
                               message={"role": "user", "content": content}, **extra)).events[0].origin
    assert origin("[Request interrupted by user]") == "interrupted"
    assert origin([{"type": "text", "text": "[Request interrupted by user for tool use]"}]) == "interrupted"
    assert origin("<local-command-stdout>Set model to opus</local-command-stdout>") == "local-command"
    assert origin("<command-name>/cost</command-name>", isMeta=True) == "local-command"
    assert origin("texto de una skill", isMeta=True) == "meta"      # este sí lo contesta (92 %)


def test_command_name_solo_cuenta_al_principio():
    # Un prompt humano que CONTIENE la etiqueta (texto pegado) sigue siendo del usuario.
    p = parse_line(line(type="user", uuid="u", timestamp="t", origin={"kind": "human"},
                        message={"role": "user", "content": "mira esto: <command-name>/x</command-name>"}))
    assert p.events[0].origin == "human"


def test_resultado_denegado_se_distingue_de_un_error_real():
    # toolDenialKind (en disco desde 2.1.259) = la herramienta NO se ejecutó: hook que bloquea
    # (con o sin prefijo "PreToolUse:… hook error:"), regla de permisos, usuario, auto mode.
    # Sin él, is_error = la herramienta se ejecutó y falló. findings.md §F4.
    def results(**extra):
        return [(e.is_error, e.denial) for e in parse_line(line(
            type="user", uuid="u", timestamp="t", message={"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "a", "is_error": True, "content": "no"},
                {"type": "tool_result", "tool_use_id": "b", "content": "ok"}]}, **extra)).events]
    assert results(toolDenialKind="permission-rule") == [(True, "permission-rule"), (False, None)]
    assert results() == [(True, None), (False, None)]            # error real
    assert results(toolDenialKind={"x": 1}) == [(True, "?"), (False, None)]   # raro: denegado, "?"
