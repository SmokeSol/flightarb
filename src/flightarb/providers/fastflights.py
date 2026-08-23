"""Multi-airline discovery, built on the open-source ``fast-flights`` package.

Role in the system: *candidate generation*.

This reads a public search surface that nobody guarantees to us. Its own issue
tracker documents itineraries visible in a browser but missing from its results,
so treating it as a price oracle would be a mistake. Everything it returns is
``Confidence.DISCOVERY``: good enough to decide which journeys deserve
attention, never good enough to be the final number. Finalists get re-priced at
the carrier.

Written against **fast-flights 3.x**, whose model is a great deal better than
2.x's: real per-segment records with airport codes, local departure and arrival
clocks, per-segment durations, and a numeric price in whatever currency we ask
for. No string scraping of "1 hr 15 min" or "€123" any more.

    Flights(type='UX', price=95, airlines=['Air Europa'], flights=[
        SingleFlight(from_airport=Airport(code='MAD'), to_airport=Airport(code='AGP'),
                     departure=SimpleDatetime(date=(2026,10,2), time=(6,30)),
                     arrival=SimpleDatetime(...), duration=75, plane_type='Boeing 737')])

Install with:  pip install "flightarb[discovery]"

(The extra also pins ``typing_extensions``, which fast-flights imports but does
not declare -- without it the package fails to import at all.)
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

from ..models import Cabin, Confidence, FlightOffer, Segment
from .base import FlightProvider, ProviderUnavailable, SearchQuery

SEAT = {
    Cabin.ECONOMY: "economy",
    Cabin.PREMIUM: "premium-economy",
    Cabin.BUSINESS: "business",
    Cabin.FIRST: "first",
}

_IATA = re.compile(r"^[A-Z0-9]{2}$")

_AIRLINE_CODES = {
    "ryanair": "FR", "wizz": "W6", "easyjet": "U2", "vueling": "VY",
    "iberia": "IB", "air europa": "UX", "royal air maroc": "AT",
    "air arabia": "3O", "transavia": "TO", "tap": "TP", "air france": "AF",
    "british airways": "BA", "lufthansa": "LH", "klm": "KL", "turkish": "TK",
    "ita": "AZ", "eurowings": "EW", "binter": "NT", "volotea": "V7",
    "norwegian": "DY", "brussels": "SN", "swiss": "LX", "aegean": "A3",
}


def carrier_code(text: str) -> str:
    """Map an airline name to its IATA code; pass a code straight through."""
    raw = (text or "").strip()
    if _IATA.match(raw.upper()):
        return raw.upper()
    low = raw.lower()
    for needle, code in _AIRLINE_CODES.items():
        if needle in low:
            return code
    return (raw[:2].upper() or "??")


def _to_datetime(stamp) -> datetime | None:
    """``SimpleDatetime(date=(2026, 10, 2), time=(6, 30))`` -> datetime."""
    try:
        y, m, d = stamp.date
        hh, mm = stamp.time
        return datetime(int(y), int(m), int(d), int(hh), int(mm))
    except (AttributeError, TypeError, ValueError):
        return None


class FastFlightsProvider(FlightProvider):
    name = "fast-flights"
    real_prices = True
    #: One-way only. A round-trip query returns outbound options priced for the
    #: whole trip without the matching return legs, which we cannot model
    #: honestly. The engine prices each direction independently anyway, and
    #: recombines -- so this costs nothing and keeps the data truthful.
    supports_round_trip = False

    def __init__(self, ctx):
        super().__init__(ctx)
        self._mod = None
        self._import_error: str | None = None
        try:
            import fast_flights  # type: ignore

            self._mod = fast_flights
        except ImportError as exc:
            self._import_error = f"not installed ({exc}) -- pip install \"flightarb[discovery]\""
        except Exception as exc:
            # An installed-but-broken dependency is a different problem from a
            # missing one. Reporting both as "not installed" sends you off to
            # fix the wrong thing, so keep the real reason.
            self._import_error = f"installed but failed to import: {type(exc).__name__}: {exc}"

    def available(self) -> bool:
        return self._mod is not None and super().available()

    def unavailable_reason(self) -> str | None:
        if self._mod is None:
            return self._import_error
        return super().unavailable_reason()

    # ------------------------------------------------------------------ #
    def _search(self, query: SearchQuery) -> list[FlightOffer]:
        ff = self._mod
        if ff is None:
            raise ProviderUnavailable(self._import_error or "fast-flights unavailable")

        try:
            leg = ff.FlightQuery(
                date=query.depart_date.isoformat(),
                from_airport=query.origin,
                to_airport=query.destination,
                max_stops=query.max_stops,
            )
            built = ff.create_query(
                flights=[leg],
                seat=SEAT[query.cabin],
                trip="one-way",
                passengers=ff.Passengers(adults=max(1, query.seats)),
                currency="EUR",           # priced in our currency, no FX guessing
                max_stops=query.max_stops,
                # Ask the source to price the bags the traveller is bringing,
                # instead of us modelling them from a table.
                carry_on_bags=int(self.policy.get("bags.cabin", 0)),
                checked_bags=int(self.policy.get("bags.checked", 0)),
            )
            result = ff.get_flights(built)
        except Exception as exc:
            raise ProviderUnavailable(f"{type(exc).__name__}: {exc}") from exc

        offers: list[FlightOffer] = []
        for row in list(result):
            offer = self._to_offer(row, query)
            if offer is not None:
                offers.append(offer)
        offers.sort(key=lambda o: o.price_eur)
        return offers

    def _to_offer(self, row, query: SearchQuery) -> FlightOffer | None:
        """One ``Flights`` record -> one of our offers."""
        try:
            price = float(row.price)
        except (AttributeError, TypeError, ValueError):
            return None
        if price <= 0:
            return None

        airlines = list(getattr(row, "airlines", None) or [])
        code = carrier_code(getattr(row, "type", "") or (airlines[0] if airlines else ""))

        segments: list[Segment] = []
        for leg in getattr(row, "flights", None) or []:
            depart = _to_datetime(getattr(leg, "departure", None))
            arrive = _to_datetime(getattr(leg, "arrival", None))
            origin = getattr(getattr(leg, "from_airport", None), "code", None)
            destination = getattr(getattr(leg, "to_airport", None), "code", None)
            if not (depart and arrive and origin and destination):
                return None
            try:
                duration = int(getattr(leg, "duration", 0))
            except (TypeError, ValueError):
                duration = 0
            if duration <= 0:
                # Both clocks are at their own airport, so this is only right
                # within a timezone -- but it is a fallback, not the norm.
                duration = max(1, int((arrive - depart).total_seconds() // 60))
            segments.append(
                Segment(
                    carrier=code,
                    flight_no=f"{code}····",
                    origin=str(origin),
                    destination=str(destination),
                    depart=depart,
                    arrive=arrive,
                    duration_min=duration,
                    cabin=query.cabin,
                )
            )

        if not segments:
            return None
        if len(segments) - 1 > query.max_stops:
            return None

        # A layover is measured between two clocks at the *same* airport, so it
        # is always correct regardless of timezones -- which is why we keep the
        # real segments rather than collapsing the itinerary into one row.
        return FlightOffer(
            segments=tuple(segments),
            price_eur=round(price, 2),
            provider=self.name,
            fare_brand="unknown",
            included_cabin_bags=int(self.policy.get("bags.cabin", 0)),
            included_checked_bags=int(self.policy.get("bags.checked", 0)),
            confidence=Confidence.DISCOVERY,
            booking_url=None,
            raw={
                "airlines": airlines,
                "itinerary_type": getattr(row, "type", None),
                "stops": len(segments) - 1,
                "plane": [getattr(s, "plane_type", None) for s in (row.flights or [])],
                "carbon_g": getattr(getattr(row, "carbon", None), "emission", None),
                "bags_priced_in_query": True,
            },
        )
