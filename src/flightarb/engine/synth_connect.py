"""Build itineraries nobody is selling.

A flight source will tell you what ``CMN -> AGP`` costs.  It will not tell you
that ``CMN -> MAD`` on one airline plus ``MAD -> AGP`` on another, four hours
later, costs less than either carrier's through fare.  Nobody sells that
journey, so nobody advertises it -- but you can buy both halves.

The engine constructs those itineraries itself, then prices the thing that
makes them dangerous: the connection is **not protected**.  Miss it and no one
owes you a seat.  So a synthesised self-transfer carries a risk penalty and has
to clear ``economics.min_saving_for_self_transfer`` before it is ever
recommended.

Hidden-city ticketing is deliberately NOT implemented.  It breaks return legs,
forbids checked bags, and can get accounts closed -- a different category of
risk from simply buying two tickets, and not something to default anyone into.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from ..geo.airports import AirportIndex, haversine_km
from ..models import Airport, Confidence, FlightOffer
from ..policy import Policy


@dataclass(frozen=True)
class Hub:
    airport: Airport
    detour_ratio: float


def candidate_hubs(
    origin: Airport,
    destination: Airport,
    airports: AirportIndex,
    places=None,
    limit: int = 3,
    max_detour: float = 1.6,
    min_metro: int = 1_500_000,
) -> list[Hub]:
    """Airports worth trying as a self-transfer point.

    Detour is a *filter*, not the ranking. Sorting by detour is the trap:
    for Casablanca->Berlin it elects Gibraltar and Tetouan, which sit almost
    exactly on the great circle and have no connecting traffic whatsoever,
    while Madrid loses on a 1.13 ratio. What makes a hub is throughput, so the
    ranking is by metro size and detour merely has to be tolerable.

    ``size_rank`` alone is not enough either: OurAirports tags Beni Mellal as
    ``large_airport`` on runway grounds.
    """
    direct = haversine_km(origin.lat, origin.lon, destination.lat, destination.lon)
    if direct <= 0:
        return []

    hits: list[tuple[int, Hub]] = []
    for a in airports.airports:
        if a.iata in (origin.iata, destination.iata) or not a.scheduled:
            continue
        if a.size_rank < 3:
            continue  # connecting traffic needs a real airport
        leg1 = haversine_km(origin.lat, origin.lon, a.lat, a.lon)
        leg2 = haversine_km(a.lat, a.lon, destination.lat, destination.lon)
        if leg1 < 150 or leg2 < 150:
            continue  # too close to be a connection
        ratio = (leg1 + leg2) / direct
        if ratio > max_detour:
            continue
        metro = places.metro_population(a.lat, a.lon) if places is not None else 0
        if places is not None and metro < min_metro:
            continue
        hits.append((metro, Hub(a, ratio)))

    hits.sort(key=lambda t: (-t[0], t[1].detour_ratio))

    # One airport per metro area. Ranking purely by population otherwise fills
    # every slot with Heathrow, Gatwick and Stansted -- three ways to describe
    # the same connection, and two wasted probes.
    picked: list[Hub] = []
    for _metro, hub in hits:
        if any(
            haversine_km(hub.airport.lat, hub.airport.lon, p.airport.lat, p.airport.lon) < 90
            for p in picked
        ):
            continue
        picked.append(hub)
        if len(picked) >= limit:
            break
    return picked


def combine(
    first: FlightOffer,
    second: FlightOffer,
    policy: Policy,
    international: bool,
) -> FlightOffer | None:
    """Stitch two one-way legs into one self-transfer itinerary."""
    if first.destination != second.origin:
        return None

    gap_min = int((second.depart - first.arrive).total_seconds() // 60)
    floor = int(
        policy.get(
            "flight.min_self_transfer_intl_min" if international else "flight.min_self_transfer_min"
        )
    )
    ceiling = int(policy.get("flight.max_self_transfer_gap_min", 600))
    if str(policy.get("flight.allow_overnight", "conditional")).lower() != "never":
        # Let overnight pairings through. They are usually a bad idea, and the
        # cost engine says so out loud -- charging a hotel night and a risk
        # penalty, then listing them under "investigated and rejected". That is
        # far more useful than silently never constructing them.
        ceiling = max(ceiling, int(policy.get("flight.max_overnight_gap_min", 1080)))
    if gap_min < floor or gap_min > ceiling:
        return None

    return FlightOffer(
        segments=first.segments + second.segments,
        price_eur=round(first.price_eur + second.price_eur, 2),
        provider=f"synthesised({first.provider}+{second.provider})",
        fare_brand="self-transfer",
        included_cabin_bags=min(first.included_cabin_bags, second.included_cabin_bags),
        included_checked_bags=min(first.included_checked_bags, second.included_checked_bags),
        self_transfer=True,
        separate_tickets=True,
        confidence=_weakest(first.confidence, second.confidence),
        booking_url=None,
        raw={
            "built_from": [first.key(), second.key()],
            "connection_min": gap_min,
            "via": first.destination,
            "leg_prices": [first.price_eur, second.price_eur],
        },
    )


def build_all(
    firsts: list[FlightOffer],
    seconds: list[FlightOffer],
    policy: Policy,
    international: bool,
    limit: int = 6,
) -> list[FlightOffer]:
    """Cheapest viable pairings through one hub."""
    out: list[FlightOffer] = []
    for a in sorted(firsts, key=lambda o: o.price_eur)[:6]:
        for b in sorted(seconds, key=lambda o: o.price_eur)[:6]:
            merged = combine(a, b, policy, international)
            if merged is not None:
                out.append(merged)
    out.sort(key=lambda o: o.price_eur)
    return out[:limit]


def _weakest(a: Confidence, b: Confidence) -> Confidence:
    from ..models import CONFIDENCE_RANK

    return a if CONFIDENCE_RANK[a] <= CONFIDENCE_RANK[b] else b
