"""Say why, in the traveller's terms.

Every recommendation is expressed as a delta against the baseline -- the answer
an ordinary search would have given.  "EUR 295" means nothing on its own;
"save EUR 165 for 55 extra minutes, by leaving from Rabat" is a decision.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..models import Journey, Ticketing
from ..store import PriceStats


@dataclass
class Delta:
    cash: float          # positive = cheaper than baseline
    minutes: float       # positive = slower than baseline
    utility: float       # positive = better overall than baseline

    @property
    def is_saving(self) -> bool:
        return self.cash > 0


def delta_vs(journey: Journey, baseline: Journey | None) -> Delta | None:
    if baseline is None:
        return None
    return Delta(
        cash=baseline.cost.cash - journey.cost.cash,
        minutes=journey.cost.door_to_door_min - baseline.cost.door_to_door_min,
        utility=baseline.cost.utility - journey.cost.utility,
    )


def headline(journey: Journey, baseline: Journey | None) -> str:
    d = delta_vs(journey, baseline)
    if d is None:
        return f"EUR {journey.cost.cash:.0f} all-in, {_hm(journey.cost.door_to_door_min)} door to door"
    if baseline is not None and journey.key() == baseline.key():
        return "this is the obvious route -- nothing beat it"

    if d.cash >= 1 and d.minutes > 5:
        return (
            f"save EUR {d.cash:.0f} for {_hm(d.minutes)} more travel"
        )
    if d.cash >= 1 and d.minutes <= 5:
        return f"save EUR {d.cash:.0f} and it is no slower"
    if d.cash <= -1 and d.minutes < -5:
        return f"costs EUR {-d.cash:.0f} more but saves {_hm(-d.minutes)}"
    return f"EUR {journey.cost.cash:.0f} all-in, {_hm(journey.cost.door_to_door_min)} door to door"


def why(journey: Journey, baseline: Journey | None) -> list[str]:
    """The specific reasons this journey differs from the obvious one."""
    lines: list[str] = []
    ob = journey.outbound

    if baseline is not None:
        base_out = baseline.outbound.offer.origin
        if ob.offer.origin != base_out:
            lines.append(
                f"Leave from {ob.offer.origin} instead of {base_out} "
                f"({ob.ground_out.minutes:.0f} min / EUR {ob.ground_out.cost_eur:.0f} to get there, "
                f"vs {baseline.outbound.ground_out.minutes:.0f} min)"
            )
        base_dest = baseline.outbound.offer.destination
        if ob.offer.destination != base_dest:
            lines.append(
                f"Fly into {ob.offer.destination} instead of {base_dest} "
                f"({ob.ground_in.minutes:.0f} min / EUR {ob.ground_in.cost_eur:.0f} onward)"
            )
        if journey.inbound is not None and baseline.inbound is not None:
            if journey.inbound.offer.destination != baseline.inbound.offer.destination:
                lines.append(
                    f"Return into {journey.inbound.offer.destination} rather than "
                    f"{baseline.inbound.offer.destination} -- the outbound and return were "
                    f"priced independently"
                )
        if journey.outbound.offer.depart_date != baseline.outbound.offer.depart_date:
            shift = (journey.outbound.offer.depart_date - baseline.outbound.offer.depart_date).days
            lines.append(f"Shift the outbound by {shift:+d} day(s)")
        if (
            journey.inbound is not None
            and baseline.inbound is not None
            and journey.inbound.offer.depart_date != baseline.inbound.offer.depart_date
        ):
            shift = (journey.inbound.offer.depart_date - baseline.inbound.offer.depart_date).days
            lines.append(f"Shift the return by {shift:+d} day(s)")

    if journey.ticketing == Ticketing.TWO_ONE_WAYS:
        lines.append("Buy two one-way tickets rather than one round trip")
    if journey.self_transfer:
        lines.append("Includes a self-transfer -- you re-check yourself, nobody protects the connection")

    for note in journey.cost.notes:
        lines.append(note)
    return lines


def deal_score(journey: Journey, stats: PriceStats) -> str | None:
    """Position this fare in our own observed history for the route."""
    if stats.samples < 8:
        return None
    fare = journey.cost.fare
    pct = stats.percentile_of(fare)
    route = journey.outbound.offer.route_label
    if fare <= stats.p10:
        verdict = "unusually good"
    elif fare <= stats.p25:
        verdict = "good"
    elif fare <= stats.median:
        verdict = "about normal"
    else:
        verdict = "above normal"
    return (
        f"EUR {fare:.0f} is {verdict} for {route}: median EUR {stats.median:.0f} "
        f"across {stats.samples} comparable departures we have observed "
        f"(cheapest {stats.minimum:.0f}, dearest {stats.maximum:.0f}) -- "
        f"roughly the {pct:.0f}th percentile"
    )


def _hm(minutes: float) -> str:
    minutes = abs(round(minutes))
    hours, mins = divmod(int(minutes), 60)
    if hours and mins:
        return f"{hours}h{mins:02d}"
    if hours:
        return f"{hours}h"
    return f"{mins} min"
