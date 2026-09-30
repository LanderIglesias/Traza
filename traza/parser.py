"""Parser puro: una línea JSONL de Claude Code → eventos, petición, título o tipo ignorado.

Sin I/O ni estado. Las reglas están en docs/design.md §6.3.
"""
import hashlib
import json
from dataclasses import dataclass, field
from typing import NamedTuple

# Tipos que se conocen y no se muestran en v1: solo se cuentan (design.md §6.3).
IGNORED_TYPES = frozenset({
    "attachment", "queue-operation", "last-prompt", "mode", "file-history-snapshot",
    "file-history-delta", "bridge-session", "atis-latch", "frame-link",
    "artifact-comment-monitor", "artifact-autoreact-ledger", "cost-state",
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


def input_hash(obj) -> str:
    """Hash del JSON canónico (claves ordenadas, sin espacios) para detectar bucles."""
    canon = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canon.encode()).hexdigest()[:16]


def parse_line(line: str) -> Parsed:
    try:
        d = json.loads(line)
    except ValueError:
        return Parsed(events=[Event("unknown", 0, None, None)])
    if not isinstance(d, dict):
        return Parsed(events=[Event("unknown", 0, None, None)])

    t, uuid, ts = d.get("type"), d.get("uuid"), d.get("timestamp")
    if t in IGNORED_TYPES:
        return Parsed(ignored=t)
    if t == "ai-title":
        return Parsed(title=d.get("aiTitle"))
    if t == "custom-title":
        return Parsed(title=d.get("customTitle"))
    if t == "system":
        sub = d.get("subtype")
        if sub in SHOWN_SYSTEM_SUBTYPES:
            return Parsed(events=[Event(sub, 0, uuid, ts)])
        return Parsed(ignored=f"system:{sub}")

    msg = d.get("message") if isinstance(d.get("message"), dict) else {}
    content = msg.get("content")
    if t == "user":
        results = [(i, b) for i, b in enumerate(content)
                   if isinstance(b, dict) and b.get("type") == "tool_result"] \
            if isinstance(content, list) else []
        if not results:  # prompt: un evento por línea; el contenido completo se lee bajo demanda
            o = d.get("origin")
            origin = "meta" if d.get("isMeta") else (o.get("kind") if isinstance(o, dict) else None)
            return Parsed(events=[Event("prompt", 0, uuid, ts, origin=origin)])
        return Parsed(events=[
            Event("tool_result", i, uuid, ts, tool_use_id=b.get("tool_use_id"),
                  is_error=bool(b.get("is_error"))) for i, b in results])
    if t == "assistant":
        rid = d.get("requestId")
        events = []
        for i, b in enumerate(content if isinstance(content, list) else []):
            bt = b.get("type") if isinstance(b, dict) else None  # bloque raro → unknown
            if bt == "thinking":
                continue  # texto vacío en disco: no hay nada que mostrar
            if bt == "text":
                events.append(Event("text", i, uuid, ts, request_id=rid))
            elif bt == "tool_use":
                events.append(Event("tool_use", i, uuid, ts, request_id=rid,
                                    tool_name=b.get("name"), tool_use_id=b.get("id"),
                                    input_hash=input_hash(b.get("input"))))
            else:
                events.append(Event("unknown", i, uuid, ts, request_id=rid))
        return Parsed(events=events, request=_request(rid, msg, ts) if rid else None)

    return Parsed(events=[Event("unknown", 0, uuid, ts)])


def _request(rid: str, msg: dict, ts: str | None) -> Request:
    u = msg.get("usage")
    if not isinstance(u, dict):
        return Request(rid, msg.get("model"), ts, Tokens(None, None, None, None, None),
                       None, None, None, msg.get("stop_reason"))
    cc = u.get("cache_creation") or {}
    return Request(
        request_id=rid,
        model=msg.get("model"),
        timestamp=ts,
        tokens=Tokens(u.get("input_tokens"), u.get("output_tokens"),
                      u.get("cache_read_input_tokens"),
                      cc.get("ephemeral_5m_input_tokens"), cc.get("ephemeral_1h_input_tokens")),
        speed=u.get("speed"),
        inference_geo=u.get("inference_geo"),
        web_search_requests=(u.get("server_tool_use") or {}).get("web_search_requests"),
        stop_reason=msg.get("stop_reason"),
    )


def dedupe_requests(requests) -> dict[str, Request]:
    """Una petición por requestId; gana la primera (Claude Code repite el usage en cada línea)."""
    out = {}
    for r in requests:
        out.setdefault(r.request_id, r)
    return out


def tokens_by_model(requests) -> dict[str, Tokens]:
    """Suma por modelo. Si alguna petición no sabe un campo (None), la suma tampoco lo sabe."""
    acc: dict[str, list] = {}
    for r in requests:
        cur = acc.setdefault(r.model, [0] * len(Tokens._fields))
        for i, v in enumerate(r.tokens):
            cur[i] = None if v is None or cur[i] is None else cur[i] + v
    return {m: Tokens(*v) for m, v in acc.items()}


def parse_meta(text: str) -> AgentMeta:
    """agent-<id>.meta.json de un subagente. Si está roto, todo None (el agente sale huérfano)."""
    try:
        m = json.loads(text)
    except ValueError:
        m = None
    if not isinstance(m, dict):
        m = {}
    return AgentMeta(m.get("agentType"), m.get("description"), m.get("toolUseId"),
                     m.get("spawnDepth"))
