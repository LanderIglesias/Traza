"""Casos límite del parser (design.md §6.3)."""
import json

from traza.parser import Tokens, dedupe_requests, parse_line, parse_meta, tokens_by_model


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


def test_resultado_de_lanzar_un_subagente_dice_a_quien_y_en_que_estado():
    # toolUseResult {agentId, status}: "async_launched" = sigue trabajando (segundo plano),
    # "completed" = primer plano terminado, "forked" = skill fork terminado. findings.md §F4.
    def ev(content, **extra):
        return [(e.agent_ref, e.agent_status) for e in parse_line(line(
            type="user", uuid="u", timestamp="t",
            message={"role": "user", "content": content}, **extra)).events]
    one = [{"type": "tool_result", "tool_use_id": "t1", "content": "x"}]
    assert ev(one, toolUseResult={"agentId": "a1", "status": "async_launched"}) == [
        ("a1", "async_launched")]
    assert ev(one, toolUseResult="Error: texto") == [(None, None)]      # a veces es texto
    two = one + [{"type": "tool_result", "tool_use_id": "t2", "content": "y"}]
    # toolUseResult es de la línea: con dos resultados no se sabe a cuál se refiere
    assert ev(two, toolUseResult={"agentId": "a1", "status": "completed"}) == [
        (None, None), (None, None)]


def test_notificacion_de_tarea_dice_que_agente_termino():
    def ev(text):
        return [(e.kind, e.origin, e.agent_ref, e.agent_status) for e in parse_line(line(
            type="user", uuid="u", timestamp="t", origin={"kind": "task-notification"},
            message={"role": "user", "content": text})).events]
    n = ("<task-notification>\n<task-id>a97</task-id>\n<tool-use-id>toolu_1</tool-use-id>\n"
         "<status>completed</status>\n<summary>Agent \"x\" finished</summary>")
    assert ev(n) == [("prompt", "task-notification", "a97", "completed")]
    # resumen de varias tareas (sesión anterior): no se atribuye a ninguna
    many = "<task-notification><task-id>a1</task-id><task-id>a2</task-id><status>stopped</status>"
    assert ev(many) == [("prompt", "task-notification", None, "stopped")]
    # un prompt humano que cita esas etiquetas no es una notificación
    human = parse_line(line(type="user", uuid="u", timestamp="t", origin={"kind": "human"},
                            message={"role": "user", "content": n})).events[0]
    assert (human.agent_ref, human.agent_status) == (None, None)


def test_output_de_una_peticion_es_el_mayor_visto_no_el_primero():
    # En disco (374 peticiones): el mismo requestId en varias líneas con output_tokens que CRECE
    # (8 → 257); input y caché iguales. "Gana la primera" contaba 1.827 tokens en vez de 207.240.
    def req(out):
        return parse_line(line(type="assistant", requestId="r", uuid="u", timestamp="t", message={
            "model": "claude-sonnet-5", "content": [{"type": "text", "text": "x"}],
            "usage": {"input_tokens": 2, "output_tokens": out, "cache_read_input_tokens": 5,
                      "cache_creation": {"ephemeral_5m_input_tokens": 1,
                                         "ephemeral_1h_input_tokens": 0}}})).request
    assert dedupe_requests([req(8), req(8), req(257)])["r"].tokens == Tokens(2, 257, 5, 1, 0)
    assert dedupe_requests([req(257), req(8)])["r"].tokens.output == 257   # sin depender del orden


def test_stop_reason_de_una_peticion_es_el_ultimo_no_nulo():
    # Se escribe mientras se genera: thinking (None) → text (None) → tool_use ("tool_use").
    def req(stop):
        return parse_line(line(type="assistant", requestId="r", uuid="u", timestamp="t", message={
            "model": "m", "content": [], "stop_reason": stop})).request
    assert dedupe_requests([req(None), req("tool_use"), req(None)])["r"].stop_reason == "tool_use"


def test_notificacion_encolada_llega_como_attachment():
    # Si el agente está ocupado cuando termina un subagente, Claude Code (2.1.284) no escribe un
    # prompt: encola la notificación y la entrega como attachment "queued_command".
    xml = "<task-notification><task-id>a903</task-id><status>completed</status>"
    p = parse_line(line(type="attachment", uuid="u", timestamp="t", attachment={
        "type": "queued_command", "prompt": xml, "commandMode": "task-notification",
        "origin": {"kind": "task-notification"}}))
    assert [(e.kind, e.agent_ref, e.agent_status) for e in p.events] == [
        ("task_notification", "a903", "completed")]
    assert p.ignored is None
    # cualquier otro attachment se sigue ignorando (solo se cuenta)
    other = parse_line(line(type="attachment", uuid="u", timestamp="t",
                            attachment={"type": "queued_command", "prompt": "hola",
                                        "commandMode": "prompt"}))
    assert (other.events, other.ignored) == ([], "attachment")


def test_notificacion_en_bloques_de_texto():
    p = parse_line(line(type="user", uuid="u", timestamp="t", origin={"kind": "task-notification"},
                        message={"role": "user", "content": [{"type": "text", "text":
                            "<task-notification><task-id>a1</task-id><status>failed</status>"}]}))
    assert (p.events[0].agent_ref, p.events[0].agent_status) == ("a1", "failed")


def test_caracteres_escritos_por_el_modelo():
    # Para la comprobación de plausibilidad de output_tokens (design.md §8): cuánto escribió.
    p = parse_line(line(type="assistant", requestId="r", uuid="u", timestamp="t", message={
        "model": "m", "content": [{"type": "text", "text": "hola"},
                                  {"type": "tool_use", "id": "t", "name": "Bash",
                                   "input": {"command": "ls"}},
                                  {"type": "thinking", "thinking": ""}]}))
    assert [(e.kind, e.chars) for e in p.events] == [
        ("text", 4), ("tool_use", len("Bash") + len('{"command": "ls"}'))]


def test_cost_state_se_lee_para_el_coste_interno():
    # cost-state registra el gasto del proceso por modelo, incluidas las llamadas internas de
    # Claude Code (p. ej. Haiku para títulos) que no se escriben como peticiones (findings §4).
    p = parse_line(line(type="cost-state", startTime=123, modelUsage={
        "claude-haiku-4-5-20251001": {"costUSD": 0.0179, "inputTokens": 1},
        "claude-sonnet-5": {"costUSD": 2.0}}))
    assert p.cost_state == (123, {"claude-haiku-4-5-20251001": 0.0179, "claude-sonnet-5": 2.0})
    assert p.ignored is None
    # forma rara: no se inventa una medida
    raro = parse_line(line(type="cost-state", startTime="x", modelUsage=[]))
    assert raro.cost_state is None and raro.ignored == "cost-state"


def test_cost_state_con_otro_formato_se_ve_como_unknown():
    # Si Claude Code renombrara costUSD, el coste interno no puede desaparecer en silencio:
    # la línea cuenta como unknown y la barra de salud se pone en rojo (revisión de 4b2ab68).
    p = parse_line(line(type="cost-state", startTime=1, modelUsage={
        "claude-haiku-4-5-20251001": {"costUsd": 0.02}}))
    assert [e.kind for e in p.events] == ["unknown"] and p.cost_state is None
    # sin modelos (un proceso que aún no ha gastado) es legítimo: medida vacía
    assert parse_line(line(type="cost-state", startTime=1, modelUsage={})).cost_state == (1, {})


def test_cost_state_no_finito_o_negativo_no_es_una_medida():
    # json.loads acepta NaN, Infinity y 1e999: un inf llegaba a internal_cost (share = inf/inf =
    # NaN) y /api/overview daba 500 (allow_nan=False). Un coste así no es medida: se descarta, y
    # si no queda ninguno, unknown como cualquier formato raro (auditoría F7).
    p = parse_line(line(type="cost-state", startTime=1, modelUsage={
        "a": {"costUSD": float("inf")}, "b": {"costUSD": float("nan")},
        "c": {"costUSD": -1.0}, "d": {"costUSD": 0.5}, "e": {"costUSD": 1e308}}))
    assert p.cost_state == (1, {"d": 0.5})
    solo_inf = parse_line('{"type": "cost-state", "startTime": 1, "modelUsage": {"a": {"costUSD": 1e999}}}\n')
    assert [e.kind for e in solo_inf.events] == ["unknown"] and solo_inf.cost_state is None
