"""Traveller policy: the tunable economics behind every ranking decision.

Loaded from TOML (stdlib ``tomllib``) or JSON.  Access is by dotted path so
adding a knob to ``policy.toml`` never requires touching this file, while the
hot values used all over the cost engine get typed properties.
"""

from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass
from datetime import time
from pathlib import Path
from typing import Any

_MISSING = object()


DEFAULTS: dict[str, Any] = {
    "traveler": {
        "value_of_time_eur_hour": 25.0,
        "group_time_discount": 0.5,
        "home_country": None,
    },
    "airports": {
        "max_origin_reposition_minutes": 120,
        "max_destination_transfer_minutes": 120,
        "max_origin_candidates": 4,
        "max_destination_candidates": 4,
        "include_small_airports": False,
        "require_scheduled_service": True,
    },
    "flight": {
        "max_stops": 1,
        "allow_self_transfer": True,
        "allow_separate_tickets": True,
        "allow_overnight": "conditional",
        "allow_airport_change_in_transit": False,
        "min_connection_min": 60,
        "min_self_transfer_min": 150,
        "min_self_transfer_intl_min": 180,
        "max_self_transfer_gap_min": 600,
        "max_overnight_gap_min": 1080,
        "earliest_departure": "05:00",
        "latest_arrival": "23:59",
        "allow_hidden_city": False,
    },
    "economics": {
        "min_saving_per_extra_hour": 40.0,
        "min_saving_for_self_transfer": 100.0,
        "min_saving_for_separate_tickets": 40.0,
        "min_saving_for_airport_change": 60.0,
        "min_saving_to_recommend": 25.0,
        "overnight_hotel_eur": 90.0,
    },
    "bags": {"cabin": 1, "checked": 0},
    "bag_fees": {
        "default_checked_eur": 45.0,
        "default_cabin_eur": 25.0,
        "by_carrier": {},
    },
    "ground": {
        "router": "estimate",
        "osrm_url": "https://router.project-osrm.org",
        "osrm_public_demo_optin": False,
        "mode": "auto",
        "cost_per_km_eur": 0.22,
        "fixed_cost_eur": 3.0,
        "transit_base_eur": 1.5,
        "transit_cost_per_km_eur": 0.06,
        "transit_time_factor": 1.25,
        "transit_wait_min": 15,
        "transit_max_km": 400,
        "checkin_buffer_domestic_min": 75,
        "checkin_buffer_intl_min": 120,
        "arrival_buffer_min": 25,
        "arrival_buffer_checked_bag_min": 25,
    },
    "risk": {
        "self_transfer_penalty_eur": 35.0,
        "airport_change_penalty_eur": 50.0,
        "overnight_penalty_eur": 80.0,
        "tight_connection_penalty_eur": 100.0,
        "separate_ticket_penalty_eur": 30.0,
        "redeye_penalty_eur": 20.0,
        "unverified_penalty_pct": 0.03,
        "synthetic_penalty_pct": 0.0,
    },
    "search": {
        "budget_queries": 80,
        "max_wall_seconds": 120,
        "parallelism": 6,
        "finalists_to_verify": 5,
        "prune_threshold_pct": 0.30,
        "min_improvement_eur": 5.0,
        "patience": 12,
        "leg_coverage": True,
        "synthesize_self_transfer": True,
        "self_transfer_hubs": 3,
    },
    "providers": {
        "enabled": ["synthetic"],
        "requests_per_minute": 20,
        "timeout_seconds": 25,
        "max_retries": 2,
        "cache_ttl_minutes": 180,
        "airline_direct": {"enabled": False, "carriers": []},
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _parse_hhmm(value: str) -> time:
    hh, _, mm = value.partition(":")
    return time(int(hh), int(mm or 0))


@dataclass
class Policy:
    """Dotted-path view over the merged policy document."""

    data: dict[str, Any]
    source: str = "<defaults>"

    # -- loading ---------------------------------------------------------- #
    @classmethod
    def default(cls) -> "Policy":
        return cls(json.loads(json.dumps(DEFAULTS)))

    @classmethod
    def load(cls, path: str | Path | None) -> "Policy":
        if path is None:
            return cls.default()
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"policy file not found: {p}")
        raw = p.read_bytes()
        if p.suffix.lower() == ".json":
            doc = json.loads(raw.decode("utf-8"))
        else:
            doc = tomllib.loads(raw.decode("utf-8"))
        return cls(_deep_merge(DEFAULTS, doc), str(p))

    def override(self, updates: dict[str, Any]) -> "Policy":
        """Return a copy with dotted-path overrides applied (CLI flags)."""
        doc = json.loads(json.dumps(self.data))
        for dotted, value in updates.items():
            if value is None:
                continue
            node = doc
            *parents, leaf = dotted.split(".")
            for part in parents:
                node = node.setdefault(part, {})
            node[leaf] = value
        return Policy(doc, self.source)

    # -- access ----------------------------------------------------------- #
    def get(self, dotted: str, default: Any = _MISSING) -> Any:
        node: Any = self.data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                if default is _MISSING:
                    raise KeyError(f"policy key not found: {dotted}")
                return default
            node = node[part]
        return node

    def section(self, name: str) -> dict[str, Any]:
        return dict(self.get(name, {}))

    # -- hot values used across the cost engine --------------------------- #
    @property
    def value_of_time(self) -> float:
        return float(self.get("traveler.value_of_time_eur_hour"))

    @property
    def group_time_discount(self) -> float:
        return float(self.get("traveler.group_time_discount"))

    @property
    def max_origin_minutes(self) -> float:
        return float(self.get("airports.max_origin_reposition_minutes"))

    @property
    def max_destination_minutes(self) -> float:
        return float(self.get("airports.max_destination_transfer_minutes"))

    @property
    def max_stops(self) -> int:
        return int(self.get("flight.max_stops"))

    @property
    def earliest_departure(self) -> time:
        return _parse_hhmm(str(self.get("flight.earliest_departure")))

    @property
    def latest_arrival(self) -> time:
        return _parse_hhmm(str(self.get("flight.latest_arrival")))

    @property
    def query_budget(self) -> int:
        return int(self.get("search.budget_queries"))

    def time_multiplier(self, people: int) -> float:
        """A party of N does not cost N times the value of time."""
        return 1.0 + max(0, people - 1) * self.group_time_discount

    def checked_bag_fee(self, carrier: str) -> float:
        by_carrier = self.get("bag_fees.by_carrier", {})
        if carrier in by_carrier:
            return float(by_carrier[carrier])
        return float(self.get("bag_fees.default_checked_eur"))

    def cabin_bag_fee(self, carrier: str) -> float:
        return float(self.get("bag_fees.default_cabin_eur"))
