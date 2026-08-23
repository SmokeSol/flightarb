"""Browser fallback: a real Chromium session, used sparingly.

When the lightweight extractor returns nothing for a leg the engine still cares
about, this adapter loads the same public search page a person would open and
reads the rendered result list.

Deliberate limits
-----------------
* It is a **fallback**, not the primary source. The planner only reaches for it
  when a cheaper adapter came back empty on a route it wants to keep exploring.
* Consent walls and bot challenges are treated as "this source is unavailable"
  and trip the circuit breaker. Nothing here attempts to solve a CAPTCHA or
  bypass an access control -- if a site does not want automated reads, we stop.
* Requests are serialised and rate-limited; one browser, one page at a time.

Install with:  pip install playwright && playwright install chromium
"""

from __future__ import annotations

import re
import threading
from datetime import datetime, time, timedelta
from urllib.parse import quote

from ..models import Cabin, Confidence, FlightOffer, Segment
from .base import FlightProvider, ProviderUnavailable, SearchQuery
from .fastflights import parse_duration, parse_price, to_eur, _carrier_code

# Aria labels are far more stable than class names, which are minified and
# rotate constantly.
_PRICE_LABEL = re.compile(r"([€$£]\s?[\d,.]+|\b\d[\d,.]*\s?(?:EUR|USD|GBP|MAD)\b)")
_STOPS_LABEL = re.compile(r"(nonstop|non-stop|direct|(\d+)\s+stop)", re.I)
_TIME_LABEL = re.compile(r"(\d{1,2}):(\d{2})\s*(AM|PM)?", re.I)


class BrowserProvider(FlightProvider):
    name = "browser"
    real_prices = True
    supports_round_trip = True

    def __init__(self, ctx):
        super().__init__(ctx)
        self._lock = threading.Lock()
        try:
            from playwright.sync_api import sync_playwright  # type: ignore

            self._sync_playwright = sync_playwright
        except Exception:
            self._sync_playwright = None

    def available(self) -> bool:
        return self._sync_playwright is not None and super().available()

    def unavailable_reason(self) -> str | None:
        if self._sync_playwright is None:
            return "playwright not installed (pip install playwright && playwright install chromium)"
        return super().unavailable_reason()

    # ------------------------------------------------------------------ #
    @staticmethod
    def search_url(query: SearchQuery) -> str:
        """Public deep link, the same one the site builds from its own form."""
        seat = {
            Cabin.ECONOMY: "Economy",
            Cabin.PREMIUM: "Premium economy",
            Cabin.BUSINESS: "Business",
            Cabin.FIRST: "First",
        }[query.cabin]
        parts = [
            f"Flights from {query.origin} to {query.destination}",
            f"on {query.depart_date:%Y-%m-%d}",
        ]
        if query.return_date:
            parts.append(f"through {query.return_date:%Y-%m-%d}")
        parts.append(f"{seat} class")
        if query.seats > 1:
            parts.append(f"{query.seats} passengers")
        return "https://www.google.com/travel/flights?q=" + quote(" ".join(parts))

    def _search(self, query: SearchQuery) -> list[FlightOffer]:
        if self._sync_playwright is None:
            raise ProviderUnavailable("playwright not installed")

        timeout_ms = int(self.policy.get("providers.timeout_seconds", 25)) * 1000
        with self._lock:  # one page at a time; be a light visitor
            with self._sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True)
                try:
                    page = browser.new_page(
                        locale="en-GB",
                        user_agent=(
                            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
                        ),
                    )
                    page.goto(self.search_url(query), timeout=timeout_ms, wait_until="domcontentloaded")

                    body = (page.content() or "").lower()
                    if any(m in body for m in ("before you continue", "unusual traffic", "recaptcha", "/sorry/")):
                        # Consent wall or bot challenge: this source is closed to
                        # us right now. We do not try to get around it.
                        raise ProviderUnavailable("blocked by consent or bot challenge")

                    try:
                        page.wait_for_selector("li", timeout=timeout_ms // 2)
                    except Exception as exc:
                        raise ProviderUnavailable(f"no result list rendered: {exc}") from exc

                    rows = page.query_selector_all("li")
                    texts = []
                    for row in rows[:60]:
                        try:
                            label = row.get_attribute("aria-label") or row.inner_text()
                        except Exception:
                            continue
                        if label and len(label) > 40:
                            texts.append(label)
                finally:
                    browser.close()

        return self._parse_rows(texts, query)

    def _parse_rows(self, texts: list[str], query: SearchQuery) -> list[FlightOffer]:
        offers: list[FlightOffer] = []
        seen: set[str] = set()
        for text in texts:
            flat = " ".join(text.split())
            pm = _PRICE_LABEL.search(flat)
            if not pm:
                continue
            priced = parse_price(pm.group(1))
            if priced is None:
                continue
            amount, symbol = priced
            price_eur = to_eur(amount, symbol)
            if price_eur <= 0:
                continue

            sm = _STOPS_LABEL.search(flat)
            stops = 0
            if sm and sm.group(2):
                stops = int(sm.group(2))
            elif sm is None:
                stops = 1  # unknown: assume worse, never better
            if stops > query.max_stops:
                continue

            duration = parse_duration(flat) or 150
            tm = _TIME_LABEL.search(flat)
            depart = datetime.combine(query.depart_date, time(9, 0))
            if tm:
                hour, minute = int(tm.group(1)), int(tm.group(2))
                mer = (tm.group(3) or "").upper()
                if mer == "PM" and hour < 12:
                    hour += 12
                elif mer == "AM" and hour == 12:
                    hour = 0
                depart = datetime.combine(query.depart_date, time(hour % 24, minute))

            carrier = _carrier_code(flat)
            fingerprint = f"{carrier}{depart:%H%M}{round(price_eur)}{stops}"
            if fingerprint in seen:
                continue
            seen.add(fingerprint)

            seg = Segment(
                carrier=carrier,
                flight_no=f"{carrier}----",
                origin=query.origin,
                destination=query.destination,
                depart=depart,
                arrive=depart + timedelta(minutes=duration),
                duration_min=duration,
                cabin=query.cabin,
            )
            offers.append(
                FlightOffer(
                    segments=(seg,),
                    price_eur=round(price_eur, 2),
                    provider=self.name,
                    fare_brand="unknown",
                    confidence=Confidence.DISCOVERY,
                    booking_url=self.search_url(query),
                    raw={"stops": stops, "row": flat[:220]},
                )
            )
        offers.sort(key=lambda o: o.price_eur)
        return offers[:15]
