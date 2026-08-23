"""The acceptance test, stated as concretely as the brief demanded.

    Input: Casablanca -> Malaga

    The engine MUST independently consider CMN and RBA; MUST consider at least
    one alternative destination airport; MUST compare the exact route, an
    alternative origin, an alternative destination, mixed outbound/return
    airports, flexible dates and separate one-way pricing; MUST cost fare,
    baggage, ground transport, door-to-door time and self-transfer risk; and
    MUST return CHEAPEST / BEST VALUE / EASIEST with the delta against
    CMN -> AGP explained.

Everything here runs against the deterministic market simulator, so a failure
means the *engine* changed, not that a fare moved.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from conftest import DEPART, RETURN

from flightarb.engine.planner import Planner
from flightarb.engine.search import run_search
from flightarb.models import Cabin, JourneySpec, Party, Ticketing


@pytest.fixture(scope="module")
def result(request):
    """One full search, reused by every assertion below."""
    from flightarb.policy import Policy
    from flightarb.runtime import Runtime
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    policy = Policy.load(root / "policy.toml")
    rt = Runtime.build(policy=policy, db_path=None, providers=["synthetic"])
    spec = JourneySpec(
        origin=rt.places.resolve("Casablanca"),
        destination=rt.places.resolve("Malaga"),
        depart_date=DEPART,
        return_date=RETURN,
        party=Party(adults=2, children=2, cabin_bags=1, checked_bags=1),
        cabin=Cabin.ECONOMY,
        date_flex_days=2,
    )
    out = run_search(rt, spec)
    request.addfinalizer(rt.close)
    return out


# --------------------------------------------------------------------------- #
# MUST independently consider alternative airports
# --------------------------------------------------------------------------- #


def test_considers_both_casablanca_airports(result):
    codes = {ep.iata for ep in result.origins}
    assert "CMN" in codes
    assert "RBA" in codes, "Rabat must be considered as an alternative origin"


def test_considers_an_alternative_destination_airport(result):
    codes = {ep.iata for ep in result.destinations}
    assert "AGP" in codes
    assert len(codes) >= 2, "at least one alternative arrival airport is required"


def test_baseline_is_the_obvious_route(result):
    assert result.baseline is not None
    assert result.baseline.outbound.offer.origin == "CMN"
    assert result.baseline.outbound.offer.destination == "AGP"
    assert result.baseline.outbound.offer.depart_date == DEPART


# --------------------------------------------------------------------------- #
# MUST actually explore the space
# --------------------------------------------------------------------------- #


def test_explores_alternative_origins_destinations_and_dates(result):
    probed = " ".join(step.config for step in result.trace)
    assert "RBA" in probed, "the planner never probed Rabat"
    assert any(
        step.config.count("d") and ("+" in step.config or "-" in step.config)
        for step in result.trace
    ), "the planner never shifted a date"
    assert "[ow]" in probed, "the planner never priced the legs as two one-ways"


def test_search_is_bounded_not_exhaustive(result, policy):
    """The point of a best-first planner is to NOT run the full cross product."""
    assert result.stats.queries_used <= policy.query_budget
    assert result.stats.configs_visited >= 3
    assert result.stats.stopped_because


def test_considers_far_more_journeys_than_it_queried(result):
    """Independent leg pricing is what makes the combinatorics cheap."""
    assert result.considered > result.stats.queries_used


# --------------------------------------------------------------------------- #
# MUST cost the whole door-to-door journey
# --------------------------------------------------------------------------- #


def test_every_recommendation_is_fully_costed(result):
    assert result.recommendations.distinct, "no recommendation produced"
    for _label, journey in result.recommendations.distinct:
        c = journey.cost
        assert c.fare > 0
        assert c.ground > 0, "ground transport must be priced"
        assert c.door_to_door_min > 0
        assert c.time_cost > 0
        assert c.cash == pytest.approx(c.fare + c.bags + c.ground + c.hotel + c.fees)


def test_bags_are_priced_for_a_family_carrying_one(result):
    """The party brings a checked bag; at least one option must charge for it."""
    charged = [
        j for _l, j in result.recommendations.distinct if j.cost.bags > 0
    ] + [r.journey for r in result.recommendations.rejected if r.journey.cost.bags > 0]
    assert charged, "nothing priced the checked bag the traveller is bringing"


def test_ground_transport_reflects_the_chosen_airport(result):
    """A Rabat departure must cost more to reach than a Casablanca one."""
    by_iata = {ep.iata: ep for ep in result.origins}
    assert by_iata["RBA"].minutes > by_iata["CMN"].minutes
    assert by_iata["RBA"].leg.cost_eur > by_iata["CMN"].leg.cost_eur


# --------------------------------------------------------------------------- #
# MUST return three named answers, explained against the baseline
# --------------------------------------------------------------------------- #


def test_returns_cheapest_best_value_and_easiest(result):
    rec = result.recommendations
    assert rec.cheapest is not None
    assert rec.best_value is not None
    assert rec.easiest is not None


def test_cheapest_is_actually_the_cheapest(result):
    rec = result.recommendations
    feasible = [j for j in rec.front]
    assert all(rec.cheapest.cost.cash <= j.cost.cash for j in feasible)


def test_best_value_wins_on_utility_or_is_the_baseline(result):
    """Best value either minimises total cost, or falls back to the obvious
    route because the alternative did not clear the traveller's threshold."""
    rec = result.recommendations
    is_baseline = result.baseline is not None and rec.best_value.key() == result.baseline.key()
    beats_cheapest_on_utility = rec.best_value.cost.utility <= rec.cheapest.cost.utility
    assert is_baseline or beats_cheapest_on_utility


def test_every_recommendation_explains_itself(result):
    for _label, journey in result.recommendations.distinct:
        headline = result.headline_for(journey)
        assert headline and len(headline) > 10
        if result.baseline and journey.key() != result.baseline.key():
            assert result.why_for(journey), "an alternative must say why it differs"


def test_rejections_are_reported_with_reasons(result):
    """Proof that alternatives were investigated, not merely never considered."""
    assert result.recommendations.rejected, "nothing was reported as investigated-and-rejected"
    for rejection in result.recommendations.rejected:
        assert rejection.reason and len(rejection.reason) > 15


def test_synthetic_prices_are_labelled_as_not_bookable(result):
    assert any("SYNTHETIC" in w or "synthetic" in w for w in result.warnings)


# --------------------------------------------------------------------------- #
# MUST support mixed airports and separate one-way pricing
# --------------------------------------------------------------------------- #


def test_can_build_mixed_airport_and_two_one_way_journeys(runtime):
    """Out of one airport, back into another, on two separate tickets."""
    spec = JourneySpec(
        origin=runtime.places.resolve("Casablanca"),
        destination=runtime.places.resolve("Malaga"),
        depart_date=DEPART,
        return_date=RETURN,
        party=Party(adults=1),
        date_flex_days=1,
    )
    planner = Planner(runtime, spec)
    journeys, _baseline = planner.run()

    assert any(j.ticketing == Ticketing.TWO_ONE_WAYS for j in journeys), (
        "the engine must be able to price the trip as two independent one-ways"
    )
    mixed = [
        j
        for j in journeys
        if j.inbound is not None and j.inbound.offer.destination != j.outbound.offer.origin
    ]
    assert mixed, "the engine must be able to fly out of one airport and back into another"


def test_one_way_searches_work(runtime):
    spec = JourneySpec(
        origin=runtime.places.resolve("Casablanca"),
        destination=runtime.places.resolve("Malaga"),
        depart_date=DEPART,
        return_date=None,
        party=Party(adults=1),
    )
    out = run_search(runtime, spec)
    assert out.recommendations.distinct
    for _label, journey in out.recommendations.distinct:
        assert journey.inbound is None
        assert journey.ticketing == Ticketing.ONE_WAY


# --------------------------------------------------------------------------- #
# MUST refuse nonsense before spending queries
# --------------------------------------------------------------------------- #


def test_validation_rejects_degenerate_requests(runtime):
    from datetime import date as _date

    from flightarb.engine.search import validate_spec

    malaga = runtime.places.resolve("Malaga")
    casablanca = runtime.places.resolve("Casablanca")

    same_place = JourneySpec(malaga, malaga, DEPART, party=Party(adults=1))
    assert any("same place" in p for p in validate_spec(same_place))

    past = JourneySpec(casablanca, malaga, _date(2020, 1, 1), party=Party(adults=1))
    assert any("past" in p for p in validate_spec(past))

    backwards = JourneySpec(
        casablanca, malaga, DEPART, DEPART - timedelta(days=2), party=Party(adults=1)
    )
    assert any("not after" in p for p in validate_spec(backwards))

    good = JourneySpec(casablanca, malaga, DEPART, RETURN, party=Party(adults=1))
    assert validate_spec(good) == []
