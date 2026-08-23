"""Local web UI. Standard library only -- no framework, nothing to install.

    flightarb serve

Serves a single page and one streaming endpoint.  The search is slow enough
(seconds, sometimes tens of seconds against a live carrier) that a spinner is
not good enough, so ``/api/search`` streams newline-delimited JSON: one line per
probe as the planner works, then the finished result.  You watch it think.

The runtime -- airport index, gazetteer, provider registry -- is built once at
startup and shared, because loading it per request would add seconds to every
search.
"""

from __future__ import annotations

import json
import threading
import traceback
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import report
from .engine.search import run_search, validate_spec
from .models import Cabin, JourneySpec, Party
from .runtime import Runtime

INDEX = Path(__file__).parent / "web" / "index.html"


def _int(values: dict, key: str, default: int) -> int:
    try:
        return int(values.get(key, [default])[0])
    except (TypeError, ValueError):
        return default


def _float(values: dict, key: str, default: float | None) -> float | None:
    raw = values.get(key, [None])[0]
    if raw in (None, ""):
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _date(values: dict, key: str) -> date | None:
    raw = values.get(key, [None])[0]
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


class Handler(BaseHTTPRequestHandler):
    server_version = "flightarb"
    runtime: Runtime = None  # type: ignore[assignment]
    lock = threading.Lock()

    def log_message(self, fmt: str, *args) -> None:
        # One tidy line per request instead of the default noise.
        print(f"  {self.address_string()} {fmt % args}")

    # -- plumbing ---------------------------------------------------------- #
    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, payload: dict) -> None:
        self._send(code, json.dumps(payload, default=str).encode("utf-8"), "application/json")

    # -- routes ------------------------------------------------------------- #
    def do_GET(self) -> None:  # noqa: N802
        route = urlparse(self.path)
        if route.path in ("/", "/index.html"):
            try:
                self._send(200, INDEX.read_bytes(), "text/html; charset=utf-8")
            except OSError:
                self._send(500, b"index.html missing", "text/plain")
        elif route.path == "/api/health":
            rt = self.runtime
            self._json(200, {
                "airports": len(rt.airports),
                "cities": len(rt.places.cities),
                "providers": rt.registry.names,
                "unavailable": rt.registry.skipped,
                "real_prices": rt.registry.has_real_prices,
            })
        elif route.path == "/api/search":
            self._search(parse_qs(route.query))
        else:
            self._send(404, b"not found", "text/plain")

    # -- the search --------------------------------------------------------- #
    def _search(self, values: dict) -> None:
        rt = self.runtime
        origin_text = (values.get("origin", [""])[0] or "").strip()
        destination_text = (values.get("destination", [""])[0] or "").strip()
        depart = _date(values, "depart")
        ret = _date(values, "return")

        if not origin_text or not destination_text or depart is None:
            self._json(422, {"error": "origin, destination and depart are required"})
            return

        try:
            origin = rt.places.resolve(origin_text)
            destination = rt.places.resolve(destination_text)
        except LookupError as exc:
            self._json(422, {"error": str(exc)})
            return

        party = Party(
            adults=max(1, _int(values, "adults", 1)),
            children=_int(values, "children", 0),
            infants=_int(values, "infants", 0),
            cabin_bags=_int(values, "cabin_bags", 1),
            checked_bags=_int(values, "checked_bags", 0),
        )
        spec = JourneySpec(
            origin=origin,
            destination=destination,
            depart_date=depart,
            return_date=ret,
            party=party,
            cabin=Cabin(values.get("cabin", ["economy"])[0]),
            date_flex_days=min(5, _int(values, "flex", 0)),
            trip_length_flex_days=min(5, _int(values, "trip_flex", 0)),
        )

        problems = validate_spec(spec)
        if problems:
            self._json(422, {"error": "; ".join(problems)})
            return

        # Per-request policy tweaks, so two people can get different answers
        # from the same running server.
        overrides: dict = {}
        vot = _float(values, "value_of_time", None)
        if vot is not None:
            overrides["traveler.value_of_time_eur_hour"] = vot
        max_origin = _float(values, "max_origin_minutes", None)
        if max_origin is not None:
            overrides["airports.max_origin_reposition_minutes"] = max_origin
        budget = _float(values, "budget", None)
        if budget is not None:
            overrides["search.budget_queries"] = int(budget)

        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        def emit(payload: dict) -> None:
            try:
                self.wfile.write((json.dumps(payload, default=str) + "\n").encode("utf-8"))
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass  # the browser navigated away mid-search

        def on_step(step) -> None:
            emit({
                "type": "step",
                "config": step.config,
                "queries": step.queries,
                "offers": step.offers,
                "best": step.best_utility,
                "note": step.note,
            })

        # The engine mutates shared provider caches, so serialise searches.
        with self.lock:
            try:
                search_runtime = rt
                if overrides:
                    search_runtime = Runtime(
                        policy=rt.policy.override(overrides),
                        airports=rt.airports,
                        places=rt.places,
                        router=rt.router,
                        registry=rt.registry,
                        store=rt.store,
                    )
                emit({"type": "start", "origin": origin.name, "destination": destination.name})
                result = run_search(search_runtime, spec, on_step=on_step)
                emit({"type": "result", "data": json.loads(report.render_json(result))})
            except Exception as exc:  # pragma: no cover - surfaced to the UI
                traceback.print_exc()
                emit({"type": "error", "error": f"{type(exc).__name__}: {exc}"})


def serve(runtime: Runtime, host: str = "127.0.0.1", port: int = 8000) -> None:
    Handler.runtime = runtime
    httpd = ThreadingHTTPServer((host, port), Handler)
    providers = ", ".join(runtime.registry.names) or "none"
    real = "REAL prices" if runtime.registry.has_real_prices else "SIMULATED prices"
    print(f"\n  flightarb  ->  http://{host}:{port}")
    print(f"  providers: {providers}  ({real})")
    print("  ctrl-c to stop\n")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  stopped")
    finally:
        httpd.server_close()
