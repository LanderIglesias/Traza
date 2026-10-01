"""Parser puro: una línea JSONL de Claude Code → eventos, petición, título o tipo ignorado.

Sin I/O ni estado. Las reglas están en docs/design.md §6.3.
"""
import dataclasses
import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import NamedTuple

# Tipos que se conocen y no se muestran en v1: solo se cuentan (design.md §6.3).
IGNORED_TYPES = frozenset({
    "attachment", "queue-operation", "last-prompt", "mode", "file-history-snapshot",
    "file-history-delta", "bridge-session", "atis-latch", "frame-link",
    "artifact-comment-monitor", "artifact-autoreact-ledger",
})
SHOWN_SYSTEM_SUBTYPES = frozenset({"api_error", "compact_boundary"})


class Tokens(NamedTuple):
    input: int | None
    output: int | None
    cache_read: int | None
    cache_write_5m: int | None
    cache_write_1h: int | None


@dataclass
class Request:
    request_id: str
    model: str | None
    timestamp: str | None
    tokens: Tokens              # todo None si la línea no trae usage: "no lo sé", nunca 0
    speed: str | None
    inference_geo: str | None
    web_search_requests: int | None
    stop_reason: str | None


@dataclass
class Event:
    kind: str
    block: int                  # índice en message.content (0 si la línea no tiene bloques)
    uuid: str | None
    timestamp: str | None
    request_id: str | None = None
    tool_name: str | None = None
    tool_use_id: str | None = None
    is_error: bool | None = None
    input_hash: str | None = None
    origin: str | None = None   # solo prompt: "human", "task-notification", "meta" (inyectado)…
    denial: str | None = None   # solo tool_result con error: toolDenialKind (no se ejecutó)
    agent_ref: str | None = None     # subagente al que se refiere (lanzamiento o notificación)
    agent_status: str | None = None  # async_launched, completed, forked, failed, stopped…
    chars: int | None = None    # text / tool_use: caracteres escritos por el modelo (plausibilidad)


@dataclass
class AgentMeta:
    agent_type: str | None
    description: str | None
    parent_tool_use_id: str | None
    spawn_depth: int | None


@dataclass
class Parsed:
    events: list[Event] = field(default_factory=list)
    request: Request | None = None
    title: str | None = None
    ignored: str | None = None  # tipo (o "system:<subtipo>") si la línea se ignora
    timestamp: str | None = None  # de cualquier línea, también las ignoradas (inicio de sesión)
    # cost-state: (startTime en ms, {modelo: costUSD}) — gasto del proceso, incluidas las
    # llamadas internas de Claude Code que no se escriben como peticiones (coste interno)
    cost_state: tuple[int, dict[str, float]] | None = None


def input_hash(obj) -> str:
    """Hash del JSON canónico (claves ordenadas, sin espacios) para detectar bucles."""
    canon = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canon.encode()).hexdigest()[:16]


_INT64 = 2 ** 63


def _str(x) -> str | None:
    """Solo texto sale del parser: un dict o lista donde se espera texto sería una fila que SQLite
    rechaza, y el watcher repetiría ese tick para siempre."""
    return x if isinstance(x, str) else None


def _int(x) -> int | None:
    """Entero que cabe en SQLite (64 bits); cualquier otra cosa es "no lo sé"."""
    return x if type(x) is int and -_INT64 <= x < _INT64 else None


def _modifier(x) -> str | None:
    """speed / inference_geo: ausente = None; valor raro = "?" para que el coste salga "?" y no
    se cobre como estándar."""
    return None if x is None else (x if isinstance(x, str) else "?")


def prompt_text(content) -> str:
    """Texto de un prompt: cadena, o los bloques `text` de una lista (imágenes fuera)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(b["text"] for b in content
                        if isinstance(b, dict) and isinstance(b.get("text"), str))
    return ""


def _prompt_origin(d: dict, content) -> str | None:
    """De dónde viene un prompt (design.md §6.3). "interrupted" y "local-command" son líneas que el
    modelo no contesta (en disco: 1 de 9 y 0 de 21), así que no dejan al agente "pensando"."""
    head = prompt_text(content).lstrip()
    if head.startswith("[Request interrupted"):
        return "interrupted"
    # solo si la línea EMPIEZA así: un prompt humano que las contiene (texto pegado) es humano
    if head.startswith(("<local-command-stdout>", "<command-name>")):
        return "local-command"
    if d.get("isMeta"):
        return "meta"
    o = d.get("origin")
    return _str(o.get("kind")) if isinstance(o, dict) else None


def _task_notification(content) -> tuple[str | None, str | None]:
    """(agente, estado) de un prompt <task-notification>. Si nombra varias tareas (resumen de
    una sesión anterior) no se atribuye a ninguna."""
    text = prompt_text(content)
    ids = re.findall(r"<task-id>([^<]*)</task-id>", text)
    status = re.search(r"<status>([^<]*)</status>", text)
    return (ids[0] if len(ids) == 1 else None), (status.group(1) if status else None)


def parse_line(line: str) -> Parsed:
    """Nunca lanza: una línea que no se entiende es un evento `unknown` visible, no un crash del
    watcher. Recibe solo líneas completas (con su salto de línea): una a medias daría unknown."""
    try:
        d = json.loads(line)
        p = _parse(d)
        p.timestamp = _str(d.get("timestamp"))
        return p
    except Exception:  # JSON roto, forma inesperada, anidamiento absurdo (RecursionError)…
        return Parsed(events=[Event("unknown", 0, None, None)])


def _parse(d) -> Parsed:
    if not isinstance(d, dict):
        raise TypeError("la línea no es un objeto")
    t, uuid, ts = d.get("type"), _str(d.get("uuid")), _str(d.get("timestamp"))
    if t == "cost-state":
        mu, start = d.get("modelUsage"), _int(d.get("startTime"))
        if not isinstance(mu, dict) or start is None:
            return Parsed(ignored="cost-state")       # forma rara: no se inventa una medida
        return Parsed(cost_state=(start, {
            m: float(v["costUSD"]) for m, v in mu.items()
            if isinstance(m, str) and isinstance(v, dict)
            and type(v.get("costUSD")) in (int, float)}))
    a = d.get("attachment")
    if (t == "attachment" and isinstance(a, dict) and a.get("type") == "queued_command"
            and a.get("commandMode") == "task-notification"):
        # fin de un subagente entregado con el agente ocupado (encolado): no es un prompt
        ref, status = _task_notification(a.get("prompt"))
        return Parsed(events=[Event("task_notification", 0, uuid, ts,
                                    agent_ref=ref, agent_status=status)])
    if t in IGNORED_TYPES:
        return Parsed(ignored=t)
    if t in ("ai-title", "custom-title"):
        title = d.get("aiTitle" if t == "ai-title" else "customTitle")
        if not isinstance(title, str):
            raise TypeError("título sin texto")
        return Parsed(title=title)
    if t == "system":
        sub = d.get("subtype")
        if sub in SHOWN_SYSTEM_SUBTYPES:
            return Parsed(events=[Event(sub, 0, uuid, ts)])
        return Parsed(ignored=f"system:{sub}")

    msg = d.get("message")
    if t in ("user", "assistant") and not isinstance(msg, dict):
        raise TypeError("user/assistant sin message")
    content = msg.get("content") if isinstance(msg, dict) else None
    if t == "user":
        results = [(i, b) for i, b in enumerate(content)
                   if isinstance(b, dict) and b.get("type") == "tool_result"] \
            if isinstance(content, list) else []
        if not results:  # prompt: un evento por línea; el contenido completo se lee bajo demanda
            origin = _prompt_origin(d, content)
            ref, status = (_task_notification(content) if origin == "task-notification"
                           else (None, None))
            return Parsed(events=[Event("prompt", 0, uuid, ts, origin=origin,
                                        agent_ref=ref, agent_status=status)])
        # toolDenialKind es de la línea, pero solo describe a los resultados con error
        denial = _modifier(d.get("toolDenialKind"))
        # toolUseResult también es de la línea: solo se atribuye si hay un único resultado
        tur = d.get("toolUseResult")
        ref, status = ((_str(tur.get("agentId")), _str(tur.get("status")))
                       if isinstance(tur, dict) and len(results) == 1 else (None, None))
        return Parsed(events=[
            Event("tool_result", i, uuid, ts, tool_use_id=_str(b.get("tool_use_id")),
                  is_error=bool(b.get("is_error")),
                  denial=denial if b.get("is_error") else None,
                  agent_ref=ref, agent_status=status) for i, b in results])
    if t == "assistant":
        rid = d.get("requestId")
        if not isinstance(content, list) or not isinstance(rid, (str, type(None))):
            raise TypeError("assistant con content o requestId de forma inesperada")
        events = []
        for i, b in enumerate(content):
            bt = b.get("type") if isinstance(b, dict) else None  # bloque raro → unknown
            if bt == "thinking":
                continue  # texto vacío en disco: no hay nada que mostrar
            if bt == "text":
                text = b.get("text")
                events.append(Event("text", i, uuid, ts, request_id=rid,
                                    chars=len(text) if isinstance(text, str) else None))
            elif bt == "tool_use":
                name = _str(b.get("name"))
                events.append(Event("tool_use", i, uuid, ts, request_id=rid,
                                    tool_name=name, tool_use_id=_str(b.get("id")),
                                    input_hash=input_hash(b.get("input")),
                                    chars=len(name or "") + len(json.dumps(b.get("input"),
                                                                           ensure_ascii=False))))
            else:
                events.append(Event("unknown", i, uuid, ts, request_id=rid))
        return Parsed(events=events, request=_request(rid, msg, ts) if rid else None)

    return Parsed(events=[Event("unknown", 0, uuid, ts)])


def _request(rid: str, msg: dict, ts: str | None) -> Request:
    model, stop = _str(msg.get("model")), _str(msg.get("stop_reason"))
    u = msg.get("usage")
    if not isinstance(u, dict):
        return Request(rid, model, ts, Tokens(None, None, None, None, None),
                       None, None, None, stop)
    cc = u.get("cache_creation") or {}
    return Request(
        request_id=rid,
        model=model,
        timestamp=ts,
        tokens=Tokens(_int(u.get("input_tokens")), _int(u.get("output_tokens")),
                      _int(u.get("cache_read_input_tokens")),
                      _int(cc.get("ephemeral_5m_input_tokens")),
                      _int(cc.get("ephemeral_1h_input_tokens"))),
        speed=_modifier(u.get("speed")),
        inference_geo=_modifier(u.get("inference_geo")),
        web_search_requests=_int((u.get("server_tool_use") or {}).get("web_search_requests")),
        stop_reason=stop,
    )


def dedupe_requests(requests) -> dict[str, Request]:
    """Una petición por requestId. Claude Code repite el usage en cada línea de la respuesta, pero
    output_tokens CRECE mientras la escribe (8 → 257; findings.md §F4): se queda el mayor visto,
    sea cual sea el orden. stop_reason: el último no nulo (solo lo lleva la línea que lo sabe).
    El resto de campos no cambia entre líneas: los de la primera."""
    out = {}
    for r in requests:
        first = out.setdefault(r.request_id, r)
        o, new = first.tokens.output, r.tokens.output
        if new is not None and (o is None or new > o):
            first = dataclasses.replace(first, tokens=first.tokens._replace(output=new))
        if r.stop_reason is not None:
            first = dataclasses.replace(first, stop_reason=r.stop_reason)
        out[r.request_id] = first
    return out


def tokens_by_model(requests) -> dict[str, Tokens]:
    """Suma por modelo. Si alguna petición no sabe un campo (None), la suma tampoco lo sabe."""
    acc: dict[str, list] = {}
    for r in requests:
        cur = acc.setdefault(r.model, [0] * len(Tokens._fields))
        for i, v in enumerate(r.tokens):
            cur[i] = None if v is None or cur[i] is None else cur[i] + v
    return {m: Tokens(*v) for m, v in acc.items()}


def parse_meta(text: str) -> AgentMeta | None:
    """agent-<id>.meta.json de un subagente. None si está roto o a medio escribir: el watcher
    lo reintenta en vez de concluir que no dice quién lo lanzó."""
    try:
        m = json.loads(text)
    except ValueError:
        return None
    if not isinstance(m, dict):
        return None
    return AgentMeta(_str(m.get("agentType")), _str(m.get("description")),
                     _str(m.get("toolUseId")), _int(m.get("spawnDepth")))
