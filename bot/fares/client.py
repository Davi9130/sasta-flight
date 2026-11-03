"""Google Flights via fast-flights, with our own fetch so blocks are visible.

fast-flights' `get_flights` hides the HTTP status and crashes with AttributeError on
captcha/consent pages. We build the query with fast-flights, fetch with primp
ourselves, classify the response, and only then hand the HTML to its parser.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

import primp
from fast_flights import FlightQuery, FlightsNotFound, Passengers, Query, create_query
from fast_flights.parser import parse_js
from selectolax.lexbor import LexborHTMLParser

from bot.config import LANGUAGE, REQ_TIMEOUT_SECS, STOPS_MAX
from bot.fares.models import Quote

logger = logging.getLogger(__name__)

URL = "https://www.google.com/travel/flights"
# Pre-accepting consent avoids the EU consent interstitial (consent.google.com).
CONSENT_COOKIES = {"CONSENT": "YES+cb", "SOCS": "CAI"}
_BLOCK_MARKERS = (
    ("/sorry/", "captcha"),
    ("g-recaptcha", "captcha"),
    ("unusual traffic", "captcha"),
    ("consent.google.", "consent"),
    ("before you continue to google", "consent"),
)

OK = "ok"
NO_FLIGHTS = "no_flights"
BLOCKED = "blocked"
ERROR = "error"


@dataclass
class FetchResult:
    status: str
    quotes: list[Quote] = field(default_factory=list)
    detail: str | None = None
    http_status: int | None = None


def build_query(
    from_airport: str,
    to_airport: str,
    date: str,
    return_date: str | None = None,
    *,
    max_stops: str = "any",
    currency: str = "BRL",
) -> Query:
    stops = STOPS_MAX.get(max_stops)
    legs = [FlightQuery(date=date, from_airport=from_airport, to_airport=to_airport)]
    if return_date:
        legs.append(FlightQuery(date=return_date, from_airport=to_airport, to_airport=from_airport))
    return create_query(
        flights=legs,
        trip="round-trip" if return_date else "one-way",
        seat="economy",
        passengers=Passengers(adults=1),
        language=LANGUAGE,
        currency=currency,
        max_stops=stops,
    )


def booking_url(
    from_airport: str,
    to_airport: str,
    date: str,
    return_date: str | None = None,
    *,
    max_stops: str = "any",
    currency: str = "BRL",
) -> str:
    return build_query(
        from_airport, to_airport, date, return_date, max_stops=max_stops, currency=currency
    ).url()


def classify(http_status: int | None, final_url: str, html: str) -> tuple[str, str | None]:
    """Return (status, detail). Anything that smells like a block is BLOCKED, never ERROR."""
    if http_status == 429:
        return BLOCKED, "http_429"
    if http_status in (403, 503):
        return BLOCKED, f"http_{http_status}"
    url = final_url.lower()
    if "/sorry/" in url:
        return BLOCKED, "captcha"
    if "consent.google." in url:
        return BLOCKED, "consent"
    if http_status is not None and http_status >= 400:
        return ERROR, f"http_{http_status}"
    if "ds:1" in html:
        return OK, None
    # A normal results page always ships the ds:1 data script; without it we were
    # served an interstitial. Name it if we can, but treat it as a block either way.
    page = html[:50000].lower()
    for marker, reason in _BLOCK_MARKERS:
        if marker in page:
            return BLOCKED, reason
    return BLOCKED, "no_data_script"


def _to_datetime(sdt) -> datetime | None:
    try:
        (y, m, d), (hh, mm) = sdt.date, sdt.time
        return datetime(y, m, d, hh, mm)
    except (TypeError, ValueError):
        return None


def _total_duration(segments) -> int | None:
    try:
        total = sum(int(s.duration) for s in segments)
    except (TypeError, ValueError):
        return None
    for prev, nxt in zip(segments, segments[1:]):
        arrive, depart = _to_datetime(prev.arrival), _to_datetime(nxt.departure)
        if arrive and depart and depart > arrive:
            total += int((depart - arrive).total_seconds() // 60)
    return total


def parse_quotes(
    html: str,
    *,
    from_airport: str,
    to_airport: str,
    date: str,
    return_date: str | None,
    currency: str,
    max_stops: str,
    url: str | None,
) -> FetchResult:
    script = LexborHTMLParser(html).css_first(r"script.ds\:1")
    if script is None:
        return FetchResult(BLOCKED, detail="no_data_script")
    try:
        flights = parse_js(script.text())
    except FlightsNotFound:
        return FetchResult(NO_FLIGHTS)
    except Exception as exc:  # layout change or truncated payload
        logger.warning("fast-flights parse failed: %s", exc)
        return FetchResult(ERROR, detail=f"parse: {type(exc).__name__}")

    limit = STOPS_MAX.get(max_stops)
    quotes: list[Quote] = []
    for flight in flights:
        if not flight.price or not flight.flights:
            continue
        segments = flight.flights
        stops = len(segments) - 1
        if limit is not None and stops > limit:
            continue
        quotes.append(
            Quote(
                from_airport=segments[0].from_airport.code or from_airport,
                to_airport=segments[-1].to_airport.code or to_airport,
                date=date,
                return_date=return_date,
                price=float(flight.price),
                currency=currency,
                airlines=[str(a) for a in (flight.airlines or [])],
                departure_at=_to_datetime(segments[0].departure),
                arrival_at=_to_datetime(segments[-1].arrival),
                duration=_total_duration(segments),
                stops=stops,
                booking_url=url,
            )
        )
    quotes.sort(key=lambda q: q.price)
    return FetchResult(OK if quotes else NO_FLIGHTS, quotes=quotes)


class FareClient:
    """Synchronous; callers run `fetch` in a worker thread behind the guard."""

    name = "fast-flights"

    def __init__(self, timeout: float = REQ_TIMEOUT_SECS):
        self.timeout = timeout
        self._clients: dict[str | None, primp.Client] = {}

    def _client(self, proxy: str | None) -> primp.Client:
        # One client per proxy keeps a consistent cookie jar per exit IP.
        client = self._clients.get(proxy)
        if client is None:
            client = primp.Client(
                impersonate="chrome_145",
                impersonate_os="macos",
                referer=True,
                cookie_store=True,
                proxy=proxy,
                timeout=self.timeout,
                cookies=CONSENT_COOKIES,
            )
            self._clients[proxy] = client
        return client

    def reset(self, proxy: str | None):
        """Drop cookies for an exit IP after it was blocked."""
        self._clients.pop(proxy, None)

    def fetch(
        self,
        from_airport: str,
        to_airport: str,
        date: str,
        return_date: str | None,
        *,
        max_stops: str,
        currency: str,
        proxy: str | None,
    ) -> FetchResult:
        query = build_query(
            from_airport, to_airport, date, return_date, max_stops=max_stops, currency=currency
        )
        try:
            res = self._client(proxy).get(URL, params=query.params())
            html = res.text or ""
            status, final_url = res.status_code, str(res.url or "")
        except primp.TimeoutError:
            return FetchResult(ERROR, detail="timeout")
        except primp.PrimpError as exc:
            return FetchResult(ERROR, detail=f"network: {type(exc).__name__}")

        verdict, detail = classify(status, final_url, html)
        if verdict != OK:
            return FetchResult(verdict, detail=detail, http_status=status)
        result = parse_quotes(
            html,
            from_airport=from_airport,
            to_airport=to_airport,
            date=date,
            return_date=return_date,
            currency=currency,
            max_stops=max_stops,
            url=query.url(),
        )
        result.http_status = status
        return result
