"""Which airports could plausibly serve this trip?

The naive version of this function returns "airports within 100 km".  That is
the single biggest reason ordinary tools miss the Rabat answer: the useful
question is not how far an airport is, it is how long it takes to get there and
what that costs.

So every candidate is scored in *driving minutes* and *euros*, and the shortlist
is cut on minutes.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..geo.airports import AirportIndex
from ..geo.ground import GroundRouter
from ..models import Airport, GroundLeg, Place
from ..policy import Policy


@dataclass(frozen=True)
class Endpoint:
    """An airport, plus the real cost of using it from/to the traveller's place."""

    airport: Airport
    leg: GroundLeg      # place -> airport (origin side) or airport -> place (dest side)
    minutes: float
    is_baseline: bool   # the airport a normal search would have picked

    @property
    def iata(self) -> str:
        return self.airport.iata

    @property
    def label(self) -> str:
        return self.airport.label


def _shortlist(
    place: Place,
    airports: AirportIndex,
    router: GroundRouter,
    max_minutes: float,
    limit: int,
    reverse: bool,
    search_radius_km: float,
    people: int,
) -> list[Endpoint]:
    # Cast a wide great-circle net, then let the road network do the filtering.
    nearby = airports.near(place.lat, place.lon, radius_km=search_radius_km, limit=24)
    if not nearby:
        return []

    scored: list[tuple[float, Endpoint]] = []
    for airport, _gc_km in nearby:
        if reverse:
            leg = router.leg(
                airport.label, airport.lat, airport.lon, place.label, place.lat, place.lon,
                people=people, max_minutes=max_minutes,
            )
        else:
            leg = router.leg(
                place.label, place.lat, place.lon, airport.label, airport.lat, airport.lon,
                people=people, max_minutes=max_minutes,
            )
        scored.append((leg.minutes, Endpoint(airport, leg, leg.minutes, False)))

    scored.sort(key=lambda t: t[0])
    baseline_iata = scored[0][1].iata

    out: list[Endpoint] = []
    for minutes, ep in scored:
        if minutes > max_minutes and ep.iata != baseline_iata:
            continue
        out.append(
            Endpoint(ep.airport, ep.leg, ep.minutes, is_baseline=(ep.iata == baseline_iata))
        )
        if len(out) >= limit:
            break
    return out


def origin_endpoints(
    place: Place, airports: AirportIndex, router: GroundRouter, policy: Policy, people: int = 1
) -> list[Endpoint]:
    return _shortlist(
        place,
        airports,
        router,
        max_minutes=policy.max_origin_minutes,
        limit=int(policy.get("airports.max_origin_candidates", 4)),
        reverse=False,
        search_radius_km=max(250.0, policy.max_origin_minutes * 2.2),
        people=people,
    )


def destination_endpoints(
    place: Place, airports: AirportIndex, router: GroundRouter, policy: Policy, people: int = 1
) -> list[Endpoint]:
    return _shortlist(
        place,
        airports,
        router,
        max_minutes=policy.max_destination_minutes,
        limit=int(policy.get("airports.max_destination_candidates", 4)),
        reverse=True,
        search_radius_km=max(250.0, policy.max_destination_minutes * 2.2),
        people=people,
    )


def baseline_of(endpoints: list[Endpoint]) -> Endpoint:
    """What an ordinary search would have used: the closest airport."""
    for ep in endpoints:
        if ep.is_baseline:
            return ep
    return endpoints[0]
