"""SQLite persistence: ground cache, offer cache, and the price memory.

The price memory is the part that compounds.  Every search writes what it saw;
after a few weeks the engine can say

    "EUR 112 is in the cheapest 10% of the 47 CMN-AGP departures we have
     observed 30-45 days out"

which is a statement about *your* observed market, not a language model's
guess about airfares.
"""

from __future__ import annotations

import json
import sqlite3
import statistics
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Sequence

from .models import Confidence, FlightOffer
from .serde import offer_from_json, offer_to_json

SCHEMA = """
CREATE TABLE IF NOT EXISTS ground_cache (
    lat1 REAL, lon1 REAL, lat2 REAL, lon2 REAL, backend TEXT,
    km REAL, minutes REAL, source TEXT, created_at TEXT,
    PRIMARY KEY (lat1, lon1, lat2, lon2, backend)
);

CREATE TABLE IF NOT EXISTS offer_cache (
    query_key TEXT PRIMARY KEY,
    provider  TEXT NOT NULL,
    payload   TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    origin TEXT NOT NULL,
    destination TEXT NOT NULL,
    carrier TEXT,
    depart_date TEXT NOT NULL,
    depart_weekday INTEGER NOT NULL,
    days_before INTEGER NOT NULL,
    stops INTEGER NOT NULL,
    price_eur REAL NOT NULL,
    provider TEXT NOT NULL,
    confidence TEXT NOT NULL,
    observed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_obs_route ON observations (origin, destination, days_before);
CREATE INDEX IF NOT EXISTS ix_obs_seen  ON observations (observed_at);

CREATE TABLE IF NOT EXISTS searches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ran_at TEXT NOT NULL,
    spec TEXT NOT NULL,
    result TEXT NOT NULL
);
"""


@dataclass
class PriceStats:
    samples: int
    median: float
    p10: float
    p25: float
    minimum: float
    maximum: float

    def percentile_of(self, price: float) -> float:
        """Rough percentile rank of ``price`` in the observed distribution."""
        if self.samples == 0 or self.maximum <= self.minimum:
            return 50.0
        if price <= self.minimum:
            return 0.0
        if price >= self.maximum:
            return 100.0
        span = self.maximum - self.minimum
        return round(100.0 * (price - self.minimum) / span, 1)


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- ground cache ------------------------------------------------------ #
    def get_ground(self, lat1, lon1, lat2, lon2, backend) -> tuple[float, float, str] | None:
        row = self.conn.execute(
            "SELECT km, minutes, source FROM ground_cache "
            "WHERE lat1=? AND lon1=? AND lat2=? AND lon2=? AND backend=?",
            (lat1, lon1, lat2, lon2, backend),
        ).fetchone()
        return (row[0], row[1], row[2]) if row else None

    def put_ground(self, lat1, lon1, lat2, lon2, backend, km, minutes, source) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO ground_cache VALUES (?,?,?,?,?,?,?,?,?)",
            (lat1, lon1, lat2, lon2, backend, km, minutes, source, datetime.now().isoformat()),
        )
        self.conn.commit()

    # -- offer cache ------------------------------------------------------- #
    def get_offers(self, query_key: str, ttl_minutes: int) -> list[FlightOffer] | None:
        row = self.conn.execute(
            "SELECT payload, created_at FROM offer_cache WHERE query_key=?", (query_key,)
        ).fetchone()
        if not row:
            return None
        created = datetime.fromisoformat(row[1])
        if datetime.now() - created > timedelta(minutes=ttl_minutes):
            return None
        try:
            return [offer_from_json(d) for d in json.loads(row[0])]
        except (json.JSONDecodeError, KeyError, ValueError):
            return None

    def put_offers(self, query_key: str, provider: str, offers: Sequence[FlightOffer]) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO offer_cache VALUES (?,?,?,?)",
            (
                query_key,
                provider,
                json.dumps([offer_to_json(o) for o in offers]),
                datetime.now().isoformat(),
            ),
        )
        self.conn.commit()

    # -- price memory ------------------------------------------------------ #
    def record(self, offers: Iterable[FlightOffer]) -> int:
        rows = []
        now = datetime.now()
        for o in offers:
            if o.confidence == Confidence.SYNTHETIC:
                continue  # never pollute real history with simulated prices
            dep = o.depart_date
            rows.append(
                (
                    o.origin,
                    o.destination,
                    o.carriers[0] if o.carriers else None,
                    dep.isoformat(),
                    dep.weekday(),
                    max(0, (dep - now.date()).days),
                    o.stops,
                    float(o.price_eur),
                    o.provider,
                    o.confidence.value,
                    now.isoformat(),
                )
            )
        if not rows:
            return 0
        self.conn.executemany(
            "INSERT INTO observations "
            "(origin,destination,carrier,depart_date,depart_weekday,days_before,"
            " stops,price_eur,provider,confidence,observed_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
        self.conn.commit()
        return len(rows)

    def stats(
        self,
        origin: str,
        destination: str,
        days_before: int,
        window: int = 15,
        max_age_days: int = 180,
    ) -> PriceStats:
        cutoff = (datetime.now() - timedelta(days=max_age_days)).isoformat()
        rows = self.conn.execute(
            "SELECT price_eur FROM observations "
            "WHERE origin=? AND destination=? AND days_before BETWEEN ? AND ? "
            "AND observed_at >= ?",
            (origin, destination, max(0, days_before - window), days_before + window, cutoff),
        ).fetchall()
        prices = sorted(r[0] for r in rows)
        if not prices:
            return PriceStats(0, 0.0, 0.0, 0.0, 0.0, 0.0)

        def pct(p: float) -> float:
            if len(prices) == 1:
                return prices[0]
            k = (len(prices) - 1) * p
            lo, hi = int(k), min(int(k) + 1, len(prices) - 1)
            return prices[lo] + (prices[hi] - prices[lo]) * (k - lo)

        return PriceStats(
            samples=len(prices),
            median=statistics.median(prices),
            p10=pct(0.10),
            p25=pct(0.25),
            minimum=prices[0],
            maximum=prices[-1],
        )

    # -- search log -------------------------------------------------------- #
    def log_search(self, spec: dict[str, Any], result: dict[str, Any]) -> None:
        self.conn.execute(
            "INSERT INTO searches (ran_at, spec, result) VALUES (?,?,?)",
            (datetime.now().isoformat(), json.dumps(spec, default=str), json.dumps(result, default=str)),
        )
        self.conn.commit()

    def counts(self) -> dict[str, int]:
        out = {}
        for table in ("ground_cache", "offer_cache", "observations", "searches"):
            out[table] = self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        return out
