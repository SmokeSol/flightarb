"""Ground routing: place <-> airport, in minutes and euros.

Why this matters more than it looks: an engine that shortlists "airports within
100 km" is wrong.  100 km of Moroccan motorway is 55 minutes; 100 km of coastal
road is two hours.  Every reachability decision here is made in *minutes*.

Two backends:

* ``estimate`` -- offline. Great-circle distance x a detour factor, driven
  through a distance-dependent speed profile. No network, deterministic,
  good to roughly +/-15% on the routes that matter.
* ``osrm``     -- real road routing against an OSRM server (self-hosted, or the
  public demo server if you explicitly opt in).

Results are cached in SQLite forever -- roads do not move.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Protocol

from ..models import GroundLeg, GroundMode
from ..policy import Policy
from .airports import haversine_km


@dataclass(frozen=True)
class RouteResult:
    km: float
    minutes: float
    source: str


class RoutingBackend(Protocol):
    name: str

    def route(self, lat1: float, lon1: float, lat2: float, lon2: float) -> RouteResult: ...


# --------------------------------------------------------------------------- #
# Offline estimator
# --------------------------------------------------------------------------- #


class EstimateBackend:
    """Distance-dependent detour + speed model. No network required.

    Calibrated against OSRM on a spread of real city->airport routes
    (Casablanca-CMN/RBA/RAK, Malaga-AGP/GRX/SVQ, Paris-BVA).  It models *pure
    road time*, exactly like OSRM, so the two backends are interchangeable --
    parking and terminal walking belong to the check-in buffer, not here.

    Residual error on the calibration set is roughly +/-15% with no systematic
    bias.  That is the honest ceiling for a global offline model: detour ratios
    genuinely range from 1.06 (Casablanca-Rabat motorway) to 1.43 (Malaga-
    Granada through the mountains).  Point ``router = "osrm"`` at a container
    when you want the real number.
    """

    name = "estimate"

    # (max straight-line km, detour factor, average km/h)
    PROFILE: tuple[tuple[float, float, float], ...] = (
        (5.0, 1.70, 25.0),    # inner city
        (15.0, 1.70, 36.0),   # city + ring road
        (40.0, 1.28, 58.0),   # metro area to its own airport
        (120.0, 1.15, 68.0),  # short intercity, mixed motorway
        (400.0, 1.25, 80.0),  # motorway haul
        (1e9, 1.22, 88.0),
    )

    def route(self, lat1: float, lon1: float, lat2: float, lon2: float) -> RouteResult:
        straight = haversine_km(lat1, lon1, lat2, lon2)
        detour, speed = next((d, s) for lim, d, s in self.PROFILE if straight <= lim)
        km = straight * detour
        return RouteResult(km=km, minutes=(km / speed) * 60.0, source="estimate")


# --------------------------------------------------------------------------- #
# OSRM
# --------------------------------------------------------------------------- #


class OSRMBackend:
    """Real road routing. Point ``base_url`` at your own container for
    unlimited use; the public demo server is opt-in and lightly used."""

    name = "osrm"

    def __init__(self, base_url: str, profile: str = "driving", timeout: int = 20):
        self.base_url = base_url.rstrip("/")
        self.profile = profile
        self.timeout = timeout
        self._fallback = EstimateBackend()

    def route(self, lat1: float, lon1: float, lat2: float, lon2: float) -> RouteResult:
        url = (
            f"{self.base_url}/route/v1/{self.profile}/"
            f"{lon1:.6f},{lat1:.6f};{lon2:.6f},{lat2:.6f}?overview=false&alternatives=false"
        )
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "flightarb/0.1"})
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                doc = json.loads(resp.read().decode("utf-8"))
            if doc.get("code") != "Ok" or not doc.get("routes"):
                raise ValueError(doc.get("code", "no route"))
            r = doc["routes"][0]
            return RouteResult(km=r["distance"] / 1000.0, minutes=r["duration"] / 60.0, source="osrm")
        except (urllib.error.URLError, TimeoutError, ValueError, KeyError, json.JSONDecodeError):
            # Never let a routing hiccup kill a search.
            res = self._fallback.route(lat1, lon1, lat2, lon2)
            return RouteResult(res.km, res.minutes, "estimate-fallback")


# --------------------------------------------------------------------------- #
# Router facade
# --------------------------------------------------------------------------- #


class GroundRouter:
    """Policy-aware, cached facade over a routing backend."""

    def __init__(self, policy: Policy, store=None):
        self.policy = policy
        self.store = store
        self.backend: RoutingBackend = self._make_backend(policy)
        self._mem: dict[tuple, RouteResult] = {}

    @staticmethod
    def _make_backend(policy: Policy) -> RoutingBackend:
        kind = str(policy.get("ground.router", "estimate")).lower()
        if kind != "osrm":
            return EstimateBackend()
        url = str(policy.get("ground.osrm_url"))
        is_demo = "router.project-osrm.org" in url
        if is_demo and not bool(policy.get("ground.osrm_public_demo_optin", False)):
            # Refuse to hammer someone else's free server by accident.
            return EstimateBackend()
        return OSRMBackend(url, timeout=int(policy.get("providers.timeout_seconds", 20)))

    # -- core ------------------------------------------------------------- #
    def route(self, lat1: float, lon1: float, lat2: float, lon2: float) -> RouteResult:
        key = (round(lat1, 4), round(lon1, 4), round(lat2, 4), round(lon2, 4), self.backend.name)
        if key in self._mem:
            return self._mem[key]
        if self.store is not None:
            cached = self.store.get_ground(*key)
            if cached is not None:
                res = RouteResult(cached[0], cached[1], cached[2])
                self._mem[key] = res
                return res
        res = self.backend.route(lat1, lon1, lat2, lon2)
        self._mem[key] = res
        if self.store is not None and not res.source.endswith("fallback"):
            self.store.put_ground(*key, res.km, res.minutes, res.source)
        return res

    def leg(
        self,
        from_label: str,
        lat1: float,
        lon1: float,
        to_label: str,
        lat2: float,
        lon2: float,
        mode: GroundMode | None = None,
        people: int = 1,
        max_minutes: float | None = None,
    ) -> GroundLeg:
        res = self.route(lat1, lon1, lat2, lon2)
        if res.km < 0.5:
            return GroundLeg.none(from_label)

        options = self._options(res, people)
        configured = str(self.policy.get("ground.mode", "auto")).lower()
        if mode is not None:
            chosen = options.get(mode, options[GroundMode.CAR])
        elif configured == "auto":
            # Pick per party: a car costs the same for one traveller or four,
            # a train ticket does not. This is the difference between telling a
            # backpacker and a family of four to use the same airport.
            #
            # But the traveller's travel-time limit is a limit on ACTUAL time.
            # Choosing a cheaper-but-slower mode must never push a journey past
            # it -- that once made Rabat disappear from a Casablanca search
            # because the train takes 135 min and the stated ceiling was 120,
            # even though driving there takes 96.
            affordable = options.values()
            if max_minutes is not None:
                within = [o for o in options.values() if o[1] <= max_minutes]
                affordable = within or [min(options.values(), key=lambda o: o[1])]
            chosen = min(affordable, key=lambda o: self._utility(o, people))
        else:
            chosen = options.get(GroundMode(configured), options[GroundMode.CAR])

        km, minutes, cost, picked = chosen
        return GroundLeg(
            from_label=from_label,
            to_label=to_label,
            km=km,
            minutes=minutes,
            cost_eur=cost,
            mode=picked,
            source=res.source,
        )

    def _options(self, res: RouteResult, people: int) -> dict[GroundMode, tuple]:
        p = self.policy
        car_cost = float(p.get("ground.fixed_cost_eur")) + res.km * float(
            p.get("ground.cost_per_km_eur")
        )
        options = {GroundMode.CAR: (res.km, res.minutes, car_cost, GroundMode.CAR)}

        if res.km <= float(p.get("ground.transit_max_km", 400)):
            transit_cost = (
                float(p.get("ground.transit_base_eur"))
                + res.km * float(p.get("ground.transit_cost_per_km_eur"))
            ) * max(1, people)
            transit_min = res.minutes * float(p.get("ground.transit_time_factor")) + float(
                p.get("ground.transit_wait_min")
            )
            options[GroundMode.TRANSIT] = (
                res.km,
                transit_min,
                transit_cost,
                GroundMode.TRANSIT,
            )
        return options

    def _utility(self, option: tuple, people: int) -> float:
        _km, minutes, cost, _mode = option
        return cost + (minutes / 60.0) * self.policy.value_of_time * self.policy.time_multiplier(
            people
        )
