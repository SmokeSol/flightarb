"""Turn discovered legs into complete door-to-door journeys.

The important idea here: **the outbound and the return are priced
independently.**

Consumers think in terms of ``CMN <-> AGP``.  The engine does not.  It holds a
pool of outbound legs and a pool of inbound legs -- which may use different
airports, different carriers and different dates -- and combines them.  That is
how it finds

    out:    RBA -> AGP    EUR 61
    return: AGP -> CMN    EUR 89

which no round-trip search will ever show you, because no round-trip search is
willing to land you back in a different city.

Round trips sold as a single ticket are handled separately (via ``bundle_id``)
and compete against the two-one-way combinations on equal terms.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from itertools import product

from ..models import (
    DirectionPlan,
    FlightOffer,
    Journey,
    JourneySpec,
    Ticketing,
)
from .cost import CostEngine
from .endpoints import Endpoint

LegKey = tuple[str, str, date]  # (origin_iata, destination_iata, depart_date)


@dataclass
class LegPool:
    """Everything discovered so far, indexed for combination."""

    outbound: dict[LegKey, list[FlightOffer]] = field(default_factory=dict)
    inbound: dict[LegKey, list[FlightOffer]] = field(default_factory=dict)
    bundles: dict[str, list[FlightOffer]] = field(default_factory=dict)

    def add_outbound(self, offers: list[FlightOffer]) -> None:
        for o in offers:
            self.outbound.setdefault((o.origin, o.destination, o.depart_date), []).append(o)

    def add_inbound(self, offers: list[FlightOffer]) -> None:
        for o in offers:
            self.inbound.setdefault((o.origin, o.destination, o.depart_date), []).append(o)

    def add_bundle(self, offers: list[FlightOffer]) -> None:
        for o in offers:
            if o.bundle_id:
                self.bundles.setdefault(o.bundle_id, []).append(o)

    @property
    def leg_count(self) -> int:
        return (
            sum(len(v) for v in self.outbound.values())
            + sum(len(v) for v in self.inbound.values())
            + sum(len(v) for v in self.bundles.values())
        )


@dataclass
class Assembler:
    spec: JourneySpec
    cost: CostEngine
    origins: dict[str, Endpoint]       # iata -> ground leg home->airport
    destinations: dict[str, Endpoint]  # iata -> ground leg airport->place

    #: Per endpoint-pair cap, so a single cheap city pair cannot crowd out the
    #: diversity that makes the mixed-airport trick findable.
    per_pair: int = 2
    max_per_direction: int = 10
    max_journeys: int = 400

    # -- direction construction ------------------------------------------- #
    def outbound_plan(self, offer: FlightOffer) -> DirectionPlan | None:
        start = self.origins.get(offer.origin)
        end = self.destinations.get(offer.destination)
        if start is None or end is None:
            return None
        return DirectionPlan(ground_out=start.leg, offer=offer, ground_in=end.leg)

    def inbound_plan(self, offer: FlightOffer) -> DirectionPlan | None:
        # Coming home, both ground hops are travelled in the opposite
        # direction: 'Malaga -> AGP' on the way out becomes 'AGP -> Malaga'.
        start = self.destinations.get(offer.origin)
        end = self.origins.get(offer.destination)
        if start is None or end is None:
            return None
        return DirectionPlan(
            ground_out=start.leg.reversed(), offer=offer, ground_in=end.leg.reversed()
        )

    # -- scoring a single direction --------------------------------------- #
    def direction_score(self, plan: DirectionPlan) -> float:
        """Cheap partial utility, used only to prune before combination."""
        seats = max(1, self.spec.party.seats)
        intl = self.spec.origin.country != self.spec.destination.country
        minutes = plan.door_to_door_min(
            checkin_buffer_min=int(self.cost.checkin_buffer(plan.offer, intl)),
            arrival_buffer_min=int(self.cost.arrival_buffer()),
        )
        return (
            plan.offer.price_eur * seats
            + plan.ground_out.cost_eur
            + plan.ground_in.cost_eur
            + self.cost.time_cost(minutes)
        )

    def _best_plans(self, pool: dict[LegKey, list[FlightOffer]], inbound: bool) -> list[DirectionPlan]:
        """Top plans per endpoint-pair, then globally capped."""
        by_pair: dict[tuple[str, str], list[tuple[float, DirectionPlan]]] = {}
        for (o, d, _day), offers in pool.items():
            for offer in offers:
                if offer.bundle_id:
                    continue  # bundled legs are only sellable as a pair
                plan = self.inbound_plan(offer) if inbound else self.outbound_plan(offer)
                if plan is None:
                    continue
                by_pair.setdefault((o, d), []).append((self.direction_score(plan), plan))

        picked: list[tuple[float, DirectionPlan]] = []
        headline: list[tuple[float, DirectionPlan]] = []
        for pair_plans in by_pair.values():
            pair_plans.sort(key=lambda t: t[0])
            picked.extend(pair_plans[: self.per_pair])
            # Always carry the lowest *fare* forward, even when its door-to-door
            # score is poor. That is the itinerary the traveller would have
            # clicked on, and the engine has to be able to say out loud why it
            # is worse -- a 22h overnight self-transfer silently pruned here
            # never reaches the "investigated and rejected" list.
            cheapest = min(pair_plans, key=lambda t: t[1].offer.price_eur)
            if cheapest not in picked:
                headline.append(cheapest)

        picked.sort(key=lambda t: t[0])
        keep = picked[: self.max_per_direction]
        seen = {id(plan) for _s, plan in keep}
        keep.extend(item for item in headline if id(item[1]) not in seen)
        return [plan for _score, plan in keep]

    # -- journeys ---------------------------------------------------------- #
    def from_bundles(self, pool: LegPool) -> list[Journey]:
        """Round trips the carrier sells as one ticket."""
        out: list[Journey] = []
        for offers in pool.bundles.values():
            if len(offers) == 1 and not self.spec.is_round_trip:
                plan = self.outbound_plan(offers[0])
                if plan is not None:
                    out.append(Journey(plan, None, Ticketing.ONE_WAY))
                continue
            if len(offers) != 2:
                continue
            first, second = sorted(offers, key=lambda o: o.depart)
            ob = self.outbound_plan(first)
            ib = self.inbound_plan(second)
            if ob is None or ib is None:
                continue
            out.append(Journey(ob, ib, Ticketing.RETURN))
        return out

    def build(self, pool: LegPool) -> list[Journey]:
        journeys: list[Journey] = list(self.from_bundles(pool))

        outs = self._best_plans(pool.outbound, inbound=False)
        if not self.spec.is_round_trip:
            journeys.extend(Journey(p, None, Ticketing.ONE_WAY) for p in outs)
            return self._dedupe(journeys)

        backs = self._best_plans(pool.inbound, inbound=True)
        for ob, ib in product(outs, backs):
            if ib.offer.depart <= ob.offer.arrive:
                continue
            journeys.append(Journey(ob, ib, Ticketing.TWO_ONE_WAYS))
            if len(journeys) >= self.max_journeys:
                break
        return self._dedupe(journeys)

    @staticmethod
    def _dedupe(journeys: list[Journey]) -> list[Journey]:
        seen: dict[str, Journey] = {}
        for j in journeys:
            seen.setdefault(j.key(), j)
        return list(seen.values())
