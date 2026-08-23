"""The real-fare adapter.

These tests hit a live carrier API, so they are marked ``network`` and skip
themselves when it is unreachable rather than failing a suite that is otherwise
fully offline:

    python -m pytest -m "not network"     # skip them deliberately
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from flightarb.models import Cabin, Confidence
from flightarb.providers.base import ProviderContext, SearchQuery
from flightarb.providers.ryanair import RyanairProvider

pytestmark = pytest.mark.network

#: Far enough ahead that a schedule is published, near enough to be loaded.
SOON = date.today() + timedelta(days=30)


@pytest.fixture(scope="module")
def provider(policy, airports, places):
    ctx = ProviderContext(policy=policy, airports=airports, store=None, places=places)
    p = RyanairProvider(ctx)
    if not p.destinations_from("AGP"):
        pytest.skip("Ryanair API unreachable")
    return p


def test_route_map_knows_where_the_carrier_actually_flies(provider):
    """The premise of the whole project, asserted against live data:
    Ryanair serves Rabat and does not serve Casablanca."""
    assert "AGP" in provider.destinations_from("RBA")
    assert provider.destinations_from("CMN") == set()
    assert provider.serves("RBA", "AGP")
    assert not provider.serves("CMN", "AGP")


def test_returns_real_priced_offers(provider):
    query = SearchQuery("RBA", "AGP", SOON, None, seats=1, cabin=Cabin.ECONOMY, max_stops=0)
    offers = provider.search(query)
    if not offers:
        pytest.skip("no RBA-AGP service on the sampled date")
    offer = offers[0]
    assert offer.price_eur > 0
    assert offer.confidence == Confidence.VERIFIED
    assert offer.origin == "RBA" and offer.destination == "AGP"
    assert offer.booking_url


def test_elapsed_time_survives_the_timezone_change(provider):
    """Morocco is an hour behind Spain, so naive local-clock subtraction makes
    the westbound leg look like it takes one minute."""
    provider.destinations_from("AGP")  # populates the timezone map
    depart = datetime(SOON.year, SOON.month, SOON.day, 15, 40)
    arrive = datetime(SOON.year, SOON.month, SOON.day, 14, 45)  # earlier clock, later moment

    minutes = provider.flight_minutes("AGP", "RBA", depart, arrive)
    assert minutes > 30, "a westbound leg must not come out as a few minutes"
    assert minutes < 300


def test_a_month_of_prices_costs_one_request(provider):
    """Flexible dates must not mean one HTTP call per day."""
    days = provider.month("RBA", "AGP", SOON)
    if not days:
        pytest.skip("no published schedule for the sampled month")
    assert len(days) > 5
    before = provider.stats.queries
    for offset in range(0, 5):
        provider.search(
            SearchQuery("RBA", "AGP", SOON + timedelta(days=offset), None,
                        seats=1, cabin=Cabin.ECONOMY, max_stops=0)
        )
    # The month is cached, so those five date queries made no new HTTP calls.
    assert provider.stats.queries - before <= 5


def test_round_trips_are_bundled_as_one_ticket(provider):
    """Ryanair sells a return as a single booking, so it must not be charged
    the separate-ticket risk penalty."""
    query = SearchQuery("RBA", "AGP", SOON, SOON + timedelta(days=4),
                        seats=2, cabin=Cabin.ECONOMY, max_stops=0)
    offers = provider.search(query)
    if len(offers) != 2:
        pytest.skip("no round-trip pairing available on the sampled dates")
    assert {o.bundle_id for o in offers} == {"fr-rt"}
    total = offers[0].bundle_price_eur
    assert total == pytest.approx(offers[0].price_eur + offers[1].price_eur, abs=0.01)


def test_unserved_routes_cost_nothing(provider):
    """Asking for a route the carrier does not fly must not make a fare call."""
    assert provider.search(
        SearchQuery("CMN", "AGP", SOON, None, seats=1, cabin=Cabin.ECONOMY, max_stops=0)
    ) == []
