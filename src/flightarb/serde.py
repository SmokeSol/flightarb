"""JSON (de)serialisation for the domain objects.

Used by the offer cache, the JSON report and the HTTP API, so the wire format
is defined exactly once.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from .models import (
    Cabin,
    Confidence,
    CostBreakdown,
    DirectionPlan,
    FlightOffer,
    GroundLeg,
    GroundMode,
    Journey,
    Segment,
    Ticketing,
)


# --------------------------------------------------------------------------- #
# Encode
# --------------------------------------------------------------------------- #


def segment_to_json(s: Segment) -> dict[str, Any]:
    return {
        "carrier": s.carrier,
        "flight_no": s.flight_no,
        "origin": s.origin,
        "destination": s.destination,
        "depart": s.depart.isoformat(),
        "arrive": s.arrive.isoformat(),
        "duration_min": s.duration_min,
        "cabin": s.cabin.value,
    }


def offer_to_json(o: FlightOffer) -> dict[str, Any]:
    return {
        "segments": [segment_to_json(s) for s in o.segments],
        "price_eur": round(o.price_eur, 2),
        "provider": o.provider,
        "bundle_id": o.bundle_id,
        "bundle_price_eur": round(o.bundle_price_eur, 2) if o.bundle_price_eur is not None else None,
        "fare_brand": o.fare_brand,
        "included_cabin_bags": o.included_cabin_bags,
        "included_checked_bags": o.included_checked_bags,
        "self_transfer": o.self_transfer,
        "separate_tickets": o.separate_tickets,
        "booking_url": o.booking_url,
        "observed_at": o.observed_at.isoformat(),
        "confidence": o.confidence.value,
        "route": o.route_label,
        "stops": o.stops,
        "duration_min": o.duration_min,
        "raw": o.raw,
    }


def ground_to_json(g: GroundLeg) -> dict[str, Any]:
    return {
        "from": g.from_label,
        "to": g.to_label,
        "km": round(g.km, 1),
        "minutes": round(g.minutes),
        "cost_eur": round(g.cost_eur, 2),
        "mode": g.mode.value,
        "source": g.source,
    }


def direction_to_json(d: DirectionPlan) -> dict[str, Any]:
    return {
        "ground_out": ground_to_json(d.ground_out),
        "flight": offer_to_json(d.offer),
        "ground_in": ground_to_json(d.ground_in),
    }


def journey_to_json(j: Journey) -> dict[str, Any]:
    return {
        "key": j.key(),
        "ticketing": j.ticketing.value,
        "endpoints": j.endpoint_signature,
        "confidence": j.confidence.value,
        "tags": sorted(j.tags),
        "outbound": direction_to_json(j.outbound),
        "inbound": direction_to_json(j.inbound) if j.inbound else None,
        "cost": j.cost.as_dict(),
        "rejected_reason": j.rejected_reason,
    }


# --------------------------------------------------------------------------- #
# Decode
# --------------------------------------------------------------------------- #


def segment_from_json(d: dict[str, Any]) -> Segment:
    return Segment(
        carrier=d["carrier"],
        flight_no=d["flight_no"],
        origin=d["origin"],
        destination=d["destination"],
        depart=datetime.fromisoformat(d["depart"]),
        arrive=datetime.fromisoformat(d["arrive"]),
        duration_min=int(d["duration_min"]),
        cabin=Cabin(d.get("cabin", "economy")),
    )


def offer_from_json(d: dict[str, Any]) -> FlightOffer:
    return FlightOffer(
        segments=tuple(segment_from_json(s) for s in d["segments"]),
        price_eur=float(d["price_eur"]),
        provider=d["provider"],
        bundle_id=d.get("bundle_id"),
        bundle_price_eur=(float(d["bundle_price_eur"]) if d.get("bundle_price_eur") is not None else None),
        fare_brand=d.get("fare_brand", "basic"),
        included_cabin_bags=int(d.get("included_cabin_bags", 1)),
        included_checked_bags=int(d.get("included_checked_bags", 0)),
        self_transfer=bool(d.get("self_transfer", False)),
        separate_tickets=bool(d.get("separate_tickets", False)),
        booking_url=d.get("booking_url"),
        observed_at=datetime.fromisoformat(d["observed_at"]),
        confidence=Confidence(d.get("confidence", "discovery")),
        raw=d.get("raw", {}),
    )
