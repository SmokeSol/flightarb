"""Provider registry: fan-out, merge, escalate.

Ordering policy
---------------
1. Cheap discovery adapters run first, in parallel.
2. The browser adapter is an *escalation*, not a peer -- it only runs when the
   cheap ones came back empty for a leg the planner still wants.
3. Verification adapters never participate in discovery; the planner calls them
   explicitly on finalists.

Every adapter is blocking I/O, so a thread pool is the right concurrency
primitive here -- no async plumbing to maintain, and ``requests``/Playwright's
sync API drop straight in.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from ..models import FlightOffer
from .base import FlightProvider, ProviderContext, SearchQuery

_BUILDERS: dict[str, str] = {
    "synthetic": "flightarb.providers.synthetic:SyntheticProvider",
    "ryanair": "flightarb.providers.ryanair:RyanairProvider",
    "fast-flights": "flightarb.providers.fastflights:FastFlightsProvider",
    "browser": "flightarb.providers.browser:BrowserProvider",
    "airline-direct": "flightarb.providers.airline_direct:AirlineDirectProvider",
}

FALLBACK_PROVIDERS = {"browser"}


def _load(path: str):
    module_name, _, class_name = path.partition(":")
    import importlib

    return getattr(importlib.import_module(module_name), class_name)


@dataclass
class RegistryReport:
    enabled: list[str] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)
    queries: int = 0
    cache_hits: int = 0
    offers: int = 0
    failures: int = 0
    per_provider: dict[str, dict] = field(default_factory=dict)


class ProviderRegistry:
    def __init__(self, ctx: ProviderContext, names: Sequence[str] | None = None):
        self.ctx = ctx
        wanted = list(names if names is not None else ctx.policy.get("providers.enabled", ["synthetic"]))

        self.providers: list[FlightProvider] = []
        self.verifiers: list[FlightProvider] = []
        self.skipped: dict[str, str] = {}

        # A verifier is always constructed: the planner needs it for finalists
        # even when it is not part of the discovery list.
        for name in dict.fromkeys(list(wanted) + ["airline-direct"]):
            spec = _BUILDERS.get(name)
            if spec is None:
                self.skipped[name] = "unknown provider"
                continue
            try:
                provider = _load(spec)(ctx)
            except Exception as exc:
                self.skipped[name] = f"failed to construct: {exc}"
                continue
            if not provider.available():
                self.skipped[name] = provider.unavailable_reason() or "unavailable"
                if not provider.is_verifier:
                    continue
            if provider.is_verifier:
                if provider.available():
                    self.verifiers.append(provider)
                continue
            if name in wanted:
                self.providers.append(provider)

        self.primary = [p for p in self.providers if p.name not in FALLBACK_PROVIDERS]
        self.fallback = [p for p in self.providers if p.name in FALLBACK_PROVIDERS]
        self.parallelism = max(1, int(ctx.policy.get("search.parallelism", 6)))

    # ------------------------------------------------------------------ #
    @property
    def has_real_prices(self) -> bool:
        return any(p.real_prices for p in self.providers)

    @property
    def names(self) -> list[str]:
        return [p.name for p in self.providers]

    def search(self, query: SearchQuery) -> list[FlightOffer]:
        """One leg, every primary provider, escalating to the browser only if
        nothing came back."""
        offers = self._fan_out(self.primary, query)
        if not offers and self.fallback:
            offers = self._fan_out(self.fallback, query)
        return _dedupe(offers)

    def search_many(self, queries: Iterable[SearchQuery]) -> dict[str, list[FlightOffer]]:
        """Run many legs concurrently. Keyed by ``SearchQuery.key()``."""
        queries = list(queries)
        if not queries:
            return {}
        out: dict[str, list[FlightOffer]] = {}
        with ThreadPoolExecutor(max_workers=self.parallelism) as pool:
            futures = {pool.submit(self.search, q): q for q in queries}
            for fut in as_completed(futures):
                q = futures[fut]
                try:
                    out[q.key()] = fut.result()
                except Exception:
                    out[q.key()] = []
        return out

    def _fan_out(self, providers: Sequence[FlightProvider], query: SearchQuery) -> list[FlightOffer]:
        active = [p for p in providers if p.available()]
        if not active:
            return []
        if len(active) == 1:
            return list(active[0].search(query))
        merged: list[FlightOffer] = []
        with ThreadPoolExecutor(max_workers=min(len(active), self.parallelism)) as pool:
            for fut in as_completed([pool.submit(p.search, query) for p in active]):
                try:
                    merged.extend(fut.result())
                except Exception:
                    continue
        return merged

    # ------------------------------------------------------------------ #
    def can_verify(self) -> bool:
        return bool(self.verifiers)

    def verify(self, offer: FlightOffer) -> FlightOffer | None:
        for v in self.verifiers:
            if not v.available():
                continue
            try:
                result = v.verify(offer)
            except Exception:
                continue
            if result is not None:
                return result
        return None

    def verify_many(self, offers: Sequence[FlightOffer]) -> dict[str, FlightOffer]:
        if not offers or not self.verifiers:
            return {}
        out: dict[str, FlightOffer] = {}
        with ThreadPoolExecutor(max_workers=min(len(offers), self.parallelism)) as pool:
            futures = {pool.submit(self.verify, o): o for o in offers}
            for fut in as_completed(futures):
                src = futures[fut]
                try:
                    res = fut.result()
                except Exception:
                    res = None
                if res is not None:
                    out[src.key()] = res
        return out

    # ------------------------------------------------------------------ #
    def report(self) -> RegistryReport:
        rep = RegistryReport(enabled=self.names, skipped=dict(self.skipped))
        for p in self.providers + self.verifiers:
            s = p.stats
            rep.queries += s.queries
            rep.cache_hits += s.cache_hits
            rep.offers += s.offers
            rep.failures += s.failures
            rep.per_provider[p.name] = {
                "queries": s.queries,
                "cache_hits": s.cache_hits,
                "offers": s.offers,
                "failures": s.failures,
                "seconds": round(s.seconds, 2),
            }
        return rep


def _dedupe(offers: Iterable[FlightOffer]) -> list[FlightOffer]:
    """Same flight from two sources: keep the more trustworthy, then cheaper."""
    from ..models import CONFIDENCE_RANK

    best: dict[tuple, FlightOffer] = {}
    for o in offers:
        if o.bundle_id is not None:
            key = ("bundle", o.provider, o.bundle_id, o.route_label, o.depart.isoformat())
        else:
            key = ("leg", o.route_label, o.depart.isoformat(), o.carriers, round(o.price_eur, 2))
        current = best.get(key)
        if current is None:
            best[key] = o
            continue
        if (CONFIDENCE_RANK[o.confidence], -o.price_eur) > (
            CONFIDENCE_RANK[current.confidence],
            -current.price_eur,
        ):
            best[key] = o
    return list(best.values())
