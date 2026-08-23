"""Text and money parsing shared by the adapters that scrape rendered pages.

The ``fast-flights`` adapter no longer needs any of this -- version 3 hands over
structured records with numeric prices. The browser adapter still reads text a
human would read, so the string wrangling lives here rather than being imported
out of a provider that has moved on.
"""

from __future__ import annotations

import re
from datetime import date, datetime, time

# Static conversion table. A flight search does not justify a paid FX feed;
# these only need to be right enough to *rank*, and finalists are re-priced at
# the carrier in their real currency anyway. Where we can ask a source to quote
# in EUR directly, we do that instead of using this.
FX_TO_EUR = {
    "EUR": 1.0, "€": 1.0,
    "USD": 0.92, "$": 0.92,
    "GBP": 1.17, "£": 1.17,
    "MAD": 0.092, "DH": 0.092,
    "CHF": 1.04, "PLN": 0.23, "SEK": 0.088, "NOK": 0.086, "DKK": 0.134,
}

_PRICE_RE = re.compile(r"([€$£]|\b[A-Z]{3}\b)?\s*([\d][\d,.\s]*)")
_TIME_RE = re.compile(r"(\d{1,2}):(\d{2})\s*(AM|PM)?", re.I)
_DUR_RE = re.compile(r"(?:(\d+)\s*hr)?\s*(?:(\d+)\s*min)?", re.I)


def parse_price(text: str) -> tuple[float, str] | None:
    """'€1,234' -> (1234.0, '€'). Returns None when there is no number."""
    if not text:
        return None
    match = _PRICE_RE.search(text.replace(" ", " "))
    if not match:
        return None
    symbol = (match.group(1) or "EUR").strip().upper()
    digits = match.group(2).replace(",", "").replace(" ", "").rstrip(".")
    try:
        return float(digits), symbol
    except ValueError:
        return None


def to_eur(value: float, symbol: str) -> float:
    return value * FX_TO_EUR.get(symbol, 1.0)


def parse_clock(text: str, day: date) -> datetime | None:
    """'8:05 PM' on a given day -> datetime."""
    match = _TIME_RE.search(text or "")
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    meridiem = (match.group(3) or "").upper()
    if meridiem == "PM" and hour < 12:
        hour += 12
    elif meridiem == "AM" and hour == 12:
        hour = 0
    return datetime.combine(day, time(hour % 24, minute))


def parse_duration(text: str) -> int:
    """'2 hr 15 min' -> 135."""
    match = _DUR_RE.search(text or "")
    if not match:
        return 0
    return int(match.group(1) or 0) * 60 + int(match.group(2) or 0)
