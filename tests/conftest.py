"""Shared fixtures.

The datasets are downloaded once per session and reused; the engine is then
driven entirely by the synthetic provider, so the whole suite is deterministic
and needs no network after the first run.
"""

from __future__ import annotations

import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from flightarb.geo.airports import AirportIndex  # noqa: E402
from flightarb.geo.places import PlaceResolver  # noqa: E402
from flightarb.models import (  # noqa: E402
    Cabin,
    Confidence,
    FlightOffer,
    JourneySpec,
    Party,
    Segment,
)
from flightarb.policy import Policy  # noqa: E402
from flightarb.runtime import Runtime  # noqa: E402

#: Far enough out that the days-to-departure curve is flat and stable.
DEPART = date.today() + timedelta(days=90)
RETURN = DEPART + timedelta(days=4)


@pytest.fixture(scope="session")
def airports() -> AirportIndex:
    return AirportIndex.load()


@pytest.fixture(scope="session")
def places(airports) -> PlaceResolver:
    return PlaceResolver.load(airports=airports)


@pytest.fixture(scope="session")
def policy() -> Policy:
    path = ROOT / "policy.toml"
    return Policy.load(path if path.exists() else None)


@pytest.fixture
def runtime(policy) -> Runtime:
    rt = Runtime.build(policy=policy, db_path=None, providers=["synthetic"])
    yield rt
    rt.close()


@pytest.fixture
def spec(runtime) -> JourneySpec:
    return JourneySpec(
        origin=runtime.places.resolve("Casablanca"),
        destination=runtime.places.resolve("Malaga"),
        depart_date=DEPART,
        return_date=RETURN,
        party=Party(adults=2, children=2, cabin_bags=1, checked_bags=1),
        cabin=Cabin.ECONOMY,
    )


def make_offer(
    origin: str,
    destination: str,
    depart: datetime,
    price: float,
    *,
    carrier: str = "XX",
    duration_min: int = 90,
    stops_via: str | None = None,
    layover_min: int = 240,
    self_transfer: bool = False,
    checked: int = 0,
    confidence: Confidence = Confidence.DISCOVERY,
    bundle_id: str | None = None,
    bundle_price: float | None = None,
) -> FlightOffer:
    """Hand-built offer, so cost tests never depend on the market simulator."""
    if stops_via is None:
        segments = (
            Segment(carrier, f"{carrier}1", origin, destination, depart,
                    depart + timedelta(minutes=duration_min), duration_min),
        )
    else:
        first_arr = depart + timedelta(minutes=duration_min)
        second_dep = first_arr + timedelta(minutes=layover_min)
        segments = (
            Segment(carrier, f"{carrier}1", origin, stops_via, depart, first_arr, duration_min),
            Segment(carrier, f"{carrier}2", stops_via, destination, second_dep,
                    second_dep + timedelta(minutes=duration_min), duration_min),
        )
    return FlightOffer(
        segments=segments,
        price_eur=price,
        provider="test",
        bundle_id=bundle_id,
        bundle_price_eur=bundle_price,
        included_checked_bags=checked,
        self_transfer=self_transfer,
        confidence=confidence,
    )
