"""Assembled runtime: everything a search needs, built once."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .geo import datasets
from .geo.airports import AirportIndex
from .geo.ground import GroundRouter
from .geo.places import PlaceResolver
from .policy import Policy
from .providers.base import ProviderContext
from .providers.registry import ProviderRegistry
from .store import Store


@dataclass
class Runtime:
    policy: Policy
    airports: AirportIndex
    places: PlaceResolver
    router: GroundRouter
    registry: ProviderRegistry
    store: Store | None

    @classmethod
    def build(
        cls,
        policy: Policy | None = None,
        db_path: str | Path | None = None,
        providers: list[str] | None = None,
    ) -> "Runtime":
        policy = policy or Policy.default()
        store = None
        if db_path is not None:
            store = Store(db_path)

        airports = AirportIndex.load(
            include_small=bool(policy.get("airports.include_small_airports", False))
        )
        places = PlaceResolver.load(airports=airports)
        router = GroundRouter(policy, store)
        ctx = ProviderContext(policy=policy, airports=airports, store=store, places=places)
        registry = ProviderRegistry(ctx, providers)
        return cls(policy, airports, places, router, registry, store)

    def close(self) -> None:
        if self.store is not None:
            self.store.close()

    def __enter__(self) -> "Runtime":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @staticmethod
    def default_db() -> Path:
        return datasets.data_dir() / "flightarb.sqlite3"
