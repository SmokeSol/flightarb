"""Run a watchlist and publish a static site.

This is what makes the whole thing live on GitHub with no server. A scheduled
Action runs every trip in ``watchlist.toml``, writes the results as plain JSON,
and appends what it saw to a history file committed back to the repository.
GitHub Pages then serves a dashboard that is pure static files reading that
JSON.

The division of labour:

    Actions  -> the engine     (Python, cron and on-demand)
    Pages    -> the front end  (static, no server, free)
    the repo -> the memory     (history compounds across runs)

The history file is the part that gets more valuable every day. After a few
weeks of daily runs, "EUR 61 is cheap for this route" stops being a guess and
becomes a statement about observed prices.
"""

from __future__ import annotations

import json
import re
import shutil
import tomllib
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from . import report
from .engine.search import SearchResult, run_search, validate_spec
from .models import Cabin, JourneySpec, Party
from .policy import Policy
from .runtime import Runtime

_REL = re.compile(r"^([+-])(\d+)d$")


def parse_when(value: str, today: date | None = None) -> date:
    """'2026-09-18' or '+26d'. Relative wins for a standing watch, because a
    fixed date silently becomes a past date and the watch quietly dies."""
    today = today or date.today()
    text = str(value).strip()
    match = _REL.match(text)
    if match:
        sign, days = match.group(1), int(match.group(2))
        return today + timedelta(days=days if sign == "+" else -days)
    return date.fromisoformat(text)


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "trip"


@dataclass
class Trip:
    name: str
    origin: str
    destination: str
    depart: str
    ret: str | None = None
    adults: int = 1
    children: int = 0
    infants: int = 0
    cabin_bags: int = 1
    checked_bags: int = 0
    cabin: str = "economy"
    flex: int = 0
    trip_flex: int = 0
    max_origin_minutes: float | None = None
    value_of_time: float | None = None

    @classmethod
    def from_toml(cls, row: dict[str, Any]) -> "Trip":
        return cls(
            name=row.get("name") or f"{row['origin']} -> {row['destination']}",
            origin=row["origin"],
            destination=row["destination"],
            depart=str(row["depart"]),
            ret=str(row["return"]) if row.get("return") else None,
            adults=int(row.get("adults", 1)),
            children=int(row.get("children", 0)),
            infants=int(row.get("infants", 0)),
            cabin_bags=int(row.get("cabin_bags", 1)),
            checked_bags=int(row.get("checked_bags", 0)),
            cabin=str(row.get("cabin", "economy")),
            flex=int(row.get("flex", 0)),
            trip_flex=int(row.get("trip_flex", 0)),
            max_origin_minutes=row.get("max_origin_minutes"),
            value_of_time=row.get("value_of_time"),
        )

    @property
    def slug(self) -> str:
        return slugify(self.name)

    def overrides(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if self.max_origin_minutes is not None:
            out["airports.max_origin_reposition_minutes"] = float(self.max_origin_minutes)
        if self.value_of_time is not None:
            out["traveler.value_of_time_eur_hour"] = float(self.value_of_time)
        return out

    def spec(self, runtime: Runtime) -> JourneySpec:
        return JourneySpec(
            origin=runtime.places.resolve(self.origin),
            destination=runtime.places.resolve(self.destination),
            depart_date=parse_when(self.depart),
            return_date=parse_when(self.ret) if self.ret else None,
            party=Party(self.adults, self.children, self.infants,
                        self.cabin_bags, self.checked_bags),
            cabin=Cabin(self.cabin),
            date_flex_days=self.flex,
            trip_length_flex_days=self.trip_flex,
        )


def load_watchlist(path: str | Path) -> list[Trip]:
    doc = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    return [Trip.from_toml(row) for row in doc.get("trip", [])]


# --------------------------------------------------------------------------- #
# History
# --------------------------------------------------------------------------- #


def append_history(path: Path, result: SearchResult, trip: Trip) -> int:
    """One line per observed fare. Newline-delimited JSON so a run only ever
    appends -- git diffs stay small and two runs can never corrupt each other."""
    best = result.recommendations.best_value
    if best is None:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    seen_at = datetime.now().isoformat(timespec="seconds")

    rows = []
    for label, journey in result.recommendations.distinct:
        offer = journey.outbound.offer
        rows.append({
            "seen_at": seen_at,
            "trip": trip.slug,
            "label": label,
            "route": journey.endpoint_signature,
            "origin": offer.origin,
            "destination": offer.destination,
            "depart": offer.depart_date.isoformat(),
            "days_before": max(0, (offer.depart_date - date.today()).days),
            "cash_eur": round(journey.cost.cash, 2),
            "fare_eur": round(journey.cost.fare, 2),
            "door_to_door_min": round(journey.cost.door_to_door_min),
            "carriers": list(dict.fromkeys(c for o in journey.offers for c in o.carriers)),
            "confidence": journey.confidence.value,
            "providers": list(journey.providers),
        })
    with path.open("a", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")
    return len(rows)


def read_history(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def summarise_history(rows: list[dict]) -> dict[str, Any]:
    """Per trip: the cheapest-ever seen, and the series to draw."""
    by_trip: dict[str, list[dict]] = {}
    for row in rows:
        if row.get("label", "").startswith("BEST VALUE") or "CHEAPEST" in row.get("label", ""):
            by_trip.setdefault(row["trip"], []).append(row)

    summary: dict[str, Any] = {}
    for trip, entries in by_trip.items():
        entries.sort(key=lambda r: r["seen_at"])
        prices = [e["cash_eur"] for e in entries]
        summary[trip] = {
            "observations": len(entries),
            "cheapest_ever": min(prices),
            "dearest_ever": max(prices),
            "latest": prices[-1],
            "series": [{"at": e["seen_at"], "cash": e["cash_eur"], "route": e["route"]}
                       for e in entries[-60:]],
        }
    return summary


# --------------------------------------------------------------------------- #
# Site build
# --------------------------------------------------------------------------- #


@dataclass
class WatchReport:
    ran: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)
    observations: int = 0


def run_watchlist(
    runtime_policy: Policy,
    watchlist: list[Trip],
    site_dir: Path,
    history_path: Path,
    db_path: Path | None,
    providers: list[str] | None = None,
) -> WatchReport:
    out = WatchReport()
    data_dir = site_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    index: list[dict[str, Any]] = []

    for trip in watchlist:
        policy = runtime_policy.override(trip.overrides()) if trip.overrides() else runtime_policy
        try:
            with Runtime.build(policy=policy, db_path=db_path, providers=providers) as rt:
                spec = trip.spec(rt)
                problems = validate_spec(spec)
                if problems:
                    out.failed[trip.name] = "; ".join(problems)
                    continue
                result = run_search(rt, spec)
        except Exception as exc:
            out.failed[trip.name] = f"{type(exc).__name__}: {exc}"
            continue

        (data_dir / f"{trip.slug}.json").write_text(
            report.render_json(result), encoding="utf-8"
        )
        out.observations += append_history(history_path, result, trip)
        out.ran.append(trip.name)

        best = result.recommendations.best_value
        cheapest = result.recommendations.cheapest
        index.append({
            "slug": trip.slug,
            "name": trip.name,
            "origin": spec.origin.name,
            "destination": spec.destination.name,
            "depart": spec.depart_date.isoformat(),
            "return": spec.return_date.isoformat() if spec.return_date else None,
            "party": {"adults": spec.party.adults, "children": spec.party.children},
            "best_cash": round(best.cost.cash, 2) if best else None,
            "cheapest_cash": round(cheapest.cost.cash, 2) if cheapest else None,
            "route": best.endpoint_signature if best else None,
            "headline": result.headline_for(best) if best else "no journey found",
            "confidence": best.confidence.value if best else None,
            "door_to_door_min": round(best.cost.door_to_door_min) if best else None,
            "providers": result.providers.enabled,
            "queries": result.stats.queries_used,
            "considered": result.considered,
        })

    history = read_history(history_path)
    (data_dir / "index.json").write_text(json.dumps({
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "trips": index,
        "failed": out.failed,
        "history": summarise_history(history),
    }, indent=2), encoding="utf-8")

    dashboard = Path(__file__).parent / "web" / "dashboard.html"
    if dashboard.exists():
        shutil.copyfile(dashboard, site_dir / "index.html")
    nojekyll = site_dir / ".nojekyll"      # Pages otherwise hides _-prefixed paths
    nojekyll.write_text("", encoding="utf-8")
    return out
