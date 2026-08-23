"""Airport index built on the OurAirports public-domain dump."""

from __future__ import annotations

import csv
import math
from dataclasses import dataclass, field

from pathlib import Path

from ..models import Airport
from . import datasets

EARTH_KM = 6371.0088


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_KM * math.asin(math.sqrt(a))


@dataclass
class AirportIndex:
    airports: list[Airport]
    _iata_index: dict[str, Airport] | None = field(default=None, repr=False, compare=False)

    # -- construction ----------------------------------------------------- #
    @classmethod
    def load(cls, path: str | Path | None = None, include_small: bool = False) -> "AirportIndex":
        csv_path = Path(path) if path else datasets.ensure_airports()
        keep_kinds = {"large_airport", "medium_airport"}
        if include_small:
            keep_kinds.add("small_airport")

        out: list[Airport] = []
        with open(csv_path, "r", encoding="utf-8", newline="") as fh:
            for row in csv.DictReader(fh):
                iata = (row.get("iata_code") or "").strip().upper()
                kind = (row.get("type") or "").strip()
                if len(iata) != 3 or kind not in keep_kinds:
                    continue
                try:
                    lat = float(row["latitude_deg"])
                    lon = float(row["longitude_deg"])
                except (TypeError, ValueError, KeyError):
                    continue
                out.append(
                    Airport(
                        iata=iata,
                        icao=(row.get("icao_code") or row.get("ident") or "").strip().upper(),
                        name=(row.get("name") or "").strip(),
                        municipality=(row.get("municipality") or "").strip(),
                        country=(row.get("iso_country") or "").strip().upper(),
                        lat=lat,
                        lon=lon,
                        kind=kind,
                        scheduled=(row.get("scheduled_service") or "").strip().lower() == "yes",
                    )
                )
        return cls(out)

    # -- lookup ----------------------------------------------------------- #
    def _by_iata(self) -> dict[str, Airport]:
        if self._iata_index is None:
            self._iata_index = {a.iata: a for a in self.airports}
        return self._iata_index

    def get(self, iata: str) -> Airport | None:
        return self._by_iata().get(iata.strip().upper())

    def __len__(self) -> int:
        return len(self.airports)

    def near(
        self,
        lat: float,
        lon: float,
        radius_km: float = 400.0,
        limit: int = 12,
        scheduled_only: bool = True,
    ) -> list[tuple[Airport, float]]:
        """Great-circle shortlist. Deliberately generous -- the ground router
        then re-ranks by *driving minutes*, which is what actually matters."""
        hits: list[tuple[Airport, float]] = []
        for a in self.airports:
            if scheduled_only and not a.scheduled:
                continue
            # Cheap bounding box before the trig.
            if abs(a.lat - lat) > radius_km / 111.0:
                continue
            d = haversine_km(lat, lon, a.lat, a.lon)
            if d <= radius_km:
                hits.append((a, d))
        hits.sort(key=lambda t: (t[1] / max(1, t[0].size_rank), t[1]))
        return hits[:limit]

    def search(self, text: str, limit: int = 8) -> list[Airport]:
        """Loose text lookup: IATA, city name or airport name."""
        q = text.strip().lower()
        if len(q) == 3:
            hit = self.get(q)
            if hit:
                return [hit]
        scored: list[tuple[int, Airport]] = []
        for a in self.airports:
            muni = a.municipality.lower()
            name = a.name.lower()
            if muni == q:
                scored.append((0, a))
            elif name == q:
                scored.append((1, a))
            elif q in muni:
                scored.append((2, a))
            elif q in name:
                scored.append((3, a))
        scored.sort(key=lambda t: (t[0], -t[1].size_rank, t[1].iata))
        return [a for _, a in scored[:limit]]
