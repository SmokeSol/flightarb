"""Top-level search: explore, rank, verify, re-rank, explain."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from ..models import Journey, JourneySpec
from ..providers.registry import RegistryReport
from ..runtime import Runtime
from ..store import PriceStats
from . import explain, pareto
from .cost import CostEngine
from .endpoints import Endpoint
from .planner import Planner, SearchStats, TraceStep


@dataclass
class SearchResult:
    spec: JourneySpec
    recommendations: pareto.Recommendations
    baseline: Journey | None
    origins: list[Endpoint]
    destinations: list[Endpoint]
    trace: list[TraceStep]
    stats: SearchStats
    providers: RegistryReport
    price_stats: PriceStats | None = None
    warnings: list[str] = field(default_factory=list)
    considered: int = 0

    def headline_for(self, journey: Journey) -> str:
        return explain.headline(journey, self.baseline)

    def why_for(self, journey: Journey) -> list[str]:
        return explain.why(journey, self.baseline)


#: Below this, an origin and a destination are the same place and flying
#: between them is not a question worth answering.
SAME_PLACE_KM = 60.0


def validate_spec(spec: JourneySpec) -> list[str]:
    """Reject nonsense before spending a single provider query.

    Without this the engine cheerfully answers "Malaga to Malaga" with a flight
    to Granada, because Granada is a legitimate alternative arrival airport for
    Malaga -- technically correct, obviously useless.
    """
    from ..geo.airports import haversine_km

    problems: list[str] = []

    if spec.depart_date < date.today():
        problems.append(f"departure date {spec.depart_date} is in the past")
    if spec.return_date is not None and spec.return_date <= spec.depart_date:
        problems.append(
            f"return date {spec.return_date} is not after the departure date {spec.depart_date}"
        )
    if spec.party.seats < 1:
        problems.append("a trip needs at least one fare-paying traveller")

    apart = haversine_km(
        spec.origin.lat, spec.origin.lon, spec.destination.lat, spec.destination.lon
    )
    if apart < SAME_PLACE_KM:
        problems.append(
            f"{spec.origin.name} and {spec.destination.name} are {apart:.0f} km apart "
            f"-- that is the same place, not a flight"
        )
    return problems


def run_search(runtime: Runtime, spec: JourneySpec, on_step=None) -> SearchResult:
    planner = Planner(runtime, spec, on_step=on_step)
    journeys, baseline = planner.run()
    cost: CostEngine = planner.cost

    recs = pareto.select(journeys, cost, baseline)

    # Verification pass: only the finalists, only once they have earned it.
    finalists = _finalists(recs)
    if finalists:
        verified = planner.verify(finalists)
        if verified:
            # Prices moved, so the ranking has to be redone from scratch.
            for j in journeys:
                cost.evaluate(j)
            recs = pareto.select(journeys, cost, baseline)

    warnings: list[str] = []
    if not runtime.registry.has_real_prices:
        warnings.append(
            "All prices come from the SYNTHETIC market simulator -- they are modelled, "
            "not bookable. Enable a real provider for live fares."
        )
    for name, reason in runtime.registry.skipped.items():
        warnings.append(f"provider '{name}' not used: {reason}")
    if baseline is None:
        warnings.append(
            "No itinerary matched the exact requested airports and dates, so savings "
            "are shown without a baseline comparison."
        )

    price_stats = None
    if runtime.store is not None and baseline is not None:
        offer = baseline.outbound.offer
        price_stats = runtime.store.stats(
            offer.origin, offer.destination, max(0, (offer.depart_date - date.today()).days)
        )

    return SearchResult(
        spec=spec,
        recommendations=recs,
        baseline=baseline,
        origins=planner.origins,
        destinations=planner.destinations,
        trace=planner.trace,
        stats=planner.stats,
        providers=runtime.registry.report(),
        price_stats=price_stats,
        warnings=warnings,
        considered=len(journeys),
    )


def _finalists(recs: pareto.Recommendations) -> list[Journey]:
    ordered: list[Journey] = []
    seen: set[str] = set()
    for _label, journey in recs.distinct:
        if journey.key() not in seen:
            seen.add(journey.key())
            ordered.append(journey)
    for journey in sorted(recs.front, key=lambda j: j.cost.utility):
        if journey.key() not in seen:
            seen.add(journey.key())
            ordered.append(journey)
    return ordered
