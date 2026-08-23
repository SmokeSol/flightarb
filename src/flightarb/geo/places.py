"""Resolve what a human typed into a real point on earth.

"Casablanca", "casablanca, ma", "Málaga", "33.57,-7.59" and "CMN" must all
work.  Backed by GeoNames cities15000 (free, CC-BY) plus the airport index as
a fallback, so no geocoding API is involved.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from ..models import Place
from . import datasets
from .airports import AirportIndex


def normalize(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text.strip().lower())
    return "".join(c for c in decomposed if not unicodedata.combining(c))


@dataclass
class _City:
    name: str
    lat: float
    lon: float
    country: str
    population: int


@dataclass
class PlaceResolver:
    cities: list[_City]
    airports: AirportIndex | None = None
    _index: dict[str, list[_City]] = field(default_factory=dict, repr=False)
    _metro_cache: dict[tuple, int] = field(default_factory=dict, repr=False)

    @classmethod
    def load(cls, path: str | Path | None = None, airports: AirportIndex | None = None) -> "PlaceResolver":
        """Single pass: parse the row and index its names together, so a
        malformed line can never desynchronise the two structures."""
        txt = Path(path) if path else datasets.ensure_cities()
        cities: list[_City] = []
        index: dict[str, list[_City]] = {}

        with open(txt, "r", encoding="utf-8") as fh:
            for line in fh:
                parts = line.rstrip("\n").split("\t")
                if len(parts) < 15:
                    continue
                try:
                    city = _City(
                        name=parts[1],
                        lat=float(parts[4]),
                        lon=float(parts[5]),
                        country=parts[8].upper(),
                        population=int(parts[14] or 0),
                    )
                except ValueError:
                    continue
                cities.append(city)

                names = {parts[1], parts[2]}
                if parts[3]:
                    # Alternate names are ordered roughly by relevance; a dozen
                    # is plenty to catch the common spellings.
                    names.update(parts[3].split(",")[:12])
                for name in names:
                    key = normalize(name)
                    if key:
                        index.setdefault(key, []).append(city)

        return cls(cities, airports, index)

    # -- resolution ------------------------------------------------------- #
    def resolve(self, query: str) -> Place:
        q = query.strip()
        if not q:
            raise ValueError("empty place")

        # 1. explicit coordinates
        if "," in q:
            head, _, tail = q.partition(",")
            try:
                return Place(name=q, lat=float(head), lon=float(tail), country=None)
            except ValueError:
                pass

        # 2. "City, CC" country hint
        country_hint: str | None = None
        if "," in q:
            head, _, tail = q.rpartition(",")
            tail = tail.strip()
            if len(tail) == 2 and tail.isalpha():
                country_hint = tail.upper()
                q = head.strip()

        # 3. city gazetteer
        hits = self._index.get(normalize(q), [])
        if country_hint:
            hits = [c for c in hits if c.country == country_hint] or hits
        if hits:
            best = max(hits, key=lambda c: c.population)
            return Place(best.name, best.lat, best.lon, best.country, best.population)

        # 4. fall back to the airport index (handles "CMN" and small towns)
        if self.airports is not None:
            found = self.airports.search(q, limit=1)
            if found:
                a = found[0]
                return Place(a.municipality or a.name, a.lat, a.lon, a.country)

        raise LookupError(f"could not resolve place: {query!r}")

    def metro_population(self, lat: float, lon: float, radius_km: float = 60.0) -> int:
        """Size of the largest city an airport actually serves.

        Looked up by coordinates, not by name: OurAirports says an airport is in
        "Seville" while the gazetteer calls the city "Sevilla", and that kind of
        mismatch is the norm rather than the exception across languages.
        """
        from .airports import haversine_km

        key = (round(lat, 2), round(lon, 2), radius_km)
        cached = self._metro_cache.get(key)
        if cached is not None:
            return cached

        deg = radius_km / 111.0
        best = 0
        for city in self.cities:
            if abs(city.lat - lat) > deg or city.population <= best:
                continue
            if haversine_km(lat, lon, city.lat, city.lon) <= radius_km:
                best = city.population
        self._metro_cache[key] = best
        return best

    def resolve_all(self, query: str, limit: int = 5) -> list[Place]:
        hits = self._index.get(normalize(query), [])
        hits = sorted(hits, key=lambda c: -c.population)[:limit]
        return [Place(c.name, c.lat, c.lon, c.country, c.population) for c in hits]
