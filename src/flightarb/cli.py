"""Command line interface.

    flightarb search Casablanca Malaga --depart 2026-09-18 --return 2026-09-22 \
        --adults 2 --children 2 --checked-bags 1 --flex 3

    flightarb data --refresh
    flightarb providers
    flightarb memory
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from . import report
from .engine.search import run_search, validate_spec
from .geo import datasets
from .models import Cabin, JourneySpec, Party
from .policy import Policy
from .runtime import Runtime


def _parse_date(text: str) -> date:
    text = text.strip().lower()
    if text in ("today",):
        return date.today()
    if text in ("tomorrow",):
        return date.today() + timedelta(days=1)
    if text.startswith("+") and text[1:].isdigit():
        return date.today() + timedelta(days=int(text[1:]))
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d %b %Y", "%d %B %Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise argparse.ArgumentTypeError(f"unrecognised date: {text!r} (use YYYY-MM-DD)")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="flightarb",
        description="Autonomous trip-arbitrage engine: door-to-door, not airfare.",
    )
    p.add_argument("--policy", default=None, help="path to policy.toml (default: ./policy.toml)")
    p.add_argument("--db", default=None, help="SQLite path (default: data/flightarb.sqlite3)")
    p.add_argument("--no-db", action="store_true", help="run without cache or price memory")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("search", help="find the best way to make a trip")
    s.add_argument("origin", help="city, 'lat,lon', or IATA code")
    s.add_argument("destination", help="city, 'lat,lon', or IATA code")
    s.add_argument("--depart", required=True, type=_parse_date)
    s.add_argument("--return", dest="return_date", default=None, type=_parse_date)
    s.add_argument("--adults", type=int, default=1)
    s.add_argument("--children", type=int, default=0)
    s.add_argument("--infants", type=int, default=0)
    s.add_argument("--cabin-bags", type=int, default=None)
    s.add_argument("--checked-bags", type=int, default=None)
    s.add_argument("--cabin", default="economy",
                   choices=[c.value for c in Cabin])
    s.add_argument("--flex", type=int, default=0, help="+/- days on both dates")
    s.add_argument("--trip-flex", type=int, default=0, help="+/- days on trip length")
    s.add_argument("--providers", default=None,
                   help="comma-separated: synthetic,fast-flights,browser")
    s.add_argument("--budget", type=int, default=None, help="max provider queries")
    s.add_argument("--max-origin-minutes", type=float, default=None)
    s.add_argument("--max-destination-minutes", type=float, default=None)
    s.add_argument("--value-of-time", type=float, default=None, help="EUR per hour")
    s.add_argument("--osrm", default=None,
                   help="OSRM base URL for real road routing (implies router=osrm)")
    s.add_argument("--json", dest="json_out", default=None, help="write JSON to this path")
    s.add_argument("--html", dest="html_out", default=None, help="write an HTML report here")
    s.add_argument("--quiet", action="store_true", help="suppress live progress")
    s.add_argument("-v", "--verbose", action="store_true", help="print the search trace")

    d = sub.add_parser("data", help="manage the free datasets")
    d.add_argument("--refresh", action="store_true", help="force re-download")

    w = sub.add_parser("serve", help="run the local web UI")
    w.add_argument("--port", type=int, default=8000)
    w.add_argument("--host", default="127.0.0.1")
    w.add_argument("--providers", default=None, help="comma-separated provider list")

    w2 = sub.add_parser("watch", help="run watchlist.toml and build the static site")
    w2.add_argument("--watchlist", default="watchlist.toml")
    w2.add_argument("--site", default="site", help="output directory for GitHub Pages")
    w2.add_argument("--history", default="history/observations.jsonl")
    w2.add_argument("--providers", default=None, help="comma-separated provider list")
    w2.add_argument("--only", default=None, help="run only trips whose name matches this text")

    sub.add_parser("providers", help="show which acquisition adapters are usable")
    sub.add_parser("doctor", help="check this machine can actually run a search")
    sub.add_parser("memory", help="show what the price memory holds")
    return p


def _policy_for(args) -> Policy:
    path = args.policy
    if path is None:
        default = Path("policy.toml")
        path = str(default) if default.exists() else None
    policy = Policy.load(path)

    overrides: dict = {}
    if getattr(args, "budget", None) is not None:
        overrides["search.budget_queries"] = args.budget
    if getattr(args, "max_origin_minutes", None) is not None:
        overrides["airports.max_origin_reposition_minutes"] = args.max_origin_minutes
    if getattr(args, "max_destination_minutes", None) is not None:
        overrides["airports.max_destination_transfer_minutes"] = args.max_destination_minutes
    if getattr(args, "value_of_time", None) is not None:
        overrides["traveler.value_of_time_eur_hour"] = args.value_of_time
    if getattr(args, "cabin_bags", None) is not None:
        overrides["bags.cabin"] = args.cabin_bags
    if getattr(args, "checked_bags", None) is not None:
        overrides["bags.checked"] = args.checked_bags
    if getattr(args, "osrm", None):
        overrides["ground.router"] = "osrm"
        overrides["ground.osrm_url"] = args.osrm
        overrides["ground.osrm_public_demo_optin"] = True
    return policy.override(overrides) if overrides else policy


def cmd_search(args) -> int:
    policy = _policy_for(args)
    db = None if args.no_db else (args.db or Runtime.default_db())
    providers = [p.strip() for p in args.providers.split(",")] if args.providers else None

    with Runtime.build(policy=policy, db_path=db, providers=providers) as rt:
        try:
            origin = rt.places.resolve(args.origin)
            destination = rt.places.resolve(args.destination)
        except LookupError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2

        party = Party(
            adults=args.adults,
            children=args.children,
            infants=args.infants,
            cabin_bags=(args.cabin_bags if args.cabin_bags is not None
                        else int(policy.get("bags.cabin", 1))),
            checked_bags=(args.checked_bags if args.checked_bags is not None
                          else int(policy.get("bags.checked", 0))),
        )
        spec = JourneySpec(
            origin=origin,
            destination=destination,
            depart_date=args.depart,
            return_date=args.return_date,
            party=party,
            cabin=Cabin(args.cabin),
            date_flex_days=args.flex,
            trip_length_flex_days=args.trip_flex,
        )

        problems = validate_spec(spec)
        if problems:
            for problem in problems:
                print(f"error: {problem}", file=sys.stderr)
            return 2

        def on_step(step):
            if args.quiet:
                return
            best = f"{step.best_utility:8.0f}" if step.best_utility is not None else "       -"
            print(f"  probing {step.config:<28s} offers={step.offers:<4d} best={best}",
                  file=sys.stderr)

        result = run_search(rt, spec, on_step=on_step)

        if rt.store is not None:
            rt.store.log_search(
                {"origin": origin.name, "destination": destination.name,
                 "depart": str(spec.depart_date), "return": str(spec.return_date)},
                {"queries": result.stats.queries_used, "considered": result.considered},
            )

        print(report.render_text(result, verbose=args.verbose))

        if args.json_out:
            Path(args.json_out).write_text(report.render_json(result), encoding="utf-8")
            print(f"JSON written to {args.json_out}")
        if args.html_out:
            Path(args.html_out).write_text(report.render_html(result), encoding="utf-8")
            print(f"HTML written to {args.html_out}")
    return 0


def cmd_data(args) -> int:
    if args.refresh:
        print("downloading datasets ...")
        for s in datasets.ensure_all(force=True):
            print(f"  {s.name:9s} {s.size_bytes / 1e6:6.1f} MB  {s.path}")
        return 0
    for s in datasets.status():
        state = "ok " if s.present else "MISSING"
        age = f"{s.age_days:.1f} days old" if s.age_days is not None else "-"
        print(f"  {state} {s.name:9s} {s.size_bytes / 1e6:6.1f} MB  {age}  {s.path}")
    print("\nrun 'flightarb data --refresh' to update")
    return 0


def cmd_serve(args) -> int:
    from .server import serve

    policy = _policy_for(args)
    providers = [p.strip() for p in args.providers.split(",")] if args.providers else None
    db = None if args.no_db else (args.db or Runtime.default_db())
    print("  loading airports and cities ...")
    with Runtime.build(policy=policy, db_path=db, providers=providers) as rt:
        serve(rt, host=args.host, port=args.port)
    return 0


def cmd_watch(args) -> int:
    from .watch import load_watchlist, run_watchlist

    policy = _policy_for(args)
    providers = [p.strip() for p in args.providers.split(",")] if args.providers else None
    trips = load_watchlist(args.watchlist)
    if args.only:
        needle = args.only.lower()
        trips = [t for t in trips if needle in t.name.lower()]
    if not trips:
        print("no trips to run", file=sys.stderr)
        return 1

    print(f"running {len(trips)} watched trip(s)")
    result = run_watchlist(
        runtime_policy=policy,
        watchlist=trips,
        site_dir=Path(args.site),
        history_path=Path(args.history),
        db_path=None if args.no_db else (args.db or Runtime.default_db()),
        providers=providers,
    )
    for name in result.ran:
        print(f"  ok   {name}")
    for name, reason in result.failed.items():
        print(f"  FAIL {name}: {reason}", file=sys.stderr)
    print()
    print(f"{len(result.ran)} ran, {len(result.failed)} failed, "
          f"{result.observations} observation(s) recorded")
    print(f"site written to {args.site}/")
    return 0 if result.ran else 1


def cmd_providers(args) -> int:
    policy = _policy_for(args)
    with Runtime.build(policy=policy, db_path=None) as rt:
        reg = rt.registry
        print("discovery providers")
        for p in reg.providers:
            kind = "real prices" if p.real_prices else "SIMULATED prices"
            print(f"  + {p.name:<16s} {kind}")
        print("\nverifiers")
        for p in reg.verifiers:
            print(f"  + {p.name:<16s} direct at carrier")
        if not reg.verifiers:
            print("  (none active)")
        if reg.skipped:
            print("\nunavailable")
            for name, reason in reg.skipped.items():
                print(f"  - {name:<16s} {reason}")
    return 0


def cmd_doctor(args) -> int:
    """Bring-up check for a fresh machine.

    Everything that can silently make the engine useless -- missing datasets, a
    provider that cannot import, no route to the carrier API -- reported in one
    place, with the fix next to the problem.
    """
    import platform
    import urllib.request

    ok = True

    def line(good: bool, label: str, detail: str = "") -> None:
        nonlocal ok
        ok = ok and good
        print(f"  [{'ok ' if good else 'FAIL'}] {label:<28s} {detail}")

    print()
    print("environment")
    version = sys.version_info
    line(version >= (3, 11), "python >= 3.11", platform.python_version())
    line(True, "platform", f"{platform.system()} {platform.machine()}")

    print()
    print("datasets")
    for s in datasets.status():
        line(s.present, s.name,
             f"{s.size_bytes / 1e6:.1f} MB" if s.present else "missing -> flightarb data --refresh")

    print()
    print("network")
    for label, url in (
        ("ryanair fare api", "https://services-api.ryanair.com/views/locate/searchWidget/routes/en/airport/AGP"),
        ("ourairports", "https://davidmegginson.github.io/ourairports-data/airports.csv"),
    ):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "flightarb/doctor"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                line(resp.status == 200, label, f"HTTP {resp.status}")
        except Exception as exc:
            line(False, label, f"unreachable: {type(exc).__name__}")

    print()
    print("optional packages")
    for module, what in (("fast_flights", "multi-airline discovery"),
                         ("playwright", "browser fallback"),
                         ("fastapi", "JSON API")):
        try:
            __import__(module)
            line(True, module, what)
        except ImportError:
            print(f"  [ -- ] {module:<28s} not installed ({what})")

    print()
    print("providers")
    policy = _policy_for(args)
    try:
        with Runtime.build(policy=policy, db_path=None) as rt:
            for p in rt.registry.providers:
                line(True, p.name, "REAL prices" if p.real_prices else "SIMULATED prices")
            for name, reason in rt.registry.skipped.items():
                print(f"  [ -- ] {name:<28s} {reason}")
            if not rt.registry.providers:
                line(False, "any provider", "none usable -- check providers.enabled")

            print()
            print("smoke test")
            try:
                origin = rt.places.resolve("Casablanca")
                line(True, "place lookup", f"Casablanca -> {origin.lat:.2f},{origin.lon:.2f}")
            except Exception as exc:
                line(False, "place lookup", str(exc))
    except Exception as exc:
        line(False, "runtime", f"{type(exc).__name__}: {exc}")

    print()
    print(f"{'all good -- try: flightarb serve' if ok else 'problems above need fixing'}")
    print()
    return 0 if ok else 1


def cmd_memory(args) -> int:
    from .store import Store

    db = args.db or Runtime.default_db()
    if not Path(db).exists():
        print("no price memory yet -- run a search first")
        return 0
    with Store(db) as store:
        counts = store.counts()
        print("price memory")
        for table, n in counts.items():
            print(f"  {table:<16s} {n:>8d} rows")
        rows = store.conn.execute(
            "SELECT origin, destination, COUNT(*) n, ROUND(MIN(price_eur),0), "
            "ROUND(AVG(price_eur),0), ROUND(MAX(price_eur),0) "
            "FROM observations GROUP BY origin, destination ORDER BY n DESC LIMIT 15"
        ).fetchall()
        if rows:
            print(f"\n  {'route':<12s}{'obs':>6s}{'min':>8s}{'avg':>8s}{'max':>8s}")
            for o, d, n, lo, avg, hi in rows:
                print(f"  {o + '-' + d:<12s}{n:>6d}{lo:>8.0f}{avg:>8.0f}{hi:>8.0f}")
        else:
            print("\n  no real-price observations recorded yet")
            print("  (the synthetic provider is deliberately never written to memory)")
    return 0


def main(argv: list[str] | None = None) -> int:
    report.use_utf8_stdout()
    args = build_parser().parse_args(argv)
    handlers = {
        "search": cmd_search,
        "data": cmd_data,
        "serve": cmd_serve,
        "watch": cmd_watch,
        "providers": cmd_providers,
        "doctor": cmd_doctor,
        "memory": cmd_memory,
    }
    return handlers[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
