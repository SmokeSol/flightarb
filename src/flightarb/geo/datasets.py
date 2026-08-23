"""Free, no-key geographic datasets.

* OurAirports  -- public domain, rebuilt nightly, every airport on earth.
* GeoNames cities15000 -- CC-BY, every city over 15k people, with population
  and alternate names (so "Malaga", "Málaga" and "Malaga, Spain" all resolve).

Both are downloaded once into ``data/`` and reused.  Nothing here needs an API
key, an account, or a paid tier.
"""

from __future__ import annotations

import io
import os
import time
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

AIRPORTS_URL = "https://davidmegginson.github.io/ourairports-data/airports.csv"
CITIES_URL = "https://download.geonames.org/export/dump/cities15000.zip"

USER_AGENT = "flightarb/0.1 (personal trip planner; +https://ourairports.com/data/)"
DEFAULT_MAX_AGE_DAYS = 30


def data_dir() -> Path:
    env = os.environ.get("FLIGHTARB_DATA_DIR")
    if env:
        p = Path(env)
    else:
        p = Path(__file__).resolve().parents[3] / "data"
    p.mkdir(parents=True, exist_ok=True)
    return p


@dataclass
class DatasetStatus:
    name: str
    path: Path
    present: bool
    age_days: float | None
    size_bytes: int


def _age_days(path: Path) -> float | None:
    if not path.exists():
        return None
    return (time.time() - path.stat().st_mtime) / 86400.0


def _download(url: str, dest: Path, timeout: int = 90) -> Path:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    tmp = dest.with_suffix(dest.suffix + ".part")
    with urllib.request.urlopen(req, timeout=timeout) as resp, tmp.open("wb") as fh:
        while chunk := resp.read(1 << 16):
            fh.write(chunk)
    tmp.replace(dest)
    return dest


def ensure_airports(force: bool = False, max_age_days: float = DEFAULT_MAX_AGE_DAYS) -> Path:
    dest = data_dir() / "airports.csv"
    age = _age_days(dest)
    if force or age is None or age > max_age_days:
        try:
            _download(AIRPORTS_URL, dest)
        except Exception:
            if dest.exists():
                return dest  # stale beats absent
            raise
    return dest


def ensure_cities(force: bool = False, max_age_days: float = 365.0) -> Path:
    """GeoNames ships a zip; we keep the extracted .txt."""
    dest = data_dir() / "cities15000.txt"
    age = _age_days(dest)
    if not force and age is not None and age <= max_age_days:
        return dest

    zip_path = data_dir() / "cities15000.zip"
    try:
        _download(CITIES_URL, zip_path)
        with zipfile.ZipFile(zip_path) as zf:
            with zf.open("cities15000.txt") as src, dest.open("wb") as out:
                out.write(src.read())
        zip_path.unlink(missing_ok=True)
    except Exception:
        if dest.exists():
            return dest
        raise
    return dest


def ensure_all(force: bool = False) -> list[DatasetStatus]:
    out = []
    for name, fn in (("airports", ensure_airports), ("cities", ensure_cities)):
        try:
            path = fn(force=force)
        except Exception as exc:  # pragma: no cover - network dependent
            out.append(DatasetStatus(name, data_dir() / name, False, None, 0))
            print(f"  ! {name}: {exc}")
            continue
        out.append(
            DatasetStatus(
                name=name,
                path=path,
                present=path.exists(),
                age_days=_age_days(path),
                size_bytes=path.stat().st_size if path.exists() else 0,
            )
        )
    return out


def status() -> list[DatasetStatus]:
    out = []
    for name, fname in (("airports", "airports.csv"), ("cities", "cities15000.txt")):
        p = data_dir() / fname
        out.append(
            DatasetStatus(
                name=name,
                path=p,
                present=p.exists(),
                age_days=_age_days(p),
                size_bytes=p.stat().st_size if p.exists() else 0,
            )
        )
    return out
