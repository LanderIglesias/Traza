"""Coste estimado (design.md §6.6), contra el prices.toml real."""
import pytest

from traza.parser import Request, Tokens
from traza.pricing import price_key, request_cost

M = 1_000_000


def req(model, tokens=Tokens(M, M, M, M, M), speed="standard", geo="not_available", web=0):
    return Request("r", model, "t", tokens, speed, geo, web, "end_turn")


def test_alias_solo_quita_el_sufijo_de_fecha():
    assert price_key("claude-haiku-4-5-20251001") == "claude-haiku-4-5"
    assert price_key("claude-opus-5-5") == "claude-opus-5-5"
    # opus-5 y opus-5-5 son modelos distintos: no se fusionan
    assert request_cost(req("claude-opus-5")) != request_cost(req("claude-opus-5-5"))


def test_formula_por_tipo_de_token():
    # sonnet-5: input 2 + output 10 + lectura caché 0,20 + escritura 5 min 2,50 + 1 h 4
    assert request_cost(req("claude-sonnet-5")) == pytest.approx(18.70)
    assert request_cost(req("claude-haiku-4-5-20251001", Tokens(0, M, 0, 0, 0))) == pytest.approx(5)


def test_synthetic_cuesta_cero():
    assert request_cost(req("<synthetic>", Tokens(0, 0, 0, 0, 0), speed=None, geo=None)) == 0


def test_desconocido_es_none_nunca_cero():
    assert request_cost(req("claude-futuro-9")) is None
    assert request_cost(req("claude-sonnet-5", Tokens(None, None, None, None, None))) is None
    assert request_cost(req("claude-sonnet-5", speed="turbo")) is None
    assert request_cost(req("claude-sonnet-5", geo="eu")) is None
    assert request_cost(req("claude-sonnet-5", speed="fast")) is None  # sonnet no tiene fast


def test_modificadores():
    base = Tokens(M, M, 0, 0, 0)
    assert request_cost(req("claude-opus-5-5", base, speed="fast")) == pytest.approx(8 + 40)
    assert request_cost(req("claude-opus-5-5", base, geo="us")) == pytest.approx((4 + 20) * 1.1)
    assert request_cost(req("claude-opus-5-5", base, web=3)) == pytest.approx(4 + 20 + 0.03)
    # speed ausente (versiones antiguas) = estándar
    assert request_cost(req("claude-opus-5-5", base, speed=None)) == pytest.approx(4 + 20)
