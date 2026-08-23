"""Discovery adapter built on the open-source ``fast-flights`` package.

Role in the system: *candidate generation only*.

This is a reverse-engineered reader of a public search surface.  Its own issue
tracker documents itineraries that are visible in a browser but missing from
its results, so treating it as a price oracle would be a mistake.  Everything
it returns is ``Confidence.DISCOVERY``: good enough to decide which five
journeys are worth verifying, never good enough to be the final number.

Install with:  pip install fast-flights
"""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta

from ..models import Cabin, Confidence, FlightOffer, Segment
from .base import FlightProvider, ProviderUnavailable, SearchQuery

# Static conversion table. A flight search does not justify a paid FX feed;
# these only need to be right enough to rank, and the verification pass
# re-prices the finalists in their real currency anyway.
FX_TO_EUR = {
    "EUR": 1.0, "€": 1.0,
    "USD": 0.92, "$": 0.92,
    "GBP": 1.17, "£": 1.17,
    "MAD": 0.092, "DH": 0.092,
    "CHF": 1.04, "PLN": 0.23, "SEK": 0.088, "NOK": 0.086, "DKK": 0.134,
}

_PRICE_RE = re.compile(r"([€$£]|\b[A-Z]{3}\b)?\s*([\d][\d,.\s]*)")
_TIME_RE = re.compile(r"(\d{1,2}):(\d{2})\s*(AM|PM)?", re.I)
_DUR_RE = re.compile(r"(?:(\d+)\s*hr)?\s*(?:(\d+)\s*min)?", re.I)


def parse_price(text: str) -> tuple[float, str] | None:
    if not text:
        return None
    m = _PRICE_RE.search(text.replace(" ", " "))
    if not m:
        return None
    symbol = (m.group(1) or "EUR").strip().upper()
    digits = m.group(2).replace(",", "").replace(" ", "").rstrip(".")
    try:
        value = float(digits)
    except ValueError:
        return None
    return value, symbol


def to_eur(value: float, symbol: str) -> float:
    return value * FX_TO_EUR.get(symbol, 1.0)


def parse_clock(text: str, day: date) -> datetime | None:
    m = _TIME_RE.search(text or "")
    if not m:
        return None
    hour, minute = int(m.group(1)), int(m.group(2))
    meridiem = (m.group(3) or "").upper()
    if meridiem == "PM" and hour < 12:
        hour += 12
    elif meridiem == "AM" and hour == 12:
        hour = 0
    return datetime.combine(day, time(hour % 24, minute))


def parse_duration(text: str) -> int:
    m = _DUR_RE.search(text or "")
    if not m:
        return 0
    hours = int(m.group(1) or 0)
    minutes = int(m.group(2) or 0)
    return hours * 60 + minutes


class FastFlightsProvider(FlightProvider):
    name = "fast-flights"
    real_prices = True
    supports_round_trip = True

    def __init__(self, ctx):
        super().__init__(ctx)
        self._mod = None
        try:
            import fast_flights  # type: ignore

            self._mod = fast_flights
        except Exception:
            self._mod = None

    def available(self) -> bool:
        return self._mod is not None and super().available()

    def unavailable_reason(self) -> str | None:
        if self._mod is None:
            return "package not installed (pip install fast-flights)"
        return super().unavailable_reason()

    # ------------------------------------------------------------------ #
    def _search(self, query: SearchQuery) -> list[FlightOffer]:
        if self._mod is None:
            raise ProviderUnavailable("fast-flights not installed")
        m = self._mod

        legs = [m.FlightData(date=query.depart_date.isoformat(),
                             from_airport=query.origin, to_airport=query.destination)]
        trip = "one-way"
        if query.return_date is not None:
            legs.append(m.FlightData(date=query.return_date.isoformat(),
                                     from_airport=query.destination, to_airport=query.origin))
            trip = "round-trip"

        seat = {
            Cabin.ECONOMY: "economy",
            Cabin.PREMIUM: "premium-economy",
            Cabin.BUSINESS: "business",
            Cabin.FIRST: "first",
        }[query.cabin]

        kwargs = dict(
            flight_data=legs,
            trip=trip,
            seat=seat,
            passengers=m.Passengers(adults=max(1, query.seats), children=0,
                                    infants_in_seat=0, infants_on_lap=0),
            fetch_mode="fallback",
        )
        try:
            result = m.get_flights(**kwargs)
        except TypeError:
            kwargs.pop("fetch_mode", None)
            result = m.get_flights(**kwargs)
        except Exception as exc:
            raise ProviderUnavailable(str(exc)) from exc

        flights = getattr(result, "flights", None) or []
        offers: list[FlightOffer] = []
        for f in flights:
            offer = self._to_offer(f, query)
            if offer is not None:
                offers.append(offer)
        return offers

    def _to_offer(self, f, query: SearchQuery) -> FlightOffer | None:
        """Map one library row onto our model. Defensive by design -- the
        upstream shape has changed between releases more than once."""
        priced = parse_price(str(getattr(f, "price", "") or ""))
        if priced is None:
            return None
        amount, symbol = priced
        default_ccy = str(self.policy.get("providers.fastflights.currency", "EUR")).upper()
        price_eur = to_eur(amount, symbol if symbol in FX_TO_EUR else default_ccy)

        depart = parse_clock(str(getattr(f, "departure", "")), query.depart_date)
        if depart is None:
            depart = datetime.combine(query.depart_date, time(9, 0))
        duration = parse_duration(str(getattr(f, "duration", ""))) or 120

        stops_raw = getattr(f, "stops", 0)
        try:
            stops = int(stops_raw)
        except (TypeError, ValueError):
            stops = 0 if "non" in str(stops_raw).lower() else 1
        if stops > query.max_stops:
            return None

        carrier_name = str(getattr(f, "name", "") or "").strip()
        carrier = _carrier_code(carrier_name)

        # The library reports the itinerary, not each segment. We model it as a
        # single elapsed-time segment and carry the true stop count in `raw`;
        # the cost engine uses duration_min and stops, never segment internals.
        seg = Segment(
            carrier=carrier,
            flight_no=f"{carrier}----",
            origin=query.origin,
            destination=query.destination,
            depart=depart,
            arrive=depart + timedelta(minutes=duration),
            duration_min=duration,
            cabin=query.cabin,
        )
        return FlightOffer(
            segments=(seg,),
            price_eur=round(price_eur / max(1, query.seats) if _is_total(f) else price_eur, 2),
            provider=self.name,
            fare_brand="unknown",
            included_checked_bags=0,
            confidence=Confidence.DISCOVERY,
            booking_url=None,
            raw={
                "airline": carrier_name,
                "stops": stops,
                "raw_price": str(getattr(f, "price", "")),
                "is_best": bool(getattr(f, "is_best", False)),
                "arrival_time_ahead": str(getattr(f, "arrival_time_ahead", "")),
                "round_trip": query.return_date is not None,
            },
        )


def _is_total(f) -> bool:
    """Some releases report a party total rather than a per-passenger fare."""
    return bool(getattr(f, "is_total_price", False))


_AIRLINE_CODES = {
    "ryanair": "FR", "wizz": "W6", "easyjet": "U2", "vueling": "VY",
    "iberia": "IB", "air europa": "UX", "royal air maroc": "AT",
    "air arabia": "3O", "transavia": "TO", "tap": "TP", "air france": "AF",
    "british airways": "BA", "lufthansa": "LH", "klm": "KL", "turkish": "TK",
    "ita": "AZ", "eurowings": "EW", "binter": "NT", "volotea": "V7",
}


def _carrier_code(name: str) -> str:
    low = name.lower()
    for needle, code in _AIRLINE_CODES.items():
        if needle in low:
            return code
    return (name[:2].upper() or "??")
