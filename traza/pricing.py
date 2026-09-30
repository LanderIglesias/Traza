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


def request_cost(req: Request, prices: dict = PRICES) -> float | None:
    """USD estimados. None = "no lo sé" (modelo, tokens o modificador desconocido), nunca 0."""
    if req.model == "<synthetic>":
        return 0.0  # mensajes generados por Claude Code, sin llamada a la API
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
    t = req.tokens
    tokens_usd = (t.input * table["input"] + t.output * table["output"]
                  + t.cache_read * table["cache_read"] + t.cache_write_5m * table["cache_write_5m"]
                  + t.cache_write_1h * table["cache_write_1h"]) / 1_000_000
    # ponytail: el ×1,1 de inference_geo no se aplica a las búsquedas web (sin verificar; 0 en disco)
    return tokens_usd * geo + (req.web_search_requests or 0) * prices["web_search_per_request"]
