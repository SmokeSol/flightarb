"""Render a search result: terminal, JSON, or a self-contained HTML page."""

from __future__ import annotations

import html as _html
import json
import sys
from datetime import datetime

from .engine import explain
from .engine.pareto import Recommendations, friction
from .engine.search import SearchResult
from .models import Confidence, Journey, Ticketing
from .serde import journey_to_json

BAR = "=" * 78
THIN = "-" * 78

BADGE = {
    "BEST VALUE": "[*] BEST VALUE",
    "CHEAPEST": "[E] CHEAPEST",
    "EASIEST": "[>] EASIEST",
}


def _hm(minutes: float) -> str:
    return explain._hm(minutes)


def use_utf8_stdout() -> None:
    """Windows terminals default to a legacy code page; make output safe."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


# --------------------------------------------------------------------------- #
# Terminal
# --------------------------------------------------------------------------- #


def render_text(result: SearchResult, verbose: bool = False) -> str:
    spec = result.spec
    out: list[str] = []
    add = out.append

    party = spec.party
    who = f"{party.adults} adult" + ("s" if party.adults != 1 else "")
    if party.children:
        who += f" + {party.children} child" + ("ren" if party.children != 1 else "")
    if party.infants:
        who += f" + {party.infants} infant" + ("s" if party.infants != 1 else "")
    bags = f"{party.cabin_bags} cabin / {party.checked_bags} checked"

    add(BAR)
    add(f" {spec.origin.name.upper()}  ->  {spec.destination.name.upper()}")
    dates = f"{spec.depart_date:%a %d %b %Y}"
    if spec.return_date:
        dates += f"  ->  {spec.return_date:%a %d %b %Y}  ({spec.nights} nights)"
    add(f" {dates}")
    add(f" {who}, bags: {bags}, {spec.cabin.value}")
    add(BAR)

    add("")
    add(" Airports considered")
    add(f"   from {spec.origin.name}:")
    for ep in result.origins:
        mark = "*" if ep.is_baseline else " "
        add(
            f"     {mark} {ep.iata}  {ep.airport.municipality[:20]:<20s} "
            f"{ep.minutes:5.0f} min   EUR {ep.leg.cost_eur:5.1f}   [{ep.leg.source}]"
        )
    add(f"   to {spec.destination.name}:")
    for ep in result.destinations:
        mark = "*" if ep.is_baseline else " "
        add(
            f"     {mark} {ep.iata}  {ep.airport.municipality[:20]:<20s} "
            f"{ep.minutes:5.0f} min   EUR {ep.leg.cost_eur:5.1f}   [{ep.leg.source}]"
        )
    add("   (* = what an ordinary search would have used)")

    if result.baseline is not None:
        add("")
        add(THIN)
        add(" BASELINE -- what a normal search would show you")
        add(THIN)
        for line in _journey_block(result.baseline, result, indent=" "):
            add(line)

    add("")
    add(BAR)
    add(" RECOMMENDATIONS")
    add(BAR)

    picks = result.recommendations.distinct
    if not picks:
        add("")
        add("  No feasible journey found under your policy constraints.")
    for label, journey in picks:
        add("")
        add(f" {BADGE.get(label, label)}")
        add(f"   {result.headline_for(journey)}")
        add("")
        for line in _journey_block(journey, result, indent="   "):
            add(line)
        reasons = result.why_for(journey)
        if reasons:
            add("")
            add("   Why:")
            for r in reasons:
                add(f"     - {r}")

    if result.price_stats is not None and result.recommendations.best_value is not None:
        note = explain.deal_score(result.recommendations.best_value, result.price_stats)
        if note:
            add("")
            add(" Price memory")
            add(f"   {note}")

    rejected = result.recommendations.rejected
    if rejected:
        add("")
        add(BAR)
        add(" INVESTIGATED AND REJECTED")
        add(BAR)
        for rej in rejected[:6]:
            j = rej.journey
            fare = j.cost.fare
            add("")
            add(f"   {j.endpoint_signature}  fare EUR {fare:.0f}")
            add(f"     rejected: {rej.reason}")

    add("")
    add(THIN)
    s = result.stats
    add(
        f" {s.configs_visited} configurations probed, {s.queries_used} provider queries, "
        f"{result.considered} journeys assembled, {s.seconds:.1f}s"
    )
    add(f" stopped because: {s.stopped_because}")
    if s.verified:
        add(f" {s.verified} finalist(s) re-priced at the operating carrier")
    prov = result.providers
    add(f" providers: {', '.join(prov.enabled) or 'none'}  "
        f"(cache hits {prov.cache_hits}, failures {prov.failures})")

    for w in result.warnings:
        add(f" ! {w}")

    if verbose:
        add("")
        add(" Search trace")
        for step in result.trace:
            util = f"{step.best_utility:8.0f}" if step.best_utility is not None else "       -"
            add(
                f"   {step.config:<28s} q={step.queries} offers={step.offers:<4d} "
                f"best={util}  {step.note}"
            )
    add("")
    return "\n".join(out)


def _journey_block(journey: Journey, result: SearchResult, indent: str = "") -> list[str]:
    c = journey.cost
    lines: list[str] = []
    conf = journey.confidence
    tag = {
        Confidence.SYNTHETIC: "  [SIMULATED PRICE]",
        Confidence.DISCOVERY: "  [unverified]",
        Confidence.VERIFIED: "  [verified at carrier]",
    }[conf]

    ticket = {
        Ticketing.RETURN: "one round-trip ticket",
        Ticketing.TWO_ONE_WAYS: "two separate one-way tickets",
        Ticketing.ONE_WAY: "one-way",
    }[journey.ticketing]

    lines.append(f"{indent}EUR {c.cash:7.0f} cash   |   {_hm(c.door_to_door_min)} door to door"
                 f"   |   {ticket}{tag}")
    lines.append("")

    for name, plan in (("OUT", journey.outbound), ("BACK", journey.inbound)):
        if plan is None:
            continue
        o = plan.offer
        lines.append(f"{indent}  {name}  {o.depart:%a %d %b}")
        if plan.ground_out.minutes > 0:
            lines.append(
                f"{indent}    {plan.ground_out.from_label} -> {plan.ground_out.to_label}"
                f"   {plan.ground_out.minutes:.0f} min, EUR {plan.ground_out.cost_eur:.0f} by "
                f"{plan.ground_out.mode.value}"
            )
        for seg in o.segments:
            lines.append(
                f"{indent}    {seg.depart:%H:%M} {seg.origin} -> {seg.arrive:%H:%M} {seg.destination}"
                f"   {seg.carrier} {seg.flight_no}   {_hm(seg.duration_min)}"
            )
        for _prev, _nxt, gap in o.connections:
            lines.append(f"{indent}      layover {_hm(gap)}"
                         + ("  (self-transfer)" if o.self_transfer else ""))
        if plan.ground_in.minutes > 0:
            lines.append(
                f"{indent}    {plan.ground_in.from_label} -> {plan.ground_in.to_label}"
                f"   {plan.ground_in.minutes:.0f} min, EUR {plan.ground_in.cost_eur:.0f}"
            )
        lines.append("")

    lines.append(f"{indent}  cost breakdown")
    rows = [
        ("fare", c.fare),
        ("bags", c.bags),
        ("ground transport", c.ground),
        ("hotel", c.hotel),
        ("booking fees", c.fees),
    ]
    for label, value in rows:
        if value:
            lines.append(f"{indent}    {label:<20s} EUR {value:8.2f}")
    lines.append(f"{indent}    {'-' * 32}")
    lines.append(f"{indent}    {'cash total':<20s} EUR {c.cash:8.2f}")
    if c.time_cost:
        lines.append(f"{indent}    {'time cost':<20s} EUR {c.time_cost:8.2f}   "
                     f"({_hm(c.door_to_door_min)} valued)")
    if c.risk_penalty:
        lines.append(f"{indent}    {'risk penalty':<20s} EUR {c.risk_penalty:8.2f}")
    if c.confidence_penalty:
        lines.append(f"{indent}    {'uncertainty':<20s} EUR {c.confidence_penalty:8.2f}")
    lines.append(f"{indent}    {'-' * 32}")
    lines.append(
        f"{indent}    {'decision score':<20s} EUR {c.utility:8.2f}"
        f"   <- ranking only, not money owed"
    )
    return lines


# --------------------------------------------------------------------------- #
# JSON
# --------------------------------------------------------------------------- #


def render_json(result: SearchResult) -> str:
    recs: Recommendations = result.recommendations
    doc = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "request": {
            "origin": result.spec.origin.name,
            "destination": result.spec.destination.name,
            "depart_date": result.spec.depart_date.isoformat(),
            "return_date": result.spec.return_date.isoformat() if result.spec.return_date else None,
            "adults": result.spec.party.adults,
            "children": result.spec.party.children,
            "infants": result.spec.party.infants,
            "cabin_bags": result.spec.party.cabin_bags,
            "checked_bags": result.spec.party.checked_bags,
            "cabin": result.spec.cabin.value,
        },
        "airports_considered": {
            "origin": [
                {"iata": e.iata, "minutes": round(e.minutes), "cost_eur": round(e.leg.cost_eur, 2),
                 "baseline": e.is_baseline, "source": e.leg.source}
                for e in result.origins
            ],
            "destination": [
                {"iata": e.iata, "minutes": round(e.minutes), "cost_eur": round(e.leg.cost_eur, 2),
                 "baseline": e.is_baseline, "source": e.leg.source}
                for e in result.destinations
            ],
        },
        "baseline": journey_to_json(result.baseline) if result.baseline else None,
        "recommendations": [
            {
                "label": label,
                "headline": result.headline_for(journey),
                "why": result.why_for(journey),
                "journey": journey_to_json(journey),
            }
            for label, journey in recs.distinct
        ],
        "pareto_front": [journey_to_json(j) for j in recs.front],
        "rejected": [
            {"reason": r.reason, "journey": journey_to_json(r.journey)} for r in recs.rejected
        ],
        "stats": {
            "configs_visited": result.stats.configs_visited,
            "queries_used": result.stats.queries_used,
            "journeys_considered": result.considered,
            "seconds": round(result.stats.seconds, 2),
            "stopped_because": result.stats.stopped_because,
            "verified": result.stats.verified,
        },
        "providers": {
            "enabled": result.providers.enabled,
            "skipped": result.providers.skipped,
            "per_provider": result.providers.per_provider,
        },
        "trace": [
            {"config": t.config, "queries": t.queries, "offers": t.offers,
             "best_utility": t.best_utility, "note": t.note}
            for t in result.trace
        ],
        "warnings": result.warnings,
    }
    return json.dumps(doc, indent=2, default=str)


# --------------------------------------------------------------------------- #
# HTML
# --------------------------------------------------------------------------- #


def render_html(result: SearchResult) -> str:
    e = _html.escape
    spec = result.spec
    parts: list[str] = []
    add = parts.append

    add(f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{e(spec.origin.name)} to {e(spec.destination.name)}</title>
<style>
  :root {{ --bg:#fbfaf8; --fg:#1c1a18; --muted:#6b665f; --line:#e3ded6;
           --card:#fff; --accent:#7a4a1e; --good:#1f6b3a; --bad:#8a2b2b; }}
  @media (prefers-color-scheme: dark) {{
    :root {{ --bg:#14130f; --fg:#ece7df; --muted:#9c968c; --line:#2c2a25;
             --card:#1c1a16; --accent:#d79a5b; --good:#63b884; --bad:#e08585; }}
  }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; padding:2rem 1rem; background:var(--bg); color:var(--fg);
          font:16px/1.55 ui-sans-serif,-apple-system,"Segoe UI",system-ui,sans-serif; }}
  main {{ max-width:60rem; margin:0 auto; }}
  h1 {{ font-size:1.6rem; margin:0 0 .25rem; letter-spacing:-.01em; }}
  .sub {{ color:var(--muted); margin-bottom:2rem; }}
  .card {{ background:var(--card); border:1px solid var(--line); border-radius:12px;
           padding:1.25rem 1.4rem; margin:0 0 1.25rem; }}
  .badge {{ display:inline-block; font-size:.72rem; letter-spacing:.09em;
            text-transform:uppercase; font-weight:700; color:var(--accent);
            border:1px solid var(--accent); border-radius:100px; padding:.15rem .6rem; }}
  .headline {{ font-size:1.15rem; font-weight:600; margin:.7rem 0 .2rem; }}
  .price {{ font-size:1.7rem; font-weight:700; letter-spacing:-.02em; }}
  .meta {{ color:var(--muted); font-size:.9rem; }}
  table {{ width:100%; border-collapse:collapse; margin:.75rem 0; font-size:.9rem; }}
  td {{ padding:.28rem 0; border-bottom:1px dashed var(--line); }}
  td:last-child {{ text-align:right; font-variant-numeric:tabular-nums; }}
  ul {{ margin:.5rem 0 0; padding-left:1.1rem; }}
  li {{ margin:.2rem 0; }}
  .leg {{ font-variant-numeric:tabular-nums; font-size:.92rem; margin:.15rem 0; }}
  .ground {{ color:var(--muted); font-size:.88rem; }}
  .rej {{ border-left:3px solid var(--bad); padding-left:.9rem; margin:.9rem 0; }}
  .warn {{ color:var(--bad); font-size:.9rem; }}
  footer {{ color:var(--muted); font-size:.82rem; margin-top:2rem;
            border-top:1px solid var(--line); padding-top:1rem; }}
  .scroll {{ overflow-x:auto; }}
</style></head><body><main>""")

    add(f"<h1>{e(spec.origin.name)} &rarr; {e(spec.destination.name)}</h1>")
    d = f"{spec.depart_date:%a %d %b %Y}"
    if spec.return_date:
        d += f" &rarr; {spec.return_date:%a %d %b %Y} ({spec.nights} nights)"
    add(f'<div class="sub">{d} &middot; {spec.party.adults} adult(s)'
        f'{f", {spec.party.children} child(ren)" if spec.party.children else ""} &middot; '
        f'{spec.party.cabin_bags} cabin / {spec.party.checked_bags} checked</div>')

    for label, journey in result.recommendations.distinct:
        c = journey.cost
        add('<section class="card">')
        add(f'<span class="badge">{e(label)}</span>')
        add(f'<div class="headline">{e(result.headline_for(journey))}</div>')
        add(f'<div class="price">EUR {c.cash:,.0f}</div>')
        add(f'<div class="meta">{e(_hm(c.door_to_door_min))} door to door &middot; '
            f'{e(journey.endpoint_signature)} &middot; friction {friction(journey):.0f}</div>')
        add('<div class="scroll">')
        for name, plan in (("Out", journey.outbound), ("Back", journey.inbound)):
            if plan is None:
                continue
            add(f'<div class="leg"><strong>{name}</strong> {plan.offer.depart:%a %d %b}</div>')
            if plan.ground_out.minutes > 0:
                add(f'<div class="ground">&darr; {e(plan.ground_out.from_label)} to '
                    f'{e(plan.ground_out.to_label)}: {plan.ground_out.minutes:.0f} min, '
                    f'EUR {plan.ground_out.cost_eur:.0f}</div>')
            for seg in plan.offer.segments:
                add(f'<div class="leg">{seg.depart:%H:%M} {e(seg.origin)} &rarr; '
                    f'{seg.arrive:%H:%M} {e(seg.destination)} &middot; {e(seg.carrier)} '
                    f'{e(seg.flight_no)} &middot; {e(_hm(seg.duration_min))}</div>')
            if plan.ground_in.minutes > 0:
                add(f'<div class="ground">&darr; {e(plan.ground_in.from_label)} to '
                    f'{e(plan.ground_in.to_label)}: {plan.ground_in.minutes:.0f} min, '
                    f'EUR {plan.ground_in.cost_eur:.0f}</div>')
        add("</div>")

        add("<table>")
        for lbl, val in (("Fare", c.fare), ("Bags", c.bags), ("Ground", c.ground),
                         ("Hotel", c.hotel), ("Fees", c.fees)):
            if val:
                add(f"<tr><td>{lbl}</td><td>EUR {val:,.2f}</td></tr>")
        add(f"<tr><td><strong>Cash total</strong></td><td><strong>EUR {c.cash:,.2f}</strong></td></tr>")
        if c.time_cost:
            add(f"<tr><td>Time cost</td><td>EUR {c.time_cost:,.2f}</td></tr>")
        if c.risk_penalty:
            add(f"<tr><td>Risk penalty</td><td>EUR {c.risk_penalty:,.2f}</td></tr>")
        add(f"<tr><td>Decision score <span class=\"meta\">(ranking only)</span></td>"
            f"<td>EUR {c.utility:,.2f}</td></tr>")
        add("</table>")

        reasons = result.why_for(journey)
        if reasons:
            add("<ul>" + "".join(f"<li>{e(r)}</li>" for r in reasons) + "</ul>")
        add("</section>")

    if result.recommendations.rejected:
        add('<section class="card"><span class="badge">Investigated and rejected</span>')
        for rej in result.recommendations.rejected[:6]:
            add(f'<div class="rej"><strong>{e(rej.journey.endpoint_signature)}</strong> '
                f'&middot; fare EUR {rej.journey.cost.fare:,.0f}<br>'
                f'<span class="meta">{e(rej.reason)}</span></div>')
        add("</section>")

    s = result.stats
    add("<footer>")
    add(f"{s.configs_visited} configurations probed &middot; {s.queries_used} provider queries "
        f"&middot; {result.considered} journeys assembled &middot; {s.seconds:.1f}s &middot; "
        f"stopped: {e(s.stopped_because)}")
    for w in result.warnings:
        add(f'<div class="warn">! {e(w)}</div>')
    add("</footer></main></body></html>")
    return "\n".join(parts)
