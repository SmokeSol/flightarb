"""Core domain objects for the trip-arbitrage engine.

Design notes
------------
* Everything is a plain ``dataclass`` -- no third-party runtime dependency.
* Times are stored as *naive local* datetimes (the way a timetable reads) and
  ``duration_min`` is the authoritative elapsed time.  This sidesteps an entire
  class of timezone bugs: we never subtract a local arrival from a local
  departure across timezones.
* Money is euros as ``float``; rounding happens only at render time.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import Enum
from typing import Any

# --------------------------------------------------------------------------- #
# Enumerations
# --------------------------------------------------------------------------- #


class Cabin(str, Enum):
    ECONOMY = "economy"
    PREMIUM = "premium_economy"
    BUSINESS = "business"
    FIRST = "first"


class Ticketing(str, Enum):
    """How the journey is purchased."""

    ONE_WAY = "one_way"
    RETURN = "return"              # single round-trip ticket
    TWO_ONE_WAYS = "two_one_ways"  # independent outbound + inbound tickets


class Confidence(str, Enum):
    """How much we trust a price."""

    SYNTHETIC = "synthetic"   # simulated market -- never a real bookable price
    DISCOVERY = "discovery"   # aggregator scrape: good for ranking, not truth
    VERIFIED = "verified"     # re-checked at the airline / operator itself


CONFIDENCE_RANK = {
    Confidence.SYNTHETIC: 0,
    Confidence.DISCOVERY: 1,
    Confidence.VERIFIED: 2,
}


class GroundMode(str, Enum):
    NONE = "none"
    CAR = "car"
    TAXI = "taxi"
    TRANSIT = "transit"
    TRAIN = "train"


# --------------------------------------------------------------------------- #
# Geography
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Airport:
    iata: str
    icao: str
    name: str
    municipality: str
    country: str  # ISO-3166 alpha-2
    lat: float
    lon: float
    kind: str     # large_airport | medium_airport | small_airport
    scheduled: bool

    @property
    def label(self) -> str:
        return f"{self.iata} ({self.municipality or self.name})"

    @property
    def size_rank(self) -> int:
        return {"large_airport": 3, "medium_airport": 2, "small_airport": 1}.get(self.kind, 0)


@dataclass(frozen=True, slots=True)
class Place:
    """Where the traveller actually starts or ends -- a city, not an airport."""

    name: str
    lat: float
    lon: float
    country: str | None = None
    population: int = 0

    @property
    def label(self) -> str:
        return self.name


@dataclass(frozen=True, slots=True)
class GroundLeg:
    from_label: str
    to_label: str
    km: float
    minutes: float
    cost_eur: float
    mode: GroundMode = GroundMode.CAR
    source: str = "estimate"  # estimate | osrm | none

    @staticmethod
    def none(label: str) -> "GroundLeg":
        return GroundLeg(label, label, 0.0, 0.0, 0.0, GroundMode.NONE, "none")

    def reversed(self) -> "GroundLeg":
        """The same road travelled the other way -- used on the return leg,
        where a 'home -> airport' hop becomes 'airport -> home'."""
        return GroundLeg(
            self.to_label, self.from_label, self.km, self.minutes,
            self.cost_eur, self.mode, self.source,
        )

    @property
    def is_zero(self) -> bool:
        return self.minutes <= 0.0 and self.cost_eur <= 0.0


# --------------------------------------------------------------------------- #
# Flights
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Segment:
    carrier: str      # IATA airline code, e.g. "AT", "FR"
    flight_no: str
    origin: str       # IATA airport
    destination: str
    depart: datetime  # naive local at origin
    arrive: datetime  # naive local at destination
    duration_min: int  # authoritative elapsed time
    cabin: Cabin = Cabin.ECONOMY

    @property
    def route(self) -> str:
        return f"{self.origin}-{self.destination}"


@dataclass(slots=True)
class FlightOffer:
    """A priced set of segments in ONE direction."""

    segments: tuple[Segment, ...]
    price_eur: float           # per adult, all-in as advertised by the source
    provider: str
    # Round trips are frequently sold as ONE ticket for less than two one-ways
    # -- and, on low-cost carriers, for exactly the same. Offers sharing a
    # ``bundle_id`` must be bought together; ``bundle_price_eur`` is then the
    # authoritative per-seat price for the pair, and ``price_eur`` is only an
    # indicative split used for display.
    bundle_id: str | None = None
    bundle_price_eur: float | None = None
    fare_brand: str = "basic"
    included_cabin_bags: int = 1   # small under-seat bag
    included_checked_bags: int = 0
    self_transfer: bool = False    # connection is not protected by the carrier
    separate_tickets: bool = False  # we stitched this from >1 ticket
    booking_url: str | None = None
    observed_at: datetime = field(default_factory=datetime.now)
    confidence: Confidence = Confidence.DISCOVERY
    raw: dict[str, Any] = field(default_factory=dict)

    # -- derived ---------------------------------------------------------- #
    @property
    def origin(self) -> str:
        return self.segments[0].origin

    @property
    def destination(self) -> str:
        return self.segments[-1].destination

    @property
    def depart(self) -> datetime:
        return self.segments[0].depart

    @property
    def arrive(self) -> datetime:
        return self.segments[-1].arrive

    @property
    def depart_date(self) -> date:
        return self.segments[0].depart.date()

    @property
    def stops(self) -> int:
        return len(self.segments) - 1

    @property
    def carriers(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(s.carrier for s in self.segments))

    @property
    def duration_min(self) -> int:
        """Gate-to-gate elapsed time including connections."""
        total = sum(s.duration_min for s in self.segments)
        for prev, nxt in zip(self.segments, self.segments[1:]):
            total += max(0, int((nxt.depart - prev.arrive).total_seconds() // 60))
        return total

    @property
    def connections(self) -> list[tuple[Segment, Segment, int]]:
        out = []
        for prev, nxt in zip(self.segments, self.segments[1:]):
            gap = int((nxt.depart - prev.arrive).total_seconds() // 60)
            out.append((prev, nxt, gap))
        return out

    @property
    def has_airport_change(self) -> bool:
        return any(p.destination != n.origin for p, n in zip(self.segments, self.segments[1:]))

    @property
    def is_overnight_connection(self) -> bool:
        return any(gap >= 6 * 60 for _, _, gap in self.connections)

    @property
    def is_redeye(self) -> bool:
        return self.arrive.hour < 6 or self.depart.hour < 6

    @property
    def route_label(self) -> str:
        pts = [self.segments[0].origin] + [s.destination for s in self.segments]
        return "-".join(pts)

    @property
    def is_bundled(self) -> bool:
        return self.bundle_id is not None and self.bundle_price_eur is not None

    def key(self) -> str:
        parts = [
            self.route_label,
            self.depart.isoformat(),
            ",".join(self.carriers),
            f"{self.price_eur:.2f}",
        ]
        return hashlib.sha1("|".join(parts).encode()).hexdigest()[:12]


# --------------------------------------------------------------------------- #
# Journeys (door to door)
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class DirectionPlan:
    """One door-to-door direction: ground -> flight -> ground."""

    ground_out: GroundLeg
    offer: FlightOffer
    ground_in: GroundLeg

    def door_to_door_min(self, checkin_buffer_min: int, arrival_buffer_min: int) -> float:
        return (
            self.ground_out.minutes
            + checkin_buffer_min
            + self.offer.duration_min
            + arrival_buffer_min
            + self.ground_in.minutes
        )

    @property
    def label(self) -> str:
        return self.offer.route_label


@dataclass(slots=True)
class CostBreakdown:
    fare: float = 0.0
    bags: float = 0.0
    ground: float = 0.0
    hotel: float = 0.0
    fees: float = 0.0
    time_cost: float = 0.0
    risk_penalty: float = 0.0
    confidence_penalty: float = 0.0
    door_to_door_min: float = 0.0
    notes: list[str] = field(default_factory=list)

    @property
    def cash(self) -> float:
        """What actually leaves the traveller's bank account."""
        return self.fare + self.bags + self.ground + self.hotel + self.fees

    @property
    def utility(self) -> float:
        """Cash plus the monetised cost of time and risk -- the ranking metric."""
        return self.cash + self.time_cost + self.risk_penalty + self.confidence_penalty

    def as_dict(self) -> dict[str, Any]:
        return {
            "fare": round(self.fare, 2),
            "bags": round(self.bags, 2),
            "ground": round(self.ground, 2),
            "hotel": round(self.hotel, 2),
            "fees": round(self.fees, 2),
            "cash": round(self.cash, 2),
            "time_cost": round(self.time_cost, 2),
            "risk_penalty": round(self.risk_penalty, 2),
            "confidence_penalty": round(self.confidence_penalty, 2),
            "utility": round(self.utility, 2),
            "door_to_door_min": round(self.door_to_door_min),
            "notes": list(self.notes),
        }


@dataclass(slots=True)
class Journey:
    """A complete candidate answer to the traveller's question."""

    outbound: DirectionPlan
    inbound: DirectionPlan | None
    ticketing: Ticketing
    cost: CostBreakdown = field(default_factory=CostBreakdown)
    tags: set[str] = field(default_factory=set)
    rejected_reason: str | None = None

    # -- derived ---------------------------------------------------------- #
    @property
    def directions(self) -> tuple[DirectionPlan, ...]:
        return (self.outbound, self.inbound) if self.inbound else (self.outbound,)

    @property
    def offers(self) -> tuple[FlightOffer, ...]:
        return tuple(d.offer for d in self.directions)

    @property
    def confidence(self) -> Confidence:
        return min((o.confidence for o in self.offers), key=lambda c: CONFIDENCE_RANK[c])

    @property
    def max_stops(self) -> int:
        return max(o.stops for o in self.offers)

    @property
    def self_transfer(self) -> bool:
        return any(o.self_transfer for o in self.offers)

    @property
    def endpoint_signature(self) -> str:
        """Airports used, e.g. 'RBA>AGP / AGP>CMN'."""
        return " / ".join(f"{d.offer.origin}>{d.offer.destination}" for d in self.directions)

    @property
    def providers(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(o.provider for o in self.offers))

    def key(self) -> str:
        parts = [self.ticketing.value] + [o.key() for o in self.offers]
        return hashlib.sha1("|".join(parts).encode()).hexdigest()[:12]


# --------------------------------------------------------------------------- #
# The request
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class Party:
    adults: int = 1
    children: int = 0
    infants: int = 0
    cabin_bags: int = 0
    checked_bags: int = 0

    @property
    def seats(self) -> int:
        """Fare-paying seats (an infant rides on a lap)."""
        return self.adults + self.children

    @property
    def people(self) -> int:
        return self.adults + self.children + self.infants


@dataclass(slots=True)
class JourneySpec:
    """The traveller's goal -- deliberately NOT an airport pair."""

    origin: Place
    destination: Place
    depart_date: date
    return_date: date | None = None
    party: Party = field(default_factory=Party)
    cabin: Cabin = Cabin.ECONOMY
    date_flex_days: int = 0
    trip_length_flex_days: int = 0

    @property
    def is_round_trip(self) -> bool:
        return self.return_date is not None

    @property
    def nights(self) -> int | None:
        if self.return_date is None:
            return None
        return (self.return_date - self.depart_date).days

    def depart_window(self) -> list[date]:
        f = self.date_flex_days
        return [self.depart_date + timedelta(days=d) for d in range(-f, f + 1)]

    def return_window(self) -> list[date]:
        if self.return_date is None:
            return []
        f = self.date_flex_days + self.trip_length_flex_days
        return [self.return_date + timedelta(days=d) for d in range(-f, f + 1)]
