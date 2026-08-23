"""Flight acquisition interface.

The engine never talks to a website; it talks to a ``FlightProvider``.  That
boundary is what lets the same ranking logic run against a simulated market, a
scraped aggregator, or a direct airline check without knowing the difference.

Every adapter gets, for free:

* a token-bucket rate limiter (be a polite client),
* a circuit breaker (one blocked source must not sink the whole search),
* a TTL cache in SQLite (never ask the same question twice in an hour).
"""

from __future__ import annotations

import hashlib
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date
from typing import TYPE_CHECKING, Sequence

from ..models import Cabin, FlightOffer

if TYPE_CHECKING:  # pragma: no cover
    from ..geo.airports import AirportIndex
    from ..geo.places import PlaceResolver
    from ..policy import Policy
    from ..store import Store


class ProviderUnavailable(RuntimeError):
    """Adapter cannot run: missing dependency, blocked, or disabled."""


@dataclass(frozen=True, slots=True)
class SearchQuery:
    origin: str  # IATA
    destination: str
    depart_date: date
    return_date: date | None = None  # None => one-way pricing
    seats: int = 1
    cabin: Cabin = Cabin.ECONOMY
    max_stops: int = 1

    @property
    def is_round_trip(self) -> bool:
        return self.return_date is not None

    def key(self, provider: str = "") -> str:
        parts = [
            provider,
            self.origin,
            self.destination,
            self.depart_date.isoformat(),
            self.return_date.isoformat() if self.return_date else "-",
            str(self.seats),
            self.cabin.value,
            str(self.max_stops),
        ]
        return hashlib.sha1("|".join(parts).encode()).hexdigest()[:16]

    def label(self) -> str:
        tail = f" / {self.return_date}" if self.return_date else ""
        return f"{self.origin}->{self.destination} {self.depart_date}{tail}"


@dataclass
class ProviderContext:
    """Shared services an adapter may need."""

    policy: "Policy"
    airports: "AirportIndex"
    store: "Store | None" = None
    places: "PlaceResolver | None" = None


# --------------------------------------------------------------------------- #
# Politeness / resilience
# --------------------------------------------------------------------------- #


class RateLimiter:
    """Token bucket. Thread-safe, because the planner fans out."""

    def __init__(self, per_minute: int):
        self.capacity = max(1, per_minute)
        self.tokens = float(self.capacity)
        self.refill_per_sec = self.capacity / 60.0
        self.updated = time.monotonic()
        self.lock = threading.Lock()

    def acquire(self) -> None:
        while True:
            with self.lock:
                now = time.monotonic()
                self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.refill_per_sec)
                self.updated = now
                if self.tokens >= 1.0:
                    self.tokens -= 1.0
                    return
                wait = (1.0 - self.tokens) / self.refill_per_sec
            time.sleep(min(wait, 5.0))


class CircuitBreaker:
    """Trip after N consecutive failures; retry after a cooldown."""

    def __init__(self, threshold: int = 3, cooldown_sec: float = 120.0):
        self.threshold = threshold
        self.cooldown = cooldown_sec
        self.failures = 0
        self.opened_at: float | None = None
        self.lock = threading.Lock()

    @property
    def is_open(self) -> bool:
        with self.lock:
            if self.opened_at is None:
                return False
            if time.monotonic() - self.opened_at > self.cooldown:
                self.opened_at = None
                self.failures = 0
                return False
            return True

    def record_success(self) -> None:
        with self.lock:
            self.failures = 0
            self.opened_at = None

    def record_failure(self) -> None:
        with self.lock:
            self.failures += 1
            if self.failures >= self.threshold:
                self.opened_at = time.monotonic()


# --------------------------------------------------------------------------- #
# The interface
# --------------------------------------------------------------------------- #


@dataclass
class ProviderStats:
    queries: int = 0
    offers: int = 0
    cache_hits: int = 0
    failures: int = 0
    seconds: float = 0.0


class FlightProvider(ABC):
    name: str = "abstract"
    #: True when this source returns real, bookable prices.
    real_prices: bool = True
    #: True when the adapter can price a round-trip as one ticket.
    supports_round_trip: bool = True
    #: True when this adapter is suitable for verifying a finalist at the source.
    is_verifier: bool = False
    #: Rate limiting exists to be polite to somebody else's server. A provider
    #: that computes locally has nobody to be polite to.
    needs_rate_limit: bool = True

    def __init__(self, ctx: ProviderContext):
        self.ctx = ctx
        self.policy = ctx.policy
        self.stats = ProviderStats()
        self.limiter = RateLimiter(int(ctx.policy.get("providers.requests_per_minute", 20)))
        self.breaker = CircuitBreaker()

    # -- lifecycle -------------------------------------------------------- #
    def available(self) -> bool:
        """Cheap check: is this adapter usable right now?"""
        return not self.breaker.is_open

    def unavailable_reason(self) -> str | None:
        if self.breaker.is_open:
            return "circuit breaker open after repeated failures"
        return None

    # -- work ------------------------------------------------------------- #
    @abstractmethod
    def _search(self, query: SearchQuery) -> list[FlightOffer]:
        """Adapter implementation. Raise ProviderUnavailable to disable."""

    def search(self, query: SearchQuery) -> list[FlightOffer]:
        """Cached, rate-limited, breaker-guarded search."""
        if self.breaker.is_open:
            return []

        store = self.ctx.store
        ttl = int(self.policy.get("providers.cache_ttl_minutes", 180))
        cache_key = query.key(self.name)
        if store is not None and ttl > 0:
            cached = store.get_offers(cache_key, ttl)
            if cached is not None:
                self.stats.cache_hits += 1
                return cached

        if self.needs_rate_limit:
            self.limiter.acquire()
        started = time.monotonic()
        try:
            offers = self._search(query)
        except ProviderUnavailable:
            self.breaker.record_failure()
            self.stats.failures += 1
            return []
        except Exception:
            self.breaker.record_failure()
            self.stats.failures += 1
            return []
        finally:
            self.stats.seconds += time.monotonic() - started

        self.breaker.record_success()
        self.stats.queries += 1
        self.stats.offers += len(offers)
        if store is not None and ttl > 0:
            store.put_offers(cache_key, self.name, offers)
        return offers

    def verify(self, offer: FlightOffer) -> FlightOffer | None:
        """Re-price a specific offer at the source. ``None`` = cannot verify."""
        return None
