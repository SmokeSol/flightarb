"""Optional HTTP API.

    pip install "flightarb[api]"
    uvicorn flightarb.api:app --reload

The runtime (airport index, gazetteer, provider registry) is built once at
startup and reused: loading it per request would add seconds to every call.
Searches run in a worker thread because the engine is deliberately blocking.
"""

from __future__ import annotations

import asyncio
from datetime import date
from pathlib import Path
from typing import Any

try:
    from fastapi import FastAPI, HTTPException, Query
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        'the API needs FastAPI: pip install "flightarb[api]"'
    ) from exc

from . import report
from .engine.search import run_search, validate_spec
from .models import Cabin, JourneySpec, Party
from .policy import Policy
from .runtime import Runtime

app = FastAPI(
    title="flightarb",
    description="Autonomous trip-arbitrage engine: ranks door-to-door journeys, not airfares.",
    version="0.1.0",
)

_runtime: Runtime | None = None


def runtime() -> Runtime:
    global _runtime
    if _runtime is None:
        policy_path = Path("policy.toml")
        _runtime = Runtime.build(
            policy=Policy.load(policy_path if policy_path.exists() else None),
            db_path=Runtime.default_db(),
        )
    return _runtime


@app.on_event("startup")
def _warm() -> None:
    runtime()


@app.on_event("shutdown")
def _cool() -> None:
    global _runtime
    if _runtime is not None:
        _runtime.close()
        _runtime = None


@app.get("/health")
def health() -> dict[str, Any]:
    rt = runtime()
    return {
        "status": "ok",
        "airports": len(rt.airports),
        "cities": len(rt.places.cities),
        "providers": rt.registry.names,
        "unavailable": rt.registry.skipped,
        "real_prices": rt.registry.has_real_prices,
    }


@app.get("/search")
async def search(
    origin: str = Query(..., description="city, 'lat,lon', or IATA code"),
    destination: str = Query(...),
    depart: date = Query(...),
    ret: date | None = Query(None, alias="return"),
    adults: int = Query(1, ge=1, le=9),
    children: int = Query(0, ge=0, le=9),
    infants: int = Query(0, ge=0, le=9),
    cabin_bags: int = Query(1, ge=0, le=9),
    checked_bags: int = Query(0, ge=0, le=9),
    cabin: Cabin = Query(Cabin.ECONOMY),
    flex: int = Query(0, ge=0, le=5),
    trip_flex: int = Query(0, ge=0, le=5),
) -> dict[str, Any]:
    rt = runtime()
    try:
        origin_place = rt.places.resolve(origin)
        destination_place = rt.places.resolve(destination)
    except LookupError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    spec = JourneySpec(
        origin=origin_place,
        destination=destination_place,
        depart_date=depart,
        return_date=ret,
        party=Party(adults, children, infants, cabin_bags, checked_bags),
        cabin=cabin,
        date_flex_days=flex,
        trip_length_flex_days=trip_flex,
    )
    problems = validate_spec(spec)
    if problems:
        raise HTTPException(status_code=422, detail=problems)

    # The engine is blocking by design; keep the event loop free.
    result = await asyncio.to_thread(run_search, rt, spec)
    import json

    return json.loads(report.render_json(result))


@app.get("/memory")
def memory() -> dict[str, Any]:
    rt = runtime()
    if rt.store is None:
        return {"enabled": False}
    rows = rt.store.conn.execute(
        "SELECT origin, destination, COUNT(*), ROUND(MIN(price_eur),2), "
        "ROUND(AVG(price_eur),2), ROUND(MAX(price_eur),2) "
        "FROM observations GROUP BY origin, destination ORDER BY COUNT(*) DESC LIMIT 50"
    ).fetchall()
    return {
        "enabled": True,
        "counts": rt.store.counts(),
        "routes": [
            {"origin": o, "destination": d, "observations": n, "min": lo, "avg": avg, "max": hi}
            for o, d, n, lo, avg, hi in rows
        ],
    }
