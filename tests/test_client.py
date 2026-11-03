from pathlib import Path
from types import SimpleNamespace

import primp

from bot.fares.client import (
    BLOCKED,
    ERROR,
    NO_FLIGHTS,
    OK,
    FareClient,
    booking_url,
    build_query,
    classify,
    parse_quotes,
)

FIXTURE = (Path(__file__).parent / "fixtures" / "results_gru_lis.html").read_text()


def _parse(html=FIXTURE, max_stops="any"):
    return parse_quotes(
        html,
        from_airport="GRU",
        to_airport="LIS",
        date="2026-11-23",
        return_date=None,
        currency="BRL",
        max_stops=max_stops,
        url="https://example/x",
    )


def test_parse_real_google_payload():
    result = _parse()
    assert result.status == OK
    assert result.quotes
    prices = [q.price for q in result.quotes]
    assert prices == sorted(prices)
    first = result.quotes[0]
    assert first.from_airport == "GRU" and first.to_airport == "LIS"
    assert first.price > 0
    assert first.departure_at is not None and first.arrival_at > first.departure_at
    assert first.duration and first.duration > 60
    assert first.airline


def test_parse_filters_stops_client_side():
    result = _parse(max_stops="direct")
    assert all(q.stops == 0 for q in result.quotes)


def test_parse_without_data_script_is_block():
    assert _parse("<html><body>nothing</body></html>").status == BLOCKED


def test_parse_error_payload_is_no_flights():
    html = '<script class="ds:1">AF_initDataCallback({key: \'ds:1\', data:errorHasStatus: true, x: 1});</script>'
    assert _parse(html).status == NO_FLIGHTS


def test_classify():
    assert classify(429, "https://www.google.com/travel/flights", "")[1] == "http_429"
    assert classify(403, "", "")[0] == BLOCKED
    assert classify(200, "https://www.google.com/sorry/index?continue=x", "")[1] == "captcha"
    assert classify(200, "https://consent.google.com/ml", "")[1] == "consent"
    assert classify(200, "u", "Our systems have detected unusual traffic")[1] == "captcha"
    assert classify(200, "u", "<html>empty</html>")[1] == "no_data_script"
    assert classify(500, "u", "")[0] == ERROR
    assert classify(200, "u", FIXTURE)[0] == OK


def test_build_query_round_trip_and_url():
    query = build_query("VIX", "MXP", "2026-12-01", "2026-12-11", max_stops="direct", currency="BRL")
    assert query.get_trip_type() == "round-trip"
    assert len(query.flight_data) == 2
    assert all(f.max_stops == 0 for f in query.flight_data)
    url = booking_url("VIX", "MXP", "2026-12-01", currency="BRL")
    assert url.startswith("https://www.google.com/travel/flights/search?tfs=")
    assert "curr=BRL" in url


class _FakePrimp:
    def __init__(self, response=None, exc=None):
        self.response, self.exc = response, exc

    def get(self, url, params=None):
        if self.exc:
            raise self.exc
        return self.response


def _client_with(fake):
    client = FareClient()
    client._clients[None] = fake
    return client


def _fetch(client):
    return client.fetch("GRU", "LIS", "2026-11-23", None, max_stops="any", currency="BRL", proxy=None)


def test_fetch_ok_and_blocked():
    ok = _fetch(_client_with(_FakePrimp(SimpleNamespace(text=FIXTURE, status_code=200, url="https://www.google.com/travel/flights"))))
    assert ok.status == OK and ok.quotes[0].booking_url.startswith("https://www.google.com/travel/flights/search")

    blocked = _fetch(_client_with(_FakePrimp(SimpleNamespace(text="", status_code=429, url="u"))))
    assert blocked.status == BLOCKED and blocked.detail == "http_429"


def test_fetch_network_errors_are_errors_not_blocks():
    result = _fetch(_client_with(_FakePrimp(exc=primp.TimeoutError("slow"))))
    assert result.status == ERROR and result.detail == "timeout"
    result = _fetch(_client_with(_FakePrimp(exc=primp.ConnectError("down"))))
    assert result.status == ERROR
