"""Acquisition layer: determinism, politeness, resilience, and the price memory."""

from __future__ import annotations

import time
from datetime import date, timedelta

import pytest

from conftest import DEPART, make_offer

from flightarb.models import Cabin, Confidence
from flightarb.providers.base import (
    CircuitBreaker,
    FlightProvider,
    ProviderContext,
    ProviderUnavailable,
    RateLimiter,
    SearchQuery,
)
from flightarb.providers.registry import ProviderRegistry, _dedupe
from flightarb.providers.synthetic import SyntheticProvider
from flightarb.store import Store


@pytest.fixture
def ctx(policy, airports, places) -> ProviderContext:
    return ProviderContext(policy=policy, airports=airports, store=None, places=places)


def _query(origin="CMN", destination="AGP", ret=None) -> SearchQuery:
    return SearchQuery(origin, destination, DEPART, ret, seats=1, cabin=Cabin.ECONOMY, max_stops=1)


# --------------------------------------------------------------------------- #
# Synthetic market
# --------------------------------------------------------------------------- #


def test_synthetic_market_is_deterministic(ctx):
    a = SyntheticProvider(ctx).search(_query())
    b = SyntheticProvider(ctx).search(_query())
    assert [o.price_eur for o in a] == [o.price_eur for o in b]
    assert [o.route_label for o in a] == [o.route_label for o in b]


def test_synthetic_prices_are_never_presented_as_real(ctx):
    provider = SyntheticProvider(ctx)
    assert provider.real_prices is False
    assert all(o.confidence == Confidence.SYNTHETIC for o in provider.search(_query()))


def test_secondary_airports_are_cheaper_than_flag_hubs(ctx):
    """The structural fact the whole engine is built to exploit."""
    provider = SyntheticProvider(ctx)
    hub = min(o.price_eur for o in provider.search(_query("CMN", "AGP")))
    secondary = min(o.price_eur for o in provider.search(_query("RBA", "AGP")))
    assert secondary < hub


def test_route_fleet_is_stable_across_dates(ctx):
    """An airline that flies you out is there to fly you back."""
    provider = SyntheticProvider(ctx)
    out = {c for o in provider.search(_query("CMN", "AGP")) for c in o.carriers}
    back_query = SearchQuery(
        "AGP", "CMN", DEPART + timedelta(days=4), None, seats=1, cabin=Cabin.ECONOMY, max_stops=1
    )
    back = {c for o in provider.search(back_query) for c in o.carriers}
    assert out & back, "no carrier operates the route in both directions"


def test_round_trip_bundles_are_single_carrier(ctx):
    """Only one airline can sell you one round-trip ticket."""
    provider = SyntheticProvider(ctx)
    offers = provider.search(_query(ret=DEPART + timedelta(days=4)))
    bundles: dict[str, list] = {}
    for o in offers:
        if o.bundle_id:
            bundles.setdefault(o.bundle_id, []).append(o)
    assert bundles
    for legs in bundles.values():
        assert len({c for leg in legs for c in leg.carriers}) == 1


def test_unknown_airport_yields_no_offers(ctx):
    assert SyntheticProvider(ctx).search(_query("ZZZ", "AGP")) == []


# --------------------------------------------------------------------------- #
# Politeness and resilience
# --------------------------------------------------------------------------- #


def test_rate_limiter_throttles():
    limiter = RateLimiter(per_minute=60)  # 1/sec
    limiter.tokens = 1.0
    started = time.monotonic()
    limiter.acquire()  # free
    limiter.acquire()  # must wait ~1s
    assert time.monotonic() - started >= 0.5


def test_local_providers_are_not_rate_limited(ctx):
    assert SyntheticProvider(ctx).needs_rate_limit is False


def test_circuit_breaker_opens_then_recovers():
    breaker = CircuitBreaker(threshold=2, cooldown_sec=0.2)
    breaker.record_failure()
    assert not breaker.is_open
    breaker.record_failure()
    assert breaker.is_open
    time.sleep(0.25)
    assert not breaker.is_open


def test_a_failing_provider_does_not_sink_the_search(ctx):
    class Exploding(FlightProvider):
        name = "exploding"
        needs_rate_limit = False

        def _search(self, query):
            raise ProviderUnavailable("nope")

    provider = Exploding(ctx)
    assert provider.search(_query()) == []      # swallowed
    assert provider.stats.failures == 1


def test_registry_reports_unavailable_providers(ctx):
    registry = ProviderRegistry(ctx, ["synthetic", "no-such-provider"])
    assert "synthetic" in registry.names
    assert "no-such-provider" in registry.skipped


def test_browser_provider_is_a_fallback_not_a_peer(ctx):
    registry = ProviderRegistry(ctx, ["synthetic", "browser"])
    assert all(p.name != "browser" for p in registry.primary)


def test_dedupe_prefers_the_more_trustworthy_price():
    from datetime import datetime

    dep = datetime.combine(DEPART, datetime.min.time()).replace(hour=9)
    discovery = make_offer("CMN", "AGP", dep, 100.0, confidence=Confidence.DISCOVERY)
    verified = make_offer("CMN", "AGP", dep, 100.0, confidence=Confidence.VERIFIED)
    kept = _dedupe([discovery, verified])
    assert len(kept) == 1
    assert kept[0].confidence == Confidence.VERIFIED


# --------------------------------------------------------------------------- #
# Store and price memory
# --------------------------------------------------------------------------- #


def test_offer_cache_round_trips(tmp_path, ctx):
    from datetime import datetime

    store = Store(tmp_path / "t.sqlite3")
    dep = datetime.combine(DEPART, datetime.min.time()).replace(hour=9)
    offers = [make_offer("CMN", "AGP", dep, 123.45, checked=1)]
    store.put_offers("k", "test", offers)

    restored = store.get_offers("k", ttl_minutes=60)
    assert restored is not None and len(restored) == 1
    assert restored[0].price_eur == pytest.approx(123.45)
    assert restored[0].included_checked_bags == 1
    assert store.get_offers("k", ttl_minutes=0) is None  # expired
    store.close()


def test_synthetic_prices_never_pollute_the_price_memory(tmp_path):
    from datetime import datetime

    store = Store(tmp_path / "t.sqlite3")
    dep = datetime.combine(DEPART, datetime.min.time()).replace(hour=9)
    store.record([make_offer("CMN", "AGP", dep, 100.0, confidence=Confidence.SYNTHETIC)])
    assert store.counts()["observations"] == 0

    store.record([make_offer("CMN", "AGP", dep, 100.0, confidence=Confidence.DISCOVERY)])
    assert store.counts()["observations"] == 1
    store.close()


def test_price_stats_describe_the_observed_distribution(tmp_path):
    from datetime import datetime

    store = Store(tmp_path / "t.sqlite3")
    dep = datetime.combine(DEPART, datetime.min.time()).replace(hour=9)
    store.record(
        [make_offer("CMN", "AGP", dep, price, confidence=Confidence.DISCOVERY)
         for price in (100, 120, 140, 160, 180, 200)]
    )
    days_out = (DEPART - date.today()).days
    stats = store.stats("CMN", "AGP", days_out)
    assert stats.samples == 6
    assert stats.minimum == 100 and stats.maximum == 200
    assert stats.median == pytest.approx(150)
    assert stats.percentile_of(100) == 0.0
    assert stats.percentile_of(200) == 100.0
    store.close()


def test_ground_cache_persists(tmp_path):
    store = Store(tmp_path / "t.sqlite3")
    assert store.get_ground(1.0, 2.0, 3.0, 4.0, "estimate") is None
    store.put_ground(1.0, 2.0, 3.0, 4.0, "estimate", 10.0, 20.0, "estimate")
    assert store.get_ground(1.0, 2.0, 3.0, 4.0, "estimate") == (10.0, 20.0, "estimate")
    store.close()
