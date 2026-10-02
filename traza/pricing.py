"""Coste estimado de una petición (design.md §6.6). Se calcula al consultar, nunca se guarda."""
import re
import tomllib
from pathlib import Path

from .parser import Request

PRICES = tomllib.loads((Path(__file__).parent / "prices.toml").read_text(encoding="utf-8"))
_DATE_SUFFIX = re.compile(r"-\d{8}$")


def price_key(model: str) -> str:
    """Solo se quita el sufijo de fecha: claude-haiku-4-5-20251001 → claude-haiku-4-5."""
    return _DATE_SUFFIX.sub("", model)


PARTS = ("input", "output", "cache_read", "cache_write", "web_search")


def request_cost(req: Request, prices: dict = PRICES) -> float | None:
    """USD a precios públicos de la API. None = "no lo sé" (modelo, tokens o modificador
    desconocido), nunca 0. Es la suma de request_cost_parts: una sola fórmula."""
    parts = request_cost_parts(req, prices)
    return None if parts is None else sum(parts.values())


def request_cost_parts(req: Request, prices: dict = PRICES) -> dict[str, float] | None:
    """El valor de una petición por tipo de token (la barra de valor, F6.5). None = sin precio."""
    if req.model == "<synthetic>":
        return dict.fromkeys(PARTS, 0.0)  # mensajes generados por Claude Code, sin llamada a la API
    table = prices["models"].get(price_key(req.model or ""))
    if table is None or None in req.tokens:
        return None
    if req.speed == "fast":
        table = table.get("fast")
    elif req.speed not in (None, "standard"):  # None: versiones sin el campo = estándar
        return None
    geo = 1.0 if req.inference_geo is None else prices["inference_geo"].get(req.inference_geo)
    if table is None or geo is None:
        return None
    t, m = req.tokens, geo / 1_000_000
    # ponytail: el ×1,1 de inference_geo no se aplica a las búsquedas web (sin verificar; 0 en disco)
    return {"input": t.input * table["input"] * m,
            "output": t.output * table["output"] * m,
            "cache_read": t.cache_read * table["cache_read"] * m,
            "cache_write": (t.cache_write_5m * table["cache_write_5m"]
                            + t.cache_write_1h * table["cache_write_1h"]) * m,
            "web_search": (req.web_search_requests or 0) * prices["web_search_per_request"]}
