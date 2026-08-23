"""The cost engine: the part that must never quietly lie about a price."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from conftest import DEPART, RETURN, make_offer

from flightarb.engine.cost import CostEngine
from flightarb.models import (
    DirectionPlan,
    GroundLeg,
    GroundMode,
    Journey,
    Ticketing,
)


def _leg(minutes: float = 30, cost: float = 10.0) -> GroundLeg:
    return GroundLeg("a", "b", 25.0, minutes, cost, GroundMode.CAR, "estimate")


def _journey(out_offer, back_offer=None, ticketing=Ticketing.TWO_ONE_WAYS) -> Journey:
    outbound = DirectionPlan(_leg(), out_offer, _leg())
    inbound = DirectionPlan(_leg(), back_offer, _leg()) if back_offer else None
    return Journey(outbound, inbound, ticketing)


@pytest.fixture
def engine(policy, spec) -> CostEngine:
    return CostEngine(policy, spec)


def test_two_one_ways_fare_is_the_sum_times_seats(engine, spec):
    out = make_offer("CMN", "AGP", datetime.combine(DEPART, datetime.min.time()).replace(hour=9), 100.0)
    back = make_offer("AGP", "CMN", datetime.combine(RETURN, datetime.min.time()).replace(hour=9), 80.0)
    journey = _journey(out, back)
    # 4 fare-paying seats (2 adults + 2 children)
    assert engine.fare(journey) == pytest.approx((100.0 + 80.0) * 4)


def test_bundled_round_trip_is_priced_once_not_twice(engine):
    """A round-trip ticket costs its bundle price, not the sum of two halves."""
    dep = datetime.combine(DEPART, datetime.min.time()).replace(hour=9)
    ret = datetime.combine(RETURN, datetime.min.time()).replace(hour=9)
    out = make_offer("CMN", "AGP", dep, 90.0, bundle_id="b1", bundle_price=155.0)
    back = make_offer("AGP", "CMN", ret, 90.0, bundle_id="b1", bundle_price=155.0)
    journey = _journey(out, back, Ticketing.RETURN)
    assert engine.fare(journey) == pytest.approx(155.0 * 4)


def test_bags_are_charged_only_when_not_included(engine, policy):
    dep = datetime.combine(DEPART, datetime.min.time()).replace(hour=9)
    included = make_offer("CMN", "AGP", dep, 100.0, carrier="AT", checked=1)
    excluded = make_offer("CMN", "AGP", dep, 100.0, carrier="FR", checked=0)

    free, _ = engine.bags(_journey(included))
    paid, notes = engine.bags(_journey(excluded))
    assert free == 0.0
    assert paid == pytest.approx(policy.checked_bag_fee("FR"))
    assert any("checked bag" in n for n in notes)


def test_overnight_self_transfer_is_charged_a_hotel_and_a_penalty(engine, policy):
    """The headline case: a cheap fare that is not cheap."""
    dep = datetime.combine(DEPART, datetime.min.time()).replace(hour=14)
    offer = make_offer(
        "CMN", "AGP", dep, 60.0, stops_via="MAD", layover_min=13 * 60, self_transfer=True
    )
    journey = _journey(offer)
    cost = engine.evaluate(journey)

    assert offer.is_overnight_connection
    assert cost.hotel == pytest.approx(float(policy.get("economics.overnight_hotel_eur")))
    assert cost.risk_penalty >= float(policy.get("risk.self_transfer_penalty_eur"))
    assert cost.cash > offer.price_eur * 4, "hotel and ground must land in cash"
    assert any("overnight" in n for n in cost.notes)


def test_tight_connection_is_penalised(engine, policy):
    dep = datetime.combine(DEPART, datetime.min.time()).replace(hour=9)
    tight = make_offer("CMN", "AGP", dep, 100.0, stops_via="MAD", layover_min=35)
    penalty, notes = engine.risk(_journey(tight))
    assert penalty >= float(policy.get("risk.tight_connection_penalty_eur"))
    assert any("connection" in n for n in notes)


def test_time_cost_scales_sublinearly_with_party_size(policy, spec):
    engine = CostEngine(policy, spec)
    one_hour_for_four = engine.time_cost(60)
    # 2 adults + 2 children => 1 + 3*0.5 = 2.5x, not 4x
    assert one_hour_for_four == pytest.approx(policy.value_of_time * 2.5)


def test_violations_flag_policy_breaches(policy, spec):
    engine = CostEngine(policy.override({"flight.allow_self_transfer": False}), spec)
    dep = datetime.combine(DEPART, datetime.min.time()).replace(hour=9)
    offer = make_offer("CMN", "AGP", dep, 60.0, stops_via="MAD", self_transfer=True)
    problems = engine.violations(_journey(offer))
    assert any("self-transfer" in p for p in problems)


def test_return_before_outbound_is_rejected(engine):
    dep = datetime.combine(DEPART, datetime.min.time()).replace(hour=9)
    out = make_offer("CMN", "AGP", dep, 100.0)
    back = make_offer("AGP", "CMN", dep - timedelta(days=1), 100.0)
    problems = engine.violations(_journey(out, back))
    assert any("before the outbound" in p for p in problems)


def test_unverified_prices_carry_an_uncertainty_margin(policy, spec):
    from flightarb.models import Confidence

    engine = CostEngine(policy, spec)
    dep = datetime.combine(DEPART, datetime.min.time()).replace(hour=9)
    unverified = _journey(make_offer("CMN", "AGP", dep, 100.0, confidence=Confidence.DISCOVERY))
    verified = _journey(make_offer("CMN", "AGP", dep, 100.0, confidence=Confidence.VERIFIED))

    assert engine.evaluate(unverified).confidence_penalty > 0
    assert engine.evaluate(verified).confidence_penalty == 0


def test_cash_never_includes_notional_penalties(engine):
    """Cash is what leaves the bank account. Time and risk are not cash."""
    dep = datetime.combine(DEPART, datetime.min.time()).replace(hour=9)
    journey = _journey(make_offer("CMN", "AGP", dep, 100.0))
    c = engine.evaluate(journey)
    assert c.cash == pytest.approx(c.fare + c.bags + c.ground + c.hotel + c.fees)
    assert c.utility > c.cash
