"""Adaptive best-first exploration of the journey space.

The naive approach is a nested loop:

    4 origins x 5 destinations x 7 dates x 7 return dates = 980 searches

which is slow, rude to the data sources, and mostly wasted -- the overwhelming
majority of those combinations are obviously bad after the first few probes.

Instead the planner treats the space as a set of independent *dimensions*
(origin airport, destination airport, outbound date shift, return date shift,
ticketing mode) and learns, online, what each dimension is worth.  After
probing ``RBA`` once and finding it EUR 150 better, "switch origin to RBA"
carries a learned effect, and every unexplored configuration containing RBA is
predicted to be good and gets visited early.  Dimensions that turn out not to
matter stop being explored.

That is the difference between a search agent and a ``for`` loop: the order of
exploration is decided by what has already been learned.
"""

from __future__ import annotations

import heapq
import math
import time
from dataclasses import dataclass, field
from datetime import date, timedelta

from ..models import Confidence, FlightOffer, Journey, JourneySpec, Ticketing
from ..providers.base import SearchQuery
from ..runtime import Runtime
from .assemble import Assembler, LegPool
from .cost import CostEngine
from . import synth_connect
from .endpoints import Endpoint, baseline_of, destination_endpoints, origin_endpoints

# --------------------------------------------------------------------------- #
# Search space
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Config:
    """One point in the search space."""

    origin: str
    destination: str
    dep_shift: int
    ret_shift: int
    mode: str  # "rt" = one round-trip ticket, "ow" = two independent one-ways

    def dims(self) -> tuple[tuple[str, object], ...]:
        return (
            ("origin", self.origin),
            ("destination", self.destination),
            ("dep_shift", self.dep_shift),
            ("ret_shift", self.ret_shift),
            ("mode", self.mode),
        )

    def label(self) -> str:
        shift = ""
        if self.dep_shift or self.ret_shift:
            shift = f" {self.dep_shift:+d}/{self.ret_shift:+d}d"
        return f"{self.origin}->{self.destination}{shift} [{self.mode}]"


@dataclass
class DimStats:
    """What we have learned about one dimension value."""

    n: int = 0
    total_delta: float = 0.0

    @property
    def mean(self) -> float:
        return self.total_delta / self.n if self.n else 0.0


@dataclass
class TraceStep:
    config: str
    queries: int
    offers: int
    best_utility: float | None
    note: str = ""


@dataclass
class SearchStats:
    queries_used: int = 0
    configs_visited: int = 0
    journeys_built: int = 0
    seconds: float = 0.0
    stopped_because: str = ""
    verified: int = 0


# --------------------------------------------------------------------------- #
# Planner
# --------------------------------------------------------------------------- #


class Planner:
    #: Prior belief about how much a dimension move is worth, as a fraction of
    #: the baseline utility. Negative = "we suspect this direction is cheaper".
    #: These only decide *exploration order*; observed results overwrite them
    #: as soon as a value has been probed once.
    PRIORS = {
        "origin": -0.10,
        "destination": -0.06,
        "dep_shift": -0.08,
        "ret_shift": -0.08,
        "mode": -0.05,
    }
    EXPLORATION_C = 0.06

    def __init__(self, runtime: Runtime, spec: JourneySpec, on_step=None):
        self.rt = runtime
        self.spec = spec
        self.policy = runtime.policy
        self.cost = CostEngine(self.policy, spec)
        self.on_step = on_step

        people = max(1, spec.party.people)
        self.origins: list[Endpoint] = origin_endpoints(
            spec.origin, runtime.airports, runtime.router, self.policy, people
        )
        self.destinations: list[Endpoint] = destination_endpoints(
            spec.destination, runtime.airports, runtime.router, self.policy, people
        )
        if not self.origins or not self.destinations:
            raise LookupError("no usable airports within the configured travel time")

        self.origin_by_iata = {e.iata: e for e in self.origins}
        self.dest_by_iata = {e.iata: e for e in self.destinations}
        self.assembler = Assembler(spec, self.cost, self.origin_by_iata, self.dest_by_iata)

        self.pool = LegPool()
        self.stats = SearchStats()
        self.trace: list[TraceStep] = []
        self.dim_stats: dict[tuple[str, object], DimStats] = {}
        self.visited: set[Config] = set()
        self.baseline_utility: float | None = None
        self.best_utility: float | None = None

        self._dep_shifts = self._shift_range(spec.date_flex_days)
        self._ret_shifts = (
            self._shift_range(spec.date_flex_days + spec.trip_length_flex_days)
            if spec.is_round_trip
            else [0]
        )

    @staticmethod
    def _shift_range(flex: int) -> list[int]:
        return list(range(-flex, flex + 1)) if flex > 0 else [0]

    # -- configuration -> provider queries --------------------------------- #
    def root(self) -> Config:
        return Config(
            origin=baseline_of(self.origins).iata,
            destination=baseline_of(self.destinations).iata,
            dep_shift=0,
            ret_shift=0,
            mode="rt" if self.spec.is_round_trip else "ow",
        )

    def queries_for(self, cfg: Config) -> list[SearchQuery]:
        dep = self.spec.depart_date + timedelta(days=cfg.dep_shift)
        if dep < date.today():
            return []
        seats = max(1, self.spec.party.seats)
        common = dict(seats=seats, cabin=self.spec.cabin, max_stops=self.policy.max_stops)

        if not self.spec.is_round_trip:
            return [SearchQuery(cfg.origin, cfg.destination, dep, None, **common)]

        ret = self.spec.return_date + timedelta(days=cfg.ret_shift)
        if ret <= dep:
            return []
        if cfg.mode == "rt":
            return [SearchQuery(cfg.origin, cfg.destination, dep, ret, **common)]
        return [
            SearchQuery(cfg.origin, cfg.destination, dep, None, **common),
            SearchQuery(cfg.destination, cfg.origin, ret, None, **common),
        ]

    # -- ingestion ---------------------------------------------------------- #
    def _ingest(self, cfg: Config, query: SearchQuery, offers: list[FlightOffer]) -> None:
        if not offers:
            return
        bundled = [o for o in offers if o.bundle_id]
        loose = [o for o in offers if not o.bundle_id]

        for o in bundled:
            # Bundle ids are only unique within a provider response; namespace
            # them so two configurations cannot collide.
            o.bundle_id = f"{cfg.label()}|{o.provider}|{o.bundle_id}"
        self.pool.add_bundle(bundled)

        if query.origin in self.origin_by_iata and query.destination in self.dest_by_iata:
            self.pool.add_outbound(loose)
        elif query.origin in self.dest_by_iata and query.destination in self.origin_by_iata:
            self.pool.add_inbound(loose)

    # -- scoring ------------------------------------------------------------ #
    def _effect(self, dim: str, value: object, base: float) -> tuple[float, int]:
        stats = self.dim_stats.get((dim, value))
        if stats is None or stats.n == 0:
            return self.PRIORS.get(dim, 0.0) * base, 0
        return stats.mean, stats.n

    def predict(self, cfg: Config) -> float:
        """Predicted utility, minus an exploration bonus. Lower is better."""
        base = self.baseline_utility or 1000.0
        root = self.root()
        total = base
        novelty = 0.0
        for dim, value in cfg.dims():
            if value == dict(root.dims())[dim]:
                continue  # unchanged from root contributes nothing
            effect, n = self._effect(dim, value, base)
            total += effect
            novelty += 1.0 / math.sqrt(1 + n)
        return total - self.EXPLORATION_C * base * novelty

    def _learn(self, cfg: Config, utility: float) -> None:
        base = self.baseline_utility
        if base is None:
            return
        root_dims = dict(self.root().dims())
        delta = utility - base
        changed = [(d, v) for d, v in cfg.dims() if v != root_dims[d]]
        if not changed:
            return
        # Attribute the observed delta evenly across the dimensions that moved.
        share = delta / len(changed)
        for dim, value in changed:
            st = self.dim_stats.setdefault((dim, value), DimStats())
            st.n += 1
            st.total_delta += share

    # -- neighbours --------------------------------------------------------- #
    def neighbours(self, cfg: Config) -> list[Config]:
        out: list[Config] = []
        for ep in self.origins:
            if ep.iata != cfg.origin:
                out.append(Config(ep.iata, cfg.destination, cfg.dep_shift, cfg.ret_shift, cfg.mode))
        for ep in self.destinations:
            if ep.iata != cfg.destination:
                out.append(Config(cfg.origin, ep.iata, cfg.dep_shift, cfg.ret_shift, cfg.mode))
        for shift in self._dep_shifts:
            if shift != cfg.dep_shift:
                out.append(Config(cfg.origin, cfg.destination, shift, cfg.ret_shift, cfg.mode))
        for shift in self._ret_shifts:
            if shift != cfg.ret_shift:
                out.append(Config(cfg.origin, cfg.destination, cfg.dep_shift, shift, cfg.mode))
        if self.spec.is_round_trip:
            other = "ow" if cfg.mode == "rt" else "rt"
            out.append(Config(cfg.origin, cfg.destination, cfg.dep_shift, cfg.ret_shift, other))
        return [c for c in out if c not in self.visited]

    # -- evaluation --------------------------------------------------------- #
    def _current_best(self) -> tuple[float | None, list[Journey]]:
        journeys = self.assembler.build(self.pool)
        self.stats.journeys_built = len(journeys)
        scored: list[Journey] = []
        for j in journeys:
            if self.cost.violations(j):
                continue
            self.cost.evaluate(j)
            scored.append(j)
        if not scored:
            return None, journeys
        return min(j.cost.utility for j in scored), journeys

    def visit(self, cfg: Config) -> float | None:
        """Spend budget on one configuration; return the best utility after it."""
        self.visited.add(cfg)
        queries = self.queries_for(cfg)
        if not queries:
            return None

        results = self.rt.registry.search_many(queries)
        offers_seen = 0
        for q in queries:
            offers = results.get(q.key(), [])
            offers_seen += len(offers)
            self._ingest(cfg, q, offers)
            if self.rt.store is not None:
                self.rt.store.record(offers)

        self.stats.queries_used += len(queries)
        self.stats.configs_visited += 1

        utility, _ = self._current_best()
        step = TraceStep(cfg.label(), len(queries), offers_seen, utility)
        self.trace.append(step)
        if self.on_step is not None:
            self.on_step(step)
        return utility

    # -- the loop ----------------------------------------------------------- #
    def run(self) -> tuple[list[Journey], Journey | None]:
        started = time.monotonic()
        budget = self.policy.query_budget
        max_seconds = float(self.policy.get("search.max_wall_seconds", 120))
        patience = int(self.policy.get("search.patience", 12))
        min_gain = float(self.policy.get("search.min_improvement_eur", 5.0))
        prune_pct = float(self.policy.get("search.prune_threshold_pct", 0.30))

        # 1. Baseline: exactly what an ordinary search would have done.
        root = self.root()
        self.baseline_utility = self.visit(root)
        baseline_journey = self._baseline_journey()
        if self.baseline_utility is None:
            self.baseline_utility = 1000.0
        self.best_utility = self.baseline_utility

        # 1b. Price every airport pair once so mixed-airport itineraries are
        # reachable by construction rather than by accident.
        if bool(self.policy.get("search.leg_coverage", True)):
            self.ensure_leg_coverage(budget - self.stats.queries_used)
            covered, _ = self._current_best()
            if covered is not None:
                self.best_utility = min(self.best_utility, covered)

        # 2. Best-first expansion.
        counter = 0
        frontier: list[tuple[float, int, Config]] = []
        for cand in self.neighbours(root):
            counter += 1
            heapq.heappush(frontier, (self.predict(cand), counter, cand))

        stale = 0
        while frontier:
            if self.stats.queries_used >= budget:
                self.stats.stopped_because = f"query budget exhausted ({budget})"
                break
            if time.monotonic() - started > max_seconds:
                self.stats.stopped_because = f"wall-clock limit ({max_seconds:.0f}s)"
                break
            if stale >= patience:
                self.stats.stopped_because = f"no improvement in {patience} expansions"
                break

            predicted, _, cfg = heapq.heappop(frontier)
            if cfg in self.visited:
                continue
            # Prune: this branch is predicted to be far worse than what we hold.
            if self.best_utility is not None and predicted > self.best_utility * (1 + prune_pct):
                self.trace.append(
                    TraceStep(cfg.label(), 0, 0, None, "pruned: predicted well below best")
                )
                continue

            utility = self.visit(cfg)
            if utility is None:
                stale += 1
                continue

            self._learn(cfg, utility)
            improved = utility < (self.best_utility or float("inf")) - min_gain
            self.best_utility = min(self.best_utility or utility, utility)
            stale = 0 if improved else stale + 1

            # Always expand. A configuration that did not improve on its own can
            # still lead somewhere good -- RBA alone may lose, while RBA on a
            # Tuesday wins. Priority ordering and the prune threshold decide what
            # actually gets visited; the budget decides when to stop.
            for cand in self.neighbours(cfg):
                counter += 1
                heapq.heappush(frontier, (self.predict(cand), counter, cand))

        if not self.stats.stopped_because:
            self.stats.stopped_because = "search space exhausted"

        # 3. Construct itineraries no source sells: self-transfers through a hub.
        if bool(self.policy.get("search.synthesize_self_transfer", True)):
            self.synthesize_self_transfers(budget - self.stats.queries_used)

        self.stats.seconds = time.monotonic() - started

        journeys = self.assembler.build(self.pool)
        for j in journeys:
            self.cost.evaluate(j)
        return journeys, baseline_journey

    # -- leg coverage --------------------------------------------------------- #
    def ensure_leg_coverage(self, budget_left: int) -> int:
        """Price every airport pair once, in both directions, on the requested
        dates.

        This is the cheapest high-value move the planner makes.  One-way legs
        recombine freely, so |origins| x |destinations| x 2 queries unlock
        *every* mixed-airport itinerary -- out of Rabat, back into Casablanca --
        without searching for each combination separately.  Leaving it to the
        best-first loop made the feature depend on which configurations it
        happened to visit, which is not a feature, it is luck.
        """
        dep = self.spec.depart_date
        ret = self.spec.return_date
        seats = max(1, self.spec.party.seats)
        common = dict(seats=seats, cabin=self.spec.cabin, max_stops=self.policy.max_stops)

        wanted: list[SearchQuery] = []
        for o in self.origins:
            for d in self.destinations:
                wanted.append(SearchQuery(o.iata, d.iata, dep, None, **common))
                if ret is not None:
                    wanted.append(SearchQuery(d.iata, o.iata, ret, None, **common))

        if len(wanted) > budget_left:
            # Not enough budget for the full grid: prioritise the closest
            # airports, which is where a usable alternative most likely lives.
            wanted = wanted[:max(0, budget_left)]
        if not wanted:
            return 0

        results = self.rt.registry.search_many(wanted)
        offers_seen = 0
        cfg = self.root()
        for q in wanted:
            offers = results.get(q.key(), [])
            offers_seen += len(offers)
            self._ingest(cfg, q, offers)
            if self.rt.store is not None:
                self.rt.store.record(offers)
        self.stats.queries_used += len(wanted)

        step = TraceStep(
            "leg coverage",
            len(wanted),
            offers_seen,
            self._current_best()[0],
            f"priced {len(self.origins)}x{len(self.destinations)} airport pairs "
            f"independently in both directions",
        )
        self.trace.append(step)
        if self.on_step is not None:
            self.on_step(step)
        return offers_seen

    # -- self-transfer synthesis --------------------------------------------- #
    def synthesize_self_transfers(self, budget_left: int) -> int:
        """Probe a few hubs and stitch two one-way legs into one journey.

        Runs last and only on leftover budget: it is a bonus capability, never
        a reason to starve the main search.
        """
        if budget_left < 4:
            return 0
        root = self.root()
        origin = self.rt.airports.get(root.origin)
        destination = self.rt.airports.get(root.destination)
        if origin is None or destination is None:
            return 0

        hubs = synth_connect.candidate_hubs(
            origin,
            destination,
            self.rt.airports,
            places=self.rt.places,
            limit=int(self.policy.get("search.self_transfer_hubs", 3)),
        )
        if not hubs:
            return 0

        international = self.spec.origin.country != self.spec.destination.country
        seats = max(1, self.spec.party.seats)
        common = dict(seats=seats, cabin=self.spec.cabin, max_stops=0)
        dep = self.spec.depart_date

        built = 0
        for hub in hubs:
            if self.stats.queries_used + 2 > self.policy.query_budget:
                break
            code = hub.airport.iata
            queries = [
                SearchQuery(origin.iata, code, dep, None, **common),
                SearchQuery(code, destination.iata, dep, None, **common),
                # The second leg may well be the next morning.
                SearchQuery(code, destination.iata, dep + timedelta(days=1), None, **common),
            ]
            results = self.rt.registry.search_many(queries)
            self.stats.queries_used += len(queries)

            firsts = results.get(queries[0].key(), [])
            seconds = results.get(queries[1].key(), []) + results.get(queries[2].key(), [])
            if not firsts or not seconds:
                continue

            merged = synth_connect.build_all(firsts, seconds, self.policy, international)
            if merged:
                self.pool.add_outbound(merged)
                built += len(merged)
                step = TraceStep(
                    f"self-transfer via {code}",
                    len(queries),
                    len(merged),
                    None,
                    f"stitched {len(merged)} itinerary(ies) nobody sells",
                )
                self.trace.append(step)
                if self.on_step is not None:
                    self.on_step(step)
        return built

    # -- baseline ------------------------------------------------------------ #
    def _baseline_journey(self) -> Journey | None:
        """The answer an ordinary CMN->AGP search would have produced: closest
        airports, exact dates, best price. Everything is measured against it."""
        root = self.root()
        dep = self.spec.depart_date
        ret = self.spec.return_date

        candidates: list[Journey] = []
        for j in self.assembler.build(self.pool):
            if j.outbound.offer.origin != root.origin:
                continue
            if j.outbound.offer.destination != root.destination:
                continue
            if j.outbound.offer.depart_date != dep:
                continue
            if j.inbound is not None:
                if j.inbound.offer.depart_date != ret:
                    continue
                if j.inbound.offer.destination != root.origin:
                    continue
            if self.cost.violations(j):
                continue
            self.cost.evaluate(j)
            candidates.append(j)
        if not candidates:
            return None
        best = min(candidates, key=lambda j: j.cost.utility)
        best.tags.add("baseline")
        return best

    # -- verification -------------------------------------------------------- #
    def verify(self, journeys: list[Journey]) -> int:
        """Re-price finalists at the operating carrier. Discovery is allowed to
        be approximate; what we finally show should not be."""
        if not self.rt.registry.can_verify():
            return 0
        limit = int(self.policy.get("search.finalists_to_verify", 5))
        targets = journeys[:limit]

        offers: list[FlightOffer] = []
        for j in targets:
            offers.extend(o for o in j.offers if o.confidence != Confidence.VERIFIED)
        if not offers:
            return 0

        verified = self.rt.registry.verify_many(offers)
        if not verified:
            return 0

        count = 0
        for j in targets:
            changed = False
            for plan in j.directions:
                replacement = verified.get(plan.offer.key())
                if replacement is not None:
                    plan.offer = replacement
                    changed = True
            if changed:
                j.tags.add("verified")
                self.cost.evaluate(j)
                count += 1
        self.stats.verified = count
        return count
