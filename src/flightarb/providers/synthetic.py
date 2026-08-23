"""A deterministic simulated airline market.

Why this exists
---------------
Scrapers break, get blocked, and rate-limit.  If the engine can only be
exercised when a third-party site cooperates, it cannot be tested, tuned, or
demonstrated.  This adapter generates a *plausible* market from real airport
geography -- distance, carrier mix, hub vs. secondary airport, day of week,
season, and the days-to-departure curve -- seeded deterministically so the same
query always returns the same market.

It reproduces the structural facts the engine is built to exploit:

* secondary airports are systematically cheaper than flag hubs,
* low-cost carriers give no round-trip discount, legacy carriers do,
* one-stop itineraries undercut non-stops on time-insensitive routes.

Prices from this adapter are ``Confidence.SYNTHETIC``.  They are never written
to the price memory and are labelled everywhere they surface.  It is a physics
engine for the ranker, not a source of bookable fares.
"""

from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from ..models import Airport, Cabin, Confidence, FlightOffer, Segment
from ..geo.airports import haversine_km
from .base import FlightProvider, ProviderUnavailable, SearchQuery


@dataclass(frozen=True)
class CarrierProfile:
    code: str
    name: str
    kind: str  # legacy | lcc
    checked_included: bool
    rt_discount: float  # round trip = 2 x one-way x this


LEGACY = lambda c, n: CarrierProfile(c, n, "legacy", True, 0.86)  # noqa: E731
LCC = lambda c, n: CarrierProfile(c, n, "lcc", False, 1.00)  # noqa: E731

NATIONAL: dict[str, list[CarrierProfile]] = {
    "MA": [LEGACY("AT", "Royal Air Maroc"), LCC("3O", "Air Arabia Maroc")],
    "ES": [LEGACY("IB", "Iberia"), LCC("VY", "Vueling"), LEGACY("UX", "Air Europa")],
    "PT": [LEGACY("TP", "TAP Portugal")],
    "FR": [LEGACY("AF", "Air France"), LCC("TO", "Transavia France")],
    "GB": [LEGACY("BA", "British Airways"), LCC("U2", "easyJet")],
    "DE": [LEGACY("LH", "Lufthansa"), LCC("EW", "Eurowings")],
    "IT": [LEGACY("AZ", "ITA Airways")],
    "NL": [LEGACY("KL", "KLM")],
    "BE": [LCC("TB", "TUI fly")],
    "TR": [LEGACY("TK", "Turkish Airlines")],
}

# Pan-European low-cost carriers fly nearly everywhere.
GLOBAL_LCC = [LCC("FR", "Ryanair"), LCC("W6", "Wizz Air"), LCC("U2", "easyJet")]

# Plausible connecting hubs for synthesised one-stop itineraries.
HUBS = ("MAD", "LIS", "CDG", "BCN", "CMN", "FCO", "BRU")

DTD_CURVE: tuple[tuple[int, float], ...] = (
    (2, 2.00), (3, 1.78), (6, 1.62), (10, 1.42),
    (13, 1.30), (20, 1.18), (29, 1.08), (44, 1.00),
    (59, 0.97), (89, 0.94), (10_000, 0.92),
)
WEEKDAY_FACTOR = (1.02, 0.93, 0.93, 1.00, 1.15, 1.02, 1.12)  # Mon..Sun
MONTH_FACTOR = {1: 0.92, 2: 0.92, 3: 0.96, 4: 1.05, 5: 1.02, 6: 1.12,
                7: 1.25, 8: 1.25, 9: 1.10, 10: 1.00, 11: 0.94, 12: 1.15}

CRUISE_KMH = 720.0
GROUND_MANOEUVRE_MIN = 35


def _dtd_factor(days: int) -> float:
    for limit, factor in DTD_CURVE:
        if days <= limit:
            return factor
    return 0.92


def _seed(*parts: object) -> random.Random:
    digest = hashlib.sha256("|".join(str(p) for p in parts).encode()).digest()
    return random.Random(int.from_bytes(digest[:8], "big"))


class SyntheticProvider(FlightProvider):
    name = "synthetic"
    real_prices = False
    supports_round_trip = True
    needs_rate_limit = False  # computed locally; no server to be polite to

    SALT = "flightarb-v1"

    def __init__(self, ctx):
        super().__init__(ctx)
        self._hub_cache: dict[str, float] = {}

    def available(self) -> bool:  # always
        return True

    # -- market construction ---------------------------------------------- #
    def _carriers(self, o: Airport, d: Airport, rng: random.Random) -> list[CarrierProfile]:
        pool: list[CarrierProfile] = []
        seen: set[str] = set()
        for cp in NATIONAL.get(o.country, []) + NATIONAL.get(d.country, []) + GLOBAL_LCC:
            if cp.code not in seen:
                seen.add(cp.code)
                pool.append(cp)
        if not pool:
            pool = list(GLOBAL_LCC)
        # Route thickness follows airport size: two big airports support more
        # competing carriers than two regional fields.
        thickness = o.size_rank + d.size_rank  # 2..6
        n = max(1, min(len(pool), thickness - 1 + rng.randint(0, 1)))
        rng.shuffle(pool)
        return pool[:n]

    def _airport_factor(self, a: Airport) -> float:
        """Secondary airports are structurally cheaper: lower passenger charges,
        low-cost base economics, and no flag carrier defending a hub premium.
        This factor is the arbitrage the engine exists to hunt.

        Hub status is modelled from the size of the metro area served, not from
        OurAirports' ``type`` field -- Casablanca and Rabat are both tagged
        ``large_airport``, which describes the runway, not the fare structure.
        """
        cached = self._hub_cache.get(a.iata)
        if cached is not None:
            return cached

        population = 0
        if self.ctx.places is not None:
            population = self.ctx.places.metro_population(a.lat, a.lon)

        if population >= 2_500_000:
            factor = 1.10   # flag-carrier hub
        elif population >= 1_200_000:
            factor = 1.00
        elif population >= 400_000:
            factor = 0.92
        elif population > 0:
            factor = 0.86   # regional field, low-cost territory
        else:
            factor = {3: 1.04, 2: 0.93, 1: 0.88}.get(a.size_rank, 1.0)

        self._hub_cache[a.iata] = factor
        return factor

    def _base_fare(self, cp: CarrierProfile, km: float) -> float:
        if cp.kind == "lcc":
            return 16.0 + 0.058 * km
        return 42.0 + 0.105 * km

    def _one_way_offers(
        self,
        o: Airport,
        d: Airport,
        day: date,
        cabin: Cabin,
        max_stops: int,
    ) -> list[FlightOffer]:
        rng = _seed(self.SALT, o.iata, d.iata, day.isoformat())
        km = haversine_km(o.lat, o.lon, d.lat, d.lon)
        # Who serves a route is a property of the ROUTE, not of the day, and it
        # is the same in both directions -- an airline that flies you out is
        # there to fly you back. Seeding this per-date was wrong: it produced
        # routes where no single carrier operated a round trip, which made
        # round-trip tickets vanish entirely.
        carriers = self._carriers(o, d, _seed(self.SALT, "fleet", *sorted((o.iata, d.iata))))

        days_out = max(0, (day - date.today()).days)
        market = (
            _dtd_factor(days_out)
            * WEEKDAY_FACTOR[day.weekday()]
            * MONTH_FACTOR.get(day.month, 1.0)
            * self._airport_factor(o)
            * self._airport_factor(d)
            * (1.0 - 0.03 * (len(carriers) - 1))
        )
        cabin_mult = {Cabin.ECONOMY: 1.0, Cabin.PREMIUM: 1.7, Cabin.BUSINESS: 3.1, Cabin.FIRST: 5.0}[cabin]

        offers: list[FlightOffer] = []
        for cp in carriers:
            frequencies = 1 + rng.randint(0, max(0, (o.size_rank + d.size_rank) // 2))
            for f in range(frequencies):
                dep_hour = rng.choice([6, 7, 9, 11, 13, 15, 17, 19, 21])
                dep_min = rng.choice([0, 10, 20, 30, 40, 50])
                tod = 0.88 if dep_hour < 8 else (1.10 if 17 <= dep_hour <= 20 else 1.0)
                noise = rng.uniform(0.85, 1.25)

                price = self._base_fare(cp, km) * market * tod * noise * cabin_mult
                depart = datetime.combine(day, time(dep_hour, dep_min))
                dur = int(km / CRUISE_KMH * 60 + GROUND_MANOEUVRE_MIN)
                seg = Segment(
                    carrier=cp.code,
                    flight_no=f"{cp.code}{rng.randint(100, 989)}",
                    origin=o.iata,
                    destination=d.iata,
                    depart=depart,
                    arrive=depart + timedelta(minutes=dur),
                    duration_min=dur,
                    cabin=cabin,
                )
                offers.append(
                    FlightOffer(
                        segments=(seg,),
                        price_eur=round(max(14.0, price), 2),
                        provider=self.name,
                        fare_brand="basic" if cp.kind == "lcc" else "standard",
                        included_cabin_bags=1,
                        included_checked_bags=1 if cp.checked_included else 0,
                        confidence=Confidence.SYNTHETIC,
                        raw={"carrier_kind": cp.kind, "rt_discount": cp.rt_discount, "km": round(km)},
                    )
                )

        if max_stops >= 1:
            offers.extend(self._connecting_offers(o, d, day, cabin, rng, km))
        offers.sort(key=lambda x: x.price_eur)
        return offers[:14]

    def _connecting_offers(
        self, o: Airport, d: Airport, day: date, cabin: Cabin, rng: random.Random, km: float
    ) -> list[FlightOffer]:
        """One-stop itineraries: slower, usually cheaper -- exactly the trade
        the cost engine has to adjudicate."""
        out: list[FlightOffer] = []
        hub_codes = [h for h in HUBS if h not in (o.iata, d.iata)]
        rng.shuffle(hub_codes)
        for code in hub_codes[:2]:
            hub = self.ctx.airports.get(code)
            if hub is None:
                continue
            leg1 = haversine_km(o.lat, o.lon, hub.lat, hub.lon)
            leg2 = haversine_km(hub.lat, hub.lon, d.lat, d.lon)
            if leg1 + leg2 > km * 2.4 or leg1 < 120 or leg2 < 120:
                continue  # a detour that absurd would not be sold
            cp = rng.choice(NATIONAL.get(hub.country, GLOBAL_LCC))
            price = (self._base_fare(cp, leg1 + leg2) * 0.72) * _dtd_factor(
                max(0, (day - date.today()).days)
            ) * MONTH_FACTOR.get(day.month, 1.0) * rng.uniform(0.88, 1.15)

            dep_hour = rng.choice([6, 8, 10, 12, 14])
            d1 = int(leg1 / CRUISE_KMH * 60 + GROUND_MANOEUVRE_MIN)
            d2 = int(leg2 / CRUISE_KMH * 60 + GROUND_MANOEUVRE_MIN)
            layover = rng.choice([65, 80, 110, 145, 210])

            s1_dep = datetime.combine(day, time(dep_hour, 0))
            s1_arr = s1_dep + timedelta(minutes=d1)
            s2_dep = s1_arr + timedelta(minutes=layover)
            out.append(
                FlightOffer(
                    segments=(
                        Segment(cp.code, f"{cp.code}{rng.randint(100, 989)}", o.iata, hub.iata,
                                s1_dep, s1_arr, d1, cabin),
                        Segment(cp.code, f"{cp.code}{rng.randint(100, 989)}", hub.iata, d.iata,
                                s2_dep, s2_dep + timedelta(minutes=d2), d2, cabin),
                    ),
                    price_eur=round(max(19.0, price), 2),
                    provider=self.name,
                    fare_brand="basic" if cp.kind == "lcc" else "standard",
                    included_checked_bags=1 if cp.checked_included else 0,
                    confidence=Confidence.SYNTHETIC,
                    raw={"carrier_kind": cp.kind, "rt_discount": cp.rt_discount, "via": hub.iata},
                )
            )
        return out

    # -- provider API ------------------------------------------------------ #
    def _search(self, query: SearchQuery) -> list[FlightOffer]:
        o = self.ctx.airports.get(query.origin)
        d = self.ctx.airports.get(query.destination)
        if o is None or d is None:
            raise ProviderUnavailable(f"unknown airport in {query.label()}")
        if o.iata == d.iata:
            return []

        outbound = self._one_way_offers(o, d, query.depart_date, query.cabin, query.max_stops)
        if query.return_date is None:
            return outbound

        inbound = self._one_way_offers(d, o, query.return_date, query.cabin, query.max_stops)
        return self._bundle(outbound, inbound)

    @staticmethod
    def _bundle(outbound: list[FlightOffer], inbound: list[FlightOffer]) -> list[FlightOffer]:
        """Pair directions into sellable round-trip tickets.

        Same-carrier pairs earn that carrier's round-trip discount; mixed pairs
        do not, which is precisely why buying two one-ways sometimes wins.
        """
        bundled: list[FlightOffer] = []
        pairs = 0
        for out in outbound[:6]:
            for back in inbound[:6]:
                # Only one carrier can sell you a single round-trip ticket.
                # Mixed-carrier pairs are reachable, but only as two separate
                # one-ways -- which the assembler builds from the loose legs.
                if out.carriers != back.carriers:
                    continue
                discount = out.raw.get("rt_discount", 1.0)
                total = round((out.price_eur + back.price_eur) * discount, 2)
                bid = f"rt-{len(bundled)}"
                for leg in (out, back):
                    bundled.append(
                        FlightOffer(
                            segments=leg.segments,
                            price_eur=round(total / 2, 2),
                            provider=leg.provider,
                            bundle_id=bid,
                            bundle_price_eur=total,
                            fare_brand=leg.fare_brand,
                            included_cabin_bags=leg.included_cabin_bags,
                            included_checked_bags=leg.included_checked_bags,
                            confidence=leg.confidence,
                            raw=dict(leg.raw),
                        )
                    )
                pairs += 1
                if pairs >= 12:
                    return bundled
        return bundled
