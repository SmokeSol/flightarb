"""Reduce hundreds of candidates to the three that matter.

Ordinary flight sites return 87 results sorted by price and make the traveller
do the reasoning.  The engine's job is the opposite: do the reasoning, then
show almost nothing.

A journey is kept only if nothing else is at least as good on *every* axis --
cash, door-to-door time, and friction.  From that Pareto set, three named
answers are drawn, plus an explicit list of what was rejected and why.  The
rejected list matters more than it looks: it is the proof that the alternative
was actually investigated rather than never considered.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..models import Journey, Ticketing
from .cost import CostEngine


def friction(journey: Journey) -> float:
    """How much hassle, independent of time and money."""
    score = 0.0
    for plan in journey.directions:
        offer = plan.offer
        score += offer.stops
        score += 2.0 if offer.self_transfer else 0.0
        score += 2.0 if offer.has_airport_change else 0.0
        score += 2.0 if offer.is_overnight_connection else 0.0
        score += 1.0 if offer.is_redeye else 0.0
        score += 0.5 if plan.ground_out.minutes > 60 else 0.0
        score += 0.5 if plan.ground_in.minutes > 60 else 0.0
    if journey.ticketing == Ticketing.TWO_ONE_WAYS:
        score += 1.0
    return score


def objectives(journey: Journey) -> tuple[float, float, float]:
    return (journey.cost.cash, journey.cost.door_to_door_min, friction(journey))


def pareto_front(journeys: list[Journey]) -> list[Journey]:
    """Non-dominated set. O(n^2), and n is small by the time we get here."""
    front: list[Journey] = []
    scored = [(objectives(j), j) for j in journeys]
    for obj_a, ja in scored:
        dominated = False
        for obj_b, jb in scored:
            if jb is ja:
                continue
            if all(b <= a for a, b in zip(obj_a, obj_b)) and any(
                b < a for a, b in zip(obj_a, obj_b)
            ):
                dominated = True
                break
        if not dominated:
            front.append(ja)
    return front


@dataclass
class Rejection:
    journey: Journey
    reason: str


@dataclass
class Recommendations:
    cheapest: Journey | None = None
    best_value: Journey | None = None
    easiest: Journey | None = None
    front: list[Journey] = field(default_factory=list)
    rejected: list[Rejection] = field(default_factory=list)

    @property
    def distinct(self) -> list[tuple[str, Journey]]:
        """The named picks, de-duplicated, in presentation order."""
        out: list[tuple[str, Journey]] = []
        seen: set[str] = set()
        for label, journey in (
            ("BEST VALUE", self.best_value),
            ("CHEAPEST", self.cheapest),
            ("EASIEST", self.easiest),
        ):
            if journey is None:
                continue
            key = journey.key()
            if key in seen:
                # Same journey wins two categories -- say so rather than
                # printing it twice.
                for i, (existing_label, existing) in enumerate(out):
                    if existing.key() == key:
                        out[i] = (f"{existing_label} + {label}", existing)
                        break
                continue
            seen.add(key)
            out.append((label, journey))
        return out


def select(
    journeys: list[Journey],
    cost: CostEngine,
    baseline: Journey | None = None,
) -> Recommendations:
    """Split candidates into feasible / rejected, then pick the three."""
    rec = Recommendations()
    feasible: list[Journey] = []

    for j in journeys:
        problems = cost.violations(j)
        if problems:
            j.rejected_reason = "; ".join(problems)
            rec.rejected.append(Rejection(j, j.rejected_reason))
            continue
        feasible.append(j)

    if not feasible:
        return rec

    for j in feasible:
        if j.cost.door_to_door_min <= 0:
            cost.evaluate(j)

    rec.front = pareto_front(feasible)
    rec.cheapest = min(feasible, key=lambda j: (j.cost.cash, j.cost.utility))
    rec.best_value = min(feasible, key=lambda j: (j.cost.utility, j.cost.cash))
    rec.easiest = min(
        feasible, key=lambda j: (friction(j), j.cost.door_to_door_min, j.cost.utility)
    )

    # A journey can win on continuous utility and still not be worth *changing
    # your plans for*. The thresholds in [economics] are that second hurdle:
    # people need more than break-even to accept a hassle they did not ask for.
    if baseline is not None and rec.best_value is not None:
        ok, reason = clears_hurdle(rec.best_value, baseline, cost)
        if not ok:
            rec.rejected.append(Rejection(rec.best_value, reason))
            rec.best_value = baseline

    rec.rejected.extend(_economic_rejections(feasible, rec, cost, baseline))
    return rec


def clears_hurdle(journey: Journey, baseline: Journey, cost: CostEngine) -> tuple[bool, str]:
    """Is this alternative enough better than the obvious route to suggest it?"""
    if journey.key() == baseline.key():
        return True, ""
    p = cost.policy
    saving = baseline.cost.cash - journey.cost.cash
    extra_hours = max(
        0.0, (journey.cost.door_to_door_min - baseline.cost.door_to_door_min) / 60.0
    )

    required = float(p.get("economics.min_saving_to_recommend"))
    drivers: list[str] = []
    if extra_hours > 0:
        hourly = extra_hours * float(p.get("economics.min_saving_per_extra_hour"))
        if hourly > required:
            required, drivers = hourly, [f"{extra_hours:.1f}h extra travel"]
    for flag, key, label in (
        (journey.self_transfer, "economics.min_saving_for_self_transfer", "a self-transfer"),
        (
            journey.ticketing == Ticketing.TWO_ONE_WAYS,
            "economics.min_saving_for_separate_tickets",
            "two separate tickets",
        ),
        (
            any(o.has_airport_change for o in journey.offers),
            "economics.min_saving_for_airport_change",
            "an airport change",
        ),
    ):
        if flag:
            threshold = float(p.get(key))
            if threshold > required:
                required, drivers = threshold, [label]

    if saving >= required:
        return True, ""
    because = f" for {drivers[0]}" if drivers else ""
    return False, (
        f"saves only EUR {saving:.0f}; your policy asks for at least "
        f"EUR {required:.0f}{because} before changing the obvious route"
    )


def _economic_rejections(
    feasible: list[Journey],
    rec: Recommendations,
    cost: CostEngine,
    baseline: Journey | None,
) -> list[Rejection]:
    """The interesting rejections: allowed, cheap on paper, still not worth it.

    This is where the engine earns trust -- it shows the EUR 215 itinerary it
    found and explains that the overnight self-transfer makes it really EUR 347.
    """
    out: list[Rejection] = []
    policy = cost.policy
    chosen = {j.key() for _label, j in rec.distinct}

    by_fare = sorted(feasible, key=lambda j: j.cost.fare)
    best_utility = rec.best_value.cost.utility if rec.best_value else None

    for j in by_fare[:6]:
        if j.key() in chosen or best_utility is None:
            continue
        fare_gap = (rec.best_value.cost.fare - j.cost.fare) if rec.best_value else 0.0
        if fare_gap <= 0:
            continue
        loss = j.cost.utility - best_utility
        if loss < 1.0:
            continue  # "EUR 0 worse" is not a reason
        out.append(
            Rejection(
                j,
                f"fare is EUR {fare_gap:.0f} cheaper, but real cost is "
                f"EUR {loss:.0f} worse once "
                f"{_dominant_extra(j)} is counted",
            )
        )

    # Alternatives that clear the policy but not the traveller's own thresholds.
    if baseline is not None:
        min_saving = float(policy.get("economics.min_saving_to_recommend"))
        for j in feasible:
            if j.key() in chosen or j.key() == baseline.key():
                continue
            saving = baseline.cost.cash - j.cost.cash
            if 0 < saving < min_saving and j.cost.door_to_door_min > baseline.cost.door_to_door_min:
                out.append(
                    Rejection(
                        j,
                        f"saves only EUR {saving:.0f} for "
                        f"{(j.cost.door_to_door_min - baseline.cost.door_to_door_min) / 60:.1f}h more "
                        f"travel -- under your EUR {min_saving:.0f} threshold",
                    )
                )
    return out[:6]


def _dominant_extra(journey: Journey) -> str:
    c = journey.cost
    parts = [
        ("bags", c.bags),
        ("ground transport", c.ground),
        ("a hotel night", c.hotel),
        ("travel time", c.time_cost),
        ("connection risk", c.risk_penalty),
    ]
    label, value = max(parts, key=lambda t: t[1])
    return label if value > 0 else "the full door-to-door cost"
