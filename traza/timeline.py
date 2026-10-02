"""Vista de trazas (plan.md F6.5): eje de tiempo activo, desglose de tiempo y barra de valor.

Funciones puras sobre eventos ya leídos de la caché. El eje es **tiempo activo**: los huecos de
inactividad se eliminan (el reloj va en la cabecera). Medido en disco: sesiones de hasta 264 h
de reloj con un 0–36 % de tiempo activo (findings.md "F6.5 — medidas").
"""
from bisect import bisect_right

from .pricing import PARTS
from .signals import UNANSWERED_ORIGINS, WAITS_FOR_USER, parse_ts

IDLE_S = 300  # un hueco de ≥ 5 min entre dos eventos es inactividad, no trabajo


def _secs(ts) -> float | None:
    if isinstance(ts, (int, float)):
        return float(ts)
    t = parse_ts(ts)
    return None if t is None else t.timestamp()


class ActiveAxis:
    """Reloj → segundos de tiempo activo desde el primer evento. Los huecos ≥ IDLE_S no ocupan
    nada; quedan en `breaks` ({at: posición en el eje, idle_s: lo que duró la pausa}) para
    marcarlos con una línea sin ancho. Un eje común a todos los agentes de una sesión.
    `busy`: tramos (inicio, fin) con una herramienta en marcha: un hueco que cubren es trabajo,
    no pausa, aunque nadie escriba (un subagente de primer plano de 20 min, un Bash largo)."""

    def __init__(self, timestamps, busy=()):
        self.times = sorted({s for s in map(_secs, timestamps) if s is not None})
        spans = [(a, b) for a, b in ((_secs(x), _secs(y)) for x, y in busy)
                 if a is not None and b is not None]
        self.offsets, self.breaks, acc = [], [], 0.0
        for i, t in enumerate(self.times):
            if i:
                gap = t - self.times[i - 1]
                prev = self.times[i - 1]
                if gap >= IDLE_S and not any(a <= prev and b >= t for a, b in spans):
                    self.breaks.append({"at": acc, "idle_s": gap})
                else:
                    acc += gap
            self.offsets.append(acc)
        self.total = acc
        self.start = self.times[0] if self.times else None
        self.end = self.times[-1] if self.times else None

    def offset(self, ts) -> float | None:
        """Posición en el eje de un timestamp (dentro de una pausa: el punto donde empezó)."""
        t = ts if isinstance(ts, (int, float)) else _secs(ts)
        if t is None or not self.times:
            return None
        i = bisect_right(self.times, t) - 1
        if i < 0:
            return 0.0
        delta = t - self.times[i]
        if i + 1 < len(self.times) and self.offsets[i + 1] == self.offsets[i]:
            delta = 0.0                 # cae dentro de una pausa (el hueco se eliminó del eje)
        return self.offsets[i] + delta


def _gap_kind(prev: dict, nxt: dict) -> str:
    """A quién pertenece el hueco entre dos eventos de un agente, según lo que lo precede
    (plan.md F6.5, punto 5). Cada hueco va a UNA categoría: el desglose suma el tiempo activo."""
    same_response = prev["request_id"] is not None and prev["request_id"] == nxt["request_id"]
    kind = prev["kind"]
    if kind == "tool_use":
        if same_response:                 # el modelo sigue generando la misma respuesta
            return "model"
        return "user" if prev["tool_name"] in WAITS_FOR_USER else "tool"
    if kind == "tool_result":             # otro resultado detrás: aún corren herramientas
        return "tool" if nxt["kind"] == "tool_result" else "model"
    if kind == "text":
        # tras un texto, lo que no sea un prompt es más trabajo del modelo (p. ej. la herramienta
        # de la misma respuesta, que los subagentes escriben después del texto)
        if nxt["kind"] == "prompt":       # respuesta terminada: hasta tu siguiente prompt
            # si lo que llega no es tuyo (notificación de un subagente en segundo plano), el
            # agente estaba esperando a una herramienta, no a ti
            return "user" if nxt["origin"] in ("human", None) else "tool"
        return "model"
    if kind == "prompt":
        return "user" if prev["origin"] in UNANSWERED_ORIGINS else "model"
    return "model"                        # api_error, compactación, notificación…: turno del modelo


def running(events: list[dict]) -> list[tuple[float, float]]:
    """Tramos (llamada, resultado) en segundos con una herramienta de verdad en marcha. Las que
    esperan al usuario no cuentan; una sin resultado (interrumpida) tampoco: no se sabe cuánto
    duró, y contarla hasta el siguiente evento metía horas de "herramienta" en el desglose."""
    start = {e["tool_use_id"]: _secs(e["ts"]) for e in events
             if e["kind"] == "tool_use" and e.get("tool_use_id") and e["tool_name"] not in WAITS_FOR_USER}
    return [(start[e["tool_use_id"]], _secs(e["ts"])) for e in events
            if e["kind"] == "tool_result" and e.get("tool_use_id") in start
            and start[e["tool_use_id"]] is not None and _secs(e["ts"]) is not None]


def _paused(a: float, b: float, spans) -> bool:
    """Un hueco ≥ IDLE_S es pausa salvo que lo cubra una herramienta en marcha."""
    return b - a >= IDLE_S and not any(s <= a and e >= b for s, e in spans)


def _gaps(events: list[dict]):
    """(anterior, siguiente, desde, hasta) de cada hueco entre eventos seguidos del fichero,
    contado desde el instante más tardío ya visto: el orden del fichero no siempre es
    cronológico, y con 100 → 50 → 120 contar 50→120 entero duplicaba 50–100."""
    hi = None
    for prev, nxt in zip(events, events[1:]):
        a, b = _secs(prev["ts"]), _secs(nxt["ts"])
        if a is None or b is None:               # sin hora: no resta
            continue
        hi = a if hi is None else max(hi, a)
        if b > hi:
            yield prev, nxt, hi, b


def breakdown(events: list[dict]) -> dict[str, float]:
    """Segundos de modelo / herramientas / usuario (y de inactividad, aparte) de un agente.
    `events`: sus eventos en orden de fichero (kind, ts, request_id, tool_name, origin)."""
    out = {"model": 0.0, "tool": 0.0, "user": 0.0, "idle": 0.0}
    spans = running(events)
    for prev, nxt, a, b in _gaps(events):
        gap, kind = b - a, _gap_kind(prev, nxt)
        # una herramienta en marcha es trabajo dure lo que dure (en disco, subagentes de primer
        # plano de hasta 64 min); lo demás, a partir de IDLE_S, es pausa — también esperar al
        # usuario: tras 2 h con una pregunta abierta, lo probable es que no esté
        out["idle" if _paused(a, b, spans) else kind] += gap
    return out


def segments(events: list[dict], axis: ActiveAxis) -> list[list]:
    """Barra continua de un agente en el eje común: tramos [inicio, fin, quién, evento] con la
    misma regla que `breakdown` (suman lo mismo). Una marca por turno salía sub-píxel en sesiones
    de horas. Las pausas no se dibujan; tramos seguidos del mismo tipo se funden (no a través de
    una pausa); `evento` es el primero del tramo: pulsar lleva ahí. Una herramienta aún sin
    resultado sigue abierta hasta el final del eje."""
    out, cut, spans = [], True, running(events)
    for prev, nxt, a, b in _gaps(events):
        kind = _gap_kind(prev, nxt)
        if _paused(a, b, spans):
            cut = True                    # pausa: no se dibuja y corta la fusión
            continue
        s, e = axis.offset(a), axis.offset(b)
        if not cut and out[-1][2] == kind:
            out[-1][1] = e
        else:
            out.append([s, e, kind, prev["id"]])
        cut = False
    # en marcha ahora = como el estado en vivo y la señal de colgada (§7.1): de la ÚLTIMA
    # respuesta, sin resultado y sin prompt posterior; una abandonada antes no se alarga
    results = {e.get("tool_use_id") for e in events if e["kind"] == "tool_result"}
    last_req = next((e["request_id"] for e in reversed(events) if e["request_id"]), None)
    last_prompt = max((i for i, e in enumerate(events) if e["kind"] == "prompt"), default=-1)
    pending = [e for i, e in enumerate(events) if i > last_prompt and e["kind"] == "tool_use"
               and e["request_id"] == last_req and e["tool_name"] not in WAITS_FOR_USER
               and e.get("tool_use_id") not in results]
    last = axis.offset(events[-1]["ts"]) if events else None
    if pending and last is not None and axis.total > last:
        if out and not cut and out[-1][2] == "tool" and out[-1][1] == last:
            out[-1][1] = axis.total
        else:
            out.append([last, axis.total, "tool", pending[0]["id"]])
    return [[round(s, 1), round(e, 1), k, ev] for s, e, k, ev in out]


def value_split(parts_list) -> dict:
    """Barra de valor de un conjunto de peticiones (request_cost_parts de cada una). Política de
    §6.6/F6: cada tramo suma lo que tiene precio; unpriced = cuántas no lo tienen ("+"); si
    ninguna lo tiene, parts y cost son None ("?")."""
    priced = [p for p in parts_list if p is not None]
    unpriced = len(parts_list) - len(priced)
    if unpriced and not priced:
        return {"parts": None, "cost": None, "unpriced": unpriced}
    parts = {k: sum(p[k] for p in priced) for k in PARTS}
    return {"parts": parts, "cost": sum(parts.values()), "unpriced": unpriced}
