"""Geography: airports, places, and the ground router."""

from __future__ import annotations

import pytest

from flightarb.geo.ground import EstimateBackend, GroundRouter
from flightarb.models import GroundMode


def test_airport_index_loads_real_airports(airports):
    assert len(airports) > 3000
    cmn = airports.get("CMN")
    assert cmn is not None and cmn.country == "MA"
    assert airports.get("agp").iata == "AGP"  # case-insensitive


def test_near_finds_both_casablanca_airports(airports, places):
    casablanca = places.resolve("Casablanca")
    codes = {a.iata for a, _km in airports.near(casablanca.lat, casablanca.lon, radius_km=200)}
    assert {"CMN", "RBA"} <= codes, "Rabat must be discoverable from Casablanca"


def test_place_resolution_handles_accents_and_codes(places):
    assert places.resolve("Málaga").country == "ES"
    assert places.resolve("Malaga").country == "ES"
    assert places.resolve("malaga, es").country == "ES"
    coord = places.resolve("33.57,-7.59")
    assert coord.lat == pytest.approx(33.57)


def test_unresolvable_place_raises(places):
    with pytest.raises(LookupError):
        places.resolve("Qwertyuiop Nowhereville")


def test_metro_population_is_coordinate_based(places, airports):
    """SVQ's municipality is 'Seville'; the gazetteer says 'Sevilla'."""
    svq = airports.get("SVQ")
    assert places.metro_population(svq.lat, svq.lon) > 400_000


def test_estimator_is_monotonic_and_plausible():
    backend = EstimateBackend()
    short = backend.route(33.58, -7.61, 33.37, -7.59)   # Casablanca -> CMN
    long = backend.route(33.58, -7.61, 34.05, -6.75)    # Casablanca -> RBA
    assert short.minutes < long.minutes
    assert 20 < short.minutes < 60
    assert 60 < long.minutes < 140


def test_ground_mode_depends_on_party_size(policy, places, airports):
    """A car costs the same for one traveller or four; a train ticket does not."""
    casablanca = places.resolve("Casablanca")
    rba = airports.get("RBA")

    solo = GroundRouter(policy.override({"traveler.value_of_time_eur_hour": 6.0}))
    family = GroundRouter(policy.override({"traveler.value_of_time_eur_hour": 25.0}))

    solo_leg = solo.leg("home", casablanca.lat, casablanca.lon, "RBA", rba.lat, rba.lon, people=1)
    family_leg = family.leg("home", casablanca.lat, casablanca.lon, "RBA", rba.lat, rba.lon, people=4)

    assert solo_leg.mode == GroundMode.TRANSIT
    assert family_leg.mode == GroundMode.CAR
    assert solo_leg.cost_eur < family_leg.cost_eur


def test_mode_choice_never_breaks_the_time_limit(policy, places, airports):
    """Choosing a cheaper-but-slower mode must not push a leg past the limit."""
    casablanca = places.resolve("Casablanca")
    rba = airports.get("RBA")
    router = GroundRouter(policy.override({"traveler.value_of_time_eur_hour": 6.0}))

    unconstrained = router.leg("h", casablanca.lat, casablanca.lon, "RBA", rba.lat, rba.lon, people=1)
    constrained = router.leg(
        "h", casablanca.lat, casablanca.lon, "RBA", rba.lat, rba.lon, people=1, max_minutes=120
    )
    assert unconstrained.minutes > 120        # the train is too slow
    assert constrained.minutes <= 120         # so driving is chosen instead
    assert constrained.mode == GroundMode.CAR


def test_public_osrm_demo_requires_opt_in(policy):
    """We must not hammer someone else's free server by default."""
    router = GroundRouter(policy.override({"ground.router": "osrm"}))
    assert router.backend.name == "estimate"

    opted_in = GroundRouter(
        policy.override({"ground.router": "osrm", "ground.osrm_public_demo_optin": True})
    )
    assert opted_in.backend.name == "osrm"
