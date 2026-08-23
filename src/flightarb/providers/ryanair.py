"""Real fares, straight from the carrier. Standard library only.

Ryanair publishes an unauthenticated fare API that its own website calls.  No
key, no account, no scraping, no package to install -- and it is the carrier
itself, so the prices are the real thing rather than an aggregator's cache.

Why this adapter matters more than its single-airline coverage suggests: on the
Morocco-Spain corridor Ryanair *is* the arbitrage.  It serves Rabat, Marrakesh,
Fez and Tangier, and does not serve Casablanca at all.  Anyone searching
"Casablanca -> Malaga" is structurally blind to a EUR 15 fare an hour up the
motorway.

Efficiency note
---------------
The ``cheapestPerDay`` endpoint returns an entire month of daily prices in one
call.  So a search across a flexible date window costs *one* HTTP request per
route, not one per date.  Months are cached in memory and in SQLite, which is
why a 7-day flexible search can cost a single network call per airport pair.

Limits, stated plainly:

* One airline. It finds Ryanair fares and nothing else.
* The price is the lead-in one-way fare per adult. Children pay the adult fare;
  bags, seats and infants are extra and are modelled by the cost engine.
* ``cheapestPerDay`` gives the cheapest departure on each day, not every
  departure. Good enough to decide where and when to fly.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta

from ..models import Confidence, FlightOffer, Segment
from .base import FlightProvider, ProviderUnavailable, SearchQuery

API = "https://services-api.ryanair.com"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
CARRIER = "FR"


def _get(url: str, timeout: int) -> object:
    request = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


class RyanairProvider(FlightProvider):
    name = "ryanair"
    real_prices = True
    supports_round_trip = True
    is_verifier = False

    #: The provider rate-limits its own HTTP calls (a single search may make
    #: several, or none at all on a cache hit), so the base class must not
    #: charge a second token on top.
    needs_rate_limit = False

    def __init__(self, ctx):
        super().__init__(ctx)
        self._routes: dict[str, set[str]] = {}
        self._months: dict[tuple[str, str, str], dict[date, dict]] = {}
        self._timezones: dict[str, str] = {}

    # -- elapsed time ------------------------------------------------------- #
    def flight_minutes(self, origin: str, destination: str, depart, arrive) -> int:
        """Real elapsed time between two *local* clocks in different zones.

        Ryanair reports local time at each end. Subtracting them naively makes
        Malaga -> Rabat look like it takes one minute, because Spain is an hour
        ahead of Morocco in summer. The route feed carries an IANA timezone for
        every airport, so ``zoneinfo`` (standard library) can do this properly.
        """
        tz_out, tz_in = self._timezones.get(origin), self._timezones.get(destination)
        if tz_out and tz_in:
            try:
                from zoneinfo import ZoneInfo

                delta = arrive.replace(tzinfo=ZoneInfo(tz_in)) - depart.replace(
                    tzinfo=ZoneInfo(tz_out)
                )
                minutes = int(delta.total_seconds() // 60)
                if minutes > 0:
                    return minutes
            except Exception:
                pass

        naive = int((arrive - depart).total_seconds() // 60)
        if naive > 0:
            return naive
        # Last resort: no timezone and a nonsensical clock difference. Estimate
        # from the great-circle distance rather than emit a one-minute flight.
        a = self.ctx.airports.get(origin)
        b = self.ctx.airports.get(destination)
        if a and b:
            from ..geo.airports import haversine_km

            km = haversine_km(a.lat, a.lon, b.lat, b.lon)
            return max(35, int(km / 720.0 * 60 + 35))
        return 120

    # -- route map --------------------------------------------------------- #
    def destinations_from(self, origin: str) -> set[str]:
        """Which airports this origin actually connects to. Cached, because
        asking for a route that does not exist is a wasted call."""
        if origin in self._routes:
            return self._routes[origin]
        timeout = int(self.policy.get("providers.timeout_seconds", 25))
        url = f"{API}/views/locate/searchWidget/routes/en/airport/{origin}"
        try:
            self.limiter.acquire()
            doc = _get(url, timeout)
            found = set()
            for row in doc:
                if not isinstance(row, dict) or row.get("operator") != CARRIER:
                    continue
                arrival = row.get("arrivalAirport") or {}
                code = arrival.get("code")
                if not code:
                    continue
                found.add(code)
                # The feed hands us an IANA zone per airport; keep it, because
                # elapsed flight time is meaningless without one.
                if arrival.get("timeZone"):
                    self._timezones[code] = arrival["timeZone"]
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError,
                json.JSONDecodeError, KeyError, TypeError):
            found = set()  # 404 simply means "Ryanair does not fly here"
        self._routes[origin] = found
        return found

    def serves(self, origin: str, destination: str) -> bool:
        return destination in self.destinations_from(origin)

    # -- month fetch -------------------------------------------------------- #
    def month(self, origin: str, destination: str, when: date) -> dict[date, dict]:
        """Every operating day in ``when``'s month, keyed by date."""
        key = (origin, destination, f"{when:%Y-%m}")
        if key in self._months:
            return self._months[key]

        timeout = int(self.policy.get("providers.timeout_seconds", 25))
        query = urllib.parse.urlencode(
            {"outboundMonthOfDate": when.replace(day=1).isoformat(), "currency": "EUR"}
        )
        url = f"{API}/farfnd/v4/oneWayFares/{origin}/{destination}/cheapestPerDay?{query}"
        try:
            self.limiter.acquire()
            doc = _get(url, timeout)
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError,
                json.JSONDecodeError) as exc:
            raise ProviderUnavailable(f"ryanair {origin}-{destination}: {exc}") from exc

        days: dict[date, dict] = {}
        for fare in (doc or {}).get("outbound", {}).get("fares", []):
            if fare.get("unavailable") or fare.get("soldOut"):
                continue
            price = (fare.get("price") or {}).get("value")
            if price is None:
                continue
            try:
                depart = datetime.fromisoformat(fare["departureDate"])
                arrive = datetime.fromisoformat(fare["arrivalDate"])
                day = date.fromisoformat(fare["day"])
            except (KeyError, ValueError):
                continue
            days[day] = {"depart": depart, "arrive": arrive, "price": float(price)}

        self._months[key] = days
        return days

    # -- offers ------------------------------------------------------------- #
    def _leg(self, origin: str, destination: str, when: date, query: SearchQuery) -> FlightOffer | None:
        if not self.serves(origin, destination):
            return None
        row = self.month(origin, destination, when).get(when)
        if row is None:
            return None

        duration = self.flight_minutes(origin, destination, row["depart"], row["arrive"])
        segment = Segment(
            carrier=CARRIER,
            flight_no=f"{CARRIER}····",
            origin=origin,
            destination=destination,
            depart=row["depart"],
            arrive=row["arrive"],
            duration_min=duration,
            cabin=query.cabin,
        )
        return FlightOffer(
            segments=(segment,),
            price_eur=round(row["price"], 2),
            provider=self.name,
            fare_brand="basic",
            included_cabin_bags=1,   # one small under-seat bag
            included_checked_bags=0,  # everything else is an extra
            confidence=Confidence.VERIFIED,  # the operating carrier's own price
            booking_url=(
                "https://www.ryanair.com/gb/en/trip/flights/select"
                f"?adults={query.seats}&dateOut={when:%Y-%m-%d}"
                f"&originIata={origin}&destinationIata={destination}"
            ),
            raw={"source": "ryanair-cheapest-per-day", "lead_in_fare": True},
        )

    def _search(self, query: SearchQuery) -> list[FlightOffer]:
        outbound = self._leg(query.origin, query.destination, query.depart_date, query)
        if query.return_date is None:
            return [outbound] if outbound else []

        inbound = self._leg(query.destination, query.origin, query.return_date, query)
        if not outbound or not inbound:
            return [o for o in (outbound, inbound) if o]

        # Ryanair sells a return as one booking at exactly the sum of two
        # one-ways. Bundling it says "this is one ticket", which spares it the
        # separate-ticket risk penalty it does not deserve.
        total = round(outbound.price_eur + inbound.price_eur, 2)
        bundle: list[FlightOffer] = []
        for leg in (outbound, inbound):
            leg.bundle_id = "fr-rt"
            leg.bundle_price_eur = total
            bundle.append(leg)
        return bundle
