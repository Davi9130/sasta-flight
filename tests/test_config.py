import importlib
import os

import pytest


def _reload_config(monkeypatch, currency: str | None = None):
    if currency is None:
        monkeypatch.delenv("CURRENCY", raising=False)
    else:
        monkeypatch.setenv("CURRENCY", currency)
    import bot.config as config

    return importlib.reload(config)


def test_currency_defaults_to_brl(monkeypatch):
    config = _reload_config(monkeypatch)
    assert config.CURRENCY == "BRL"
    assert config.COUNTRY == "BR"
    assert config.CURRENCY_SYMBOL["BRL"] == "R$"


@pytest.mark.parametrize(
    ("currency", "country", "symbol"),
    [
        ("USD", "US", "$"),
        ("EUR", "DE", "€"),
        ("GBP", "GB", "£"),
        ("BRL", "BR", "R$"),
    ],
)
def test_currency_from_env(monkeypatch, currency, country, symbol):
    config = _reload_config(monkeypatch, currency)
    assert config.CURRENCY == currency
    assert config.COUNTRY == country
    assert config.CURRENCY_SYMBOL[config.CURRENCY] == symbol


def test_invalid_currency_falls_back_to_brl(monkeypatch):
    config = _reload_config(monkeypatch, "JPY")
    assert config.CURRENCY == "BRL"
    assert config.COUNTRY == "BR"
