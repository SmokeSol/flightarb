"""Generalised journey cost.

    JourneyCost = Fare + Baggage + Ground + Hotel + Fees
                + TimeCost + RiskPenalty + ConfidencePenalty

Ranking on airfare is the mistake every consumer tool makes.  A EUR 180 saving
is not a saving if reaching the airport costs EUR 100 and three hours, and a
"cheapest" fare that excludes the bag you are definitely bringing is not the
cheapest fare.

Two responsibilities live here and they are deliberately separate:

* ``violations()`` -- hard constraints. Is this journey allowed at all?
* ``evaluate()``   -- soft economics. Given that it is allowed, what is it
  really worth?

Keeping them apart is what lets the engine say "I found something cheaper and
rejected it, here is why" instead of silently dropping it.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..models import (
    Confidence,
    CostBreakdown,
    DirectionPlan,
    FlightOffer,
    Journey,
    JourneySpec,
    Ticketing,
)
from ..policy import Policy


@dataclass
class CostEngine:
    policy: Policy
    spec: JourneySpec

    # -- buffers ---------------------------------------------------------- #
    def checkin_buffer(self, offer: FlightOffer, international: bool) -> float:
        key = "ground.checkin_buffer_intl_min" if international else "ground.checkin_buffer_domestic_min"
        base = float(self.policy.get(key))
        if self.spec.party.checked_bags > 0:
            base += 15.0  # bag drop queue
        return base

    def arrival_buffer(self) -> float:
        base = float(self.policy.get("ground.arrival_buffer_min"))
        if self.spec.party.checked_bags > 0:
            base += float(self.policy.get("ground.arrival_buffer_checked_bag_min"))
        return base

    # -- money ------------------------------------------------------------ #
    def fare(self, journey: Journey) -> float:
        """Bundled round trips are priced as one ticket; two one-ways add up."""
        seats = max(1, self.spec.party.seats)
        out, back = journey.outbound.offer, (journey.inbound.offer if journey.inbound else None)

        if (
            back is not None
            and out.is_bundled
            and back.is_bundled
            and out.bundle_id == back.bundle_id
        ):
            return float(out.bundle_price_eur) * seats

        total = out.price_eur
        if back is not None:
            total += back.price_eur
        return total * seats

    def bags(self, journey: Journey) -> tuple[float, list[str]]:
        """Price the bags the traveller is actually bringing, per direction."""
        seats = max(1, self.spec.party.seats)
        want_checked = self.spec.party.checked_bags
        want_cabin = self.spec.party.cabin_bags
        total = 0.0
        notes: list[str] = []

        for plan in journey.directions:
            offer = plan.offer
            carrier = offer.carriers[0] if offer.carriers else ""

            included_checked = offer.included_checked_bags * seats
            billable_checked = max(0, want_checked - included_checked)
            if billable_checked:
                fee = self.policy.checked_bag_fee(carrier)
                total += billable_checked * fee
                if fee:
                    notes.append(
                        f"{billable_checked} checked bag(s) on {carrier} "
                        f"@ EUR {fee:.0f} ({offer.route_label})"
                    )

            included_cabin = offer.included_cabin_bags * seats
            billable_cabin = max(0, want_cabin - included_cabin)
            if billable_cabin:
                fee = self.policy.cabin_bag_fee(carrier)
                total += billable_cabin * fee
                if fee:
                    notes.append(
                        f"{billable_cabin} cabin bag(s) on {carrier} @ EUR {fee:.0f}"
                    )
        return total, notes

    def ground(self, journey: Journey) -> float:
        return sum(
            plan.ground_out.cost_eur + plan.ground_in.cost_eur for plan in journey.directions
        )

    def hotel(self, journey: Journey) -> tuple[float, list[str]]:
        rate = float(self.policy.get("economics.overnight_hotel_eur"))
        cost, notes = 0.0, []
        for plan in journey.directions:
            if plan.offer.is_overnight_connection:
                cost += rate
                notes.append(f"overnight connection on {plan.offer.route_label}: +EUR {rate:.0f} hotel")
        return cost, notes

    # -- time --------------------------------------------------------------#
    def door_to_door(self, journey: Journey) -> float:
        total = 0.0
        for plan in journey.directions:
            intl = self._is_international(plan)
            total += plan.door_to_door_min(
                checkin_buffer_min=int(self.checkin_buffer(plan.offer, intl)),
                arrival_buffer_min=int(self.arrival_buffer()),
            )
        return total

    def _is_international(self, plan: DirectionPlan) -> bool:
        return self.spec.origin.country != self.spec.destination.country

    def time_cost(self, minutes: float) -> float:
        hours = minutes / 60.0
        return hours * self.policy.value_of_time * self.policy.time_multiplier(
            self.spec.party.people
        )

    # -- risk --------------------------------------------------------------#
    def risk(self, journey: Journey) -> tuple[float, list[str]]:
        p = self.policy
        total, notes = 0.0, []

        if journey.ticketing == Ticketing.TWO_ONE_WAYS:
            fee = float(p.get("risk.separate_ticket_penalty_eur"))
            total += fee
            notes.append(f"two separate tickets: +EUR {fee:.0f} (a delay on one is not the other's problem)")

        for plan in journey.directions:
            offer = plan.offer
            if offer.self_transfer:
                fee = float(p.get("risk.self_transfer_penalty_eur"))
                total += fee
                notes.append(f"self-transfer on {offer.route_label}: +EUR {fee:.0f}")
            if offer.has_airport_change:
                fee = float(p.get("risk.airport_change_penalty_eur"))
                total += fee
                notes.append(f"airport change in transit on {offer.route_label}: +EUR {fee:.0f}")
            if offer.is_overnight_connection:
                fee = float(p.get("risk.overnight_penalty_eur"))
                total += fee
                notes.append(f"overnight layover on {offer.route_label}: +EUR {fee:.0f}")
            if offer.is_redeye:
                fee = float(p.get("risk.redeye_penalty_eur"))
                total += fee
                notes.append(f"unsociable hours on {offer.route_label}: +EUR {fee:.0f}")

            floor = self._connection_floor(offer)
            for _prev, _nxt, gap in offer.connections:
                if gap < floor:
                    fee = float(p.get("risk.tight_connection_penalty_eur"))
                    total += fee
                    notes.append(
                        f"{gap} min connection on {offer.route_label} is under the "
                        f"{floor} min floor: +EUR {fee:.0f}"
                    )
        return total, notes

    def _connection_floor(self, offer: FlightOffer) -> int:
        p = self.policy
        if not offer.self_transfer:
            return int(p.get("flight.min_connection_min"))
        international = self.spec.origin.country != self.spec.destination.country
        key = "flight.min_self_transfer_intl_min" if international else "flight.min_self_transfer_min"
        return int(p.get(key))

    def confidence_penalty(self, journey: Journey, cash: float) -> tuple[float, list[str]]:
        conf = journey.confidence
        if conf == Confidence.VERIFIED:
            return 0.0, []
        pct = float(
            self.policy.get(
                "risk.synthetic_penalty_pct"
                if conf == Confidence.SYNTHETIC
                else "risk.unverified_penalty_pct"
            )
        )
        if pct <= 0:
            return 0.0, []
        return cash * pct, [f"price not verified at source: +{pct:.0%} uncertainty margin"]

    # -- the whole thing --------------------------------------------------- #
    def evaluate(self, journey: Journey) -> CostBreakdown:
        bags, bag_notes = self.bags(journey)
        hotel, hotel_notes = self.hotel(journey)
        risk, risk_notes = self.risk(journey)
        minutes = self.door_to_door(journey)

        cb = CostBreakdown(
            fare=self.fare(journey),
            bags=bags,
            ground=self.ground(journey),
            hotel=hotel,
            fees=float(self.policy.get("economics.booking_fee_eur", 0.0)),
            door_to_door_min=minutes,
        )
        cb.time_cost = self.time_cost(minutes)
        cb.risk_penalty = risk
        conf_pen, conf_notes = self.confidence_penalty(journey, cb.cash)
        cb.confidence_penalty = conf_pen
        cb.notes = bag_notes + hotel_notes + risk_notes + conf_notes
        journey.cost = cb
        return cb

    # -- hard constraints --------------------------------------------------#
    def violations(self, journey: Journey) -> list[str]:
        p = self.policy
        out: list[str] = []

        if journey.max_stops > p.max_stops:
            out.append(f"{journey.max_stops} stops exceeds the {p.max_stops}-stop limit")

        if journey.self_transfer and not bool(p.get("flight.allow_self_transfer")):
            out.append("self-transfer not allowed by policy")

        if journey.ticketing == Ticketing.TWO_ONE_WAYS and not bool(
            p.get("flight.allow_separate_tickets")
        ):
            out.append("separate tickets not allowed by policy")

        overnight_rule = str(p.get("flight.allow_overnight", "conditional")).lower()
        if overnight_rule == "never":
            for plan in journey.directions:
                if plan.offer.is_overnight_connection:
                    out.append("overnight layover not allowed by policy")
                    break

        if not bool(p.get("flight.allow_airport_change_in_transit")):
            for plan in journey.directions:
                if plan.offer.has_airport_change:
                    out.append("itinerary changes airport mid-transit")
                    break

        earliest, latest = p.earliest_departure, p.latest_arrival
        for plan in journey.directions:
            if plan.offer.depart.time() < earliest:
                out.append(f"{plan.offer.route_label} departs before {earliest:%H:%M}")
            if plan.offer.arrive.time() > latest:
                out.append(f"{plan.offer.route_label} arrives after {latest:%H:%M}")

        for plan in journey.directions:
            if plan.ground_out.minutes > p.max_origin_minutes + 1 and plan is journey.outbound:
                out.append(
                    f"{plan.ground_out.minutes:.0f} min to {plan.offer.origin} exceeds the "
                    f"{p.max_origin_minutes:.0f} min repositioning limit"
                )

        if journey.inbound is not None:
            gap = journey.inbound.offer.depart - journey.outbound.offer.arrive
            if gap.total_seconds() <= 0:
                out.append("return departs before the outbound arrives")

        return out

    def score(self, journey: Journey) -> float:
        """Evaluate and return the ranking metric."""
        return self.evaluate(journey).utility
