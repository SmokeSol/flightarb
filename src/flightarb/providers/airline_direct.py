"""Direct-at-source verification of finalists.

This is the step that separates a scraper from an engine you would actually
book from.  Discovery adapters are allowed to be approximate, because they only
have to decide *which* five journeys deserve attention.  Those five then get
re-priced at the operating carrier, and only then does a number get presented
as real.

    ~200 discovery queries  ->  rank  ->  5 finalists  ->  5 verification calls

That ratio is the whole point: verification is expensive and precise, discovery
is cheap and approximate, and the engine never confuses the two.

Off by default (``providers.airline_direct.enabled``).  Each carrier verifier
is a small class; adding one is a ~30 line exercise.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from datetime import date, datetime, timedelta

from ..models import Confidence, FlightOffer, Segment
from .base import FlightProvider, ProviderUnavailable, SearchQuery
from .fastflights import FX_TO_EUR

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"


def _get_json(url: str, timeout: int) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


class CarrierVerifier(ABC):
    """Re-price one carrier's own inventory."""

    carrier: str = "??"

    @abstractmethod
    def fares(self, origin: str, destination: str, day: date, timeout: int) -> list[dict]:
        """Return [{'depart': datetime, 'arrive': datetime, 'price_eur': float,
        'flight_no': str, 'url': str|None}, ...]"""


class RyanairVerifier(CarrierVerifier):
    """Public fare-finder JSON. Read-only, no auth, one call per leg."""

    carrier = "FR"
    BASE = "https://services-api.ryanair.com/farfnd/v4/oneWayFares"

    def fares(self, origin: str, destination: str, day: date, timeout: int) -> list[dict]:
        params = {
            "departureAirportIataCode": origin,
            "arrivalAirportIataCode": destination,
            "outboundDepartureDateFrom": day.isoformat(),
            "outboundDepartureDateTo": day.isoformat(),
            "currency": "EUR",
            "language": "en",
            "limit": "16",
            "offset": "0",
            "market": "en-gb",
        }
        doc = _get_json(f"{self.BASE}?{urllib.parse.urlencode(params)}", timeout)
        out = []
        for fare in doc.get("fares", []) or []:
            leg = fare.get("outbound") or {}
            price = leg.get("price") or {}
            value = price.get("value")
            if value is None:
                continue
            ccy = str(price.get("currencyCode", "EUR")).upper()
            try:
                depart = datetime.fromisoformat(str(leg["departureDate"]))
                arrive = datetime.fromisoformat(str(leg["arrivalDate"]))
            except (KeyError, ValueError):
                continue
            key = str(leg.get("flightKey", ""))
            flight_no = key.split("~")[1] if "~" in key else ""
            out.append(
                {
                    "depart": depart,
                    "arrive": arrive,
                    "price_eur": float(value) * FX_TO_EUR.get(ccy, 1.0),
                    "flight_no": f"FR{flight_no}",
                    "url": "https://www.ryanair.com/gb/en/trip/flights/select",
                }
            )
        return out


VERIFIERS: dict[str, CarrierVerifier] = {v.carrier: v for v in (RyanairVerifier(),)}


class AirlineDirectProvider(FlightProvider):
    name = "airline-direct"
    real_prices = True
    supports_round_trip = False
    is_verifier = True

    #: How far a verified departure may sit from the discovered one and still
    #: be considered "the same flight".
    MATCH_WINDOW_MIN = 90

    def __init__(self, ctx):
        super().__init__(ctx)
        enabled = bool(ctx.policy.get("providers.airline_direct.enabled", False))
        wanted = [c.upper() for c in ctx.policy.get("providers.airline_direct.carriers", [])]
        self.verifiers = {
            code: v for code, v in VERIFIERS.items() if not wanted or code in wanted
        }
        self._enabled = enabled and bool(self.verifiers)

    def available(self) -> bool:
        return self._enabled and super().available()

    def unavailable_reason(self) -> str | None:
        if not self._enabled:
            return "disabled (set providers.airline_direct.enabled = true)"
        return super().unavailable_reason()

    def _search(self, query: SearchQuery) -> list[FlightOffer]:
        """Verification is targeted, not exploratory: we only ask carriers we
        know operate the route the planner is asking about."""
        if not self._enabled:
            raise ProviderUnavailable("airline-direct disabled")
        timeout = int(self.policy.get("providers.timeout_seconds", 25))
        offers: list[FlightOffer] = []
        for code, verifier in self.verifiers.items():
            try:
                rows = verifier.fares(query.origin, query.destination, query.depart_date, timeout)
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ValueError):
                continue
            for row in rows:
                offers.append(self._row_to_offer(code, row, query))
        return offers

    def _row_to_offer(self, code: str, row: dict, query: SearchQuery) -> FlightOffer:
        duration = max(1, int((row["arrive"] - row["depart"]).total_seconds() // 60))
        seg = Segment(
            carrier=code,
            flight_no=row.get("flight_no") or f"{code}----",
            origin=query.origin,
            destination=query.destination,
            depart=row["depart"],
            arrive=row["arrive"],
            duration_min=duration,
            cabin=query.cabin,
        )
        return FlightOffer(
            segments=(seg,),
            price_eur=round(float(row["price_eur"]), 2),
            provider=self.name,
            fare_brand="basic",
            included_checked_bags=0,
            confidence=Confidence.VERIFIED,
            booking_url=row.get("url"),
            raw={"verified_carrier": code},
        )

    # ------------------------------------------------------------------ #
    def verify(self, offer: FlightOffer) -> FlightOffer | None:
        """Re-price one discovered offer at the operating carrier."""
        if not self._enabled or offer.stops > 0:
            return None
        carrier = offer.carriers[0] if offer.carriers else ""
        verifier = self.verifiers.get(carrier)
        if verifier is None:
            return None

        timeout = int(self.policy.get("providers.timeout_seconds", 25))
        self.limiter.acquire()
        try:
            rows = verifier.fares(offer.origin, offer.destination, offer.depart_date, timeout)
        except Exception:
            self.breaker.record_failure()
            return None
        self.breaker.record_success()
        if not rows:
            return None

        window = timedelta(minutes=self.MATCH_WINDOW_MIN)
        candidates = [r for r in rows if abs(r["depart"] - offer.depart) <= window]
        if not candidates:
            return None
        best = min(candidates, key=lambda r: abs(r["depart"] - offer.depart))

        duration = max(1, int((best["arrive"] - best["depart"]).total_seconds() // 60))
        seg = Segment(
            carrier=carrier,
            flight_no=best.get("flight_no") or offer.segments[0].flight_no,
            origin=offer.origin,
            destination=offer.destination,
            depart=best["depart"],
            arrive=best["arrive"],
            duration_min=duration,
            cabin=offer.segments[0].cabin,
        )
        return FlightOffer(
            segments=(seg,),
            price_eur=round(float(best["price_eur"]), 2),
            provider=f"{self.name}:{carrier}",
            bundle_id=offer.bundle_id,
            bundle_price_eur=offer.bundle_price_eur,
            fare_brand=offer.fare_brand,
            included_cabin_bags=offer.included_cabin_bags,
            included_checked_bags=offer.included_checked_bags,
            confidence=Confidence.VERIFIED,
            booking_url=best.get("url"),
            raw={**offer.raw, "verified_from": offer.provider, "discovery_price": offer.price_eur},
        )
