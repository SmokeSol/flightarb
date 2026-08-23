# Setup on a remote box (Vast.ai or any Linux host)

Everything below assumes a fresh Ubuntu-ish machine with Python 3.11+.
No API keys, no accounts, no paid services anywhere in this process.

---

## 1. Bring it up

```bash
git clone https://github.com/<you>/flightarb.git
cd flightarb
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
flightarb data --refresh
flightarb doctor
```

`doctor` is the whole bring-up check in one command. It verifies the Python
version, that both datasets downloaded, that the carrier API is reachable from
that machine, which optional packages are present, and which providers are
actually usable. Fix anything it marks `FAIL` before going further.

Expected good output:

```
datasets
  [ok ] airports                     12.7 MB
  [ok ] cities                        8.4 MB
network
  [ok ] ryanair fare api             HTTP 200
providers
  [ok ] ryanair                      REAL prices
```

## 2. Confirm it works

```bash
python -m pytest -q                 # 60 tests
flightarb search Casablanca Malaga --depart 2026-09-18 --return 2026-09-22 \
  --adults 2 --children 2 --checked-bags 1 --flex 3 --max-origin-minutes 240
```

## 3. Web UI over SSH

The server binds to localhost by default. Do **not** bind it to `0.0.0.0` on a
Vast.ai box — it has no authentication of any kind. Tunnel instead:

```bash
# on the remote box
flightarb serve --port 8000
```

```bash
# on your laptop
ssh -N -L 8000:127.0.0.1:8000 <user>@<vast-host> -p <port>
```

Then open <http://127.0.0.1:8000> locally.

If you are on the iPad/tmux setup, run the server inside tmux so it survives a
dropped connection:

```bash
tmux new -s flightarb 'flightarb serve --port 8000'
```

---

## 4. Turn on multi-airline coverage

This is the single most valuable thing to do next, and it needs one package:

```bash
pip install -e ".[discovery]"
flightarb doctor          # fast_flights should now show as installed
flightarb search Casablanca Malaga --depart 2026-09-18 \
  --providers ryanair,fast-flights
```

To make it the default, edit `policy.toml`:

```toml
[providers]
enabled = ["ryanair", "fast-flights"]
```

### Status: verified in CI

The `discovery` job in `.github/workflows/ci.yml` installs the package, calls
the adapter and asserts it returns offers with sane prices, durations and
segment structure. It is green, against **fast-flights 3.1**, returning real
fares like:

```
MAD-AGP      2026-10-02 06:30->07:45 UX     EUR  95.00 stops=0 elapsed=75m
MAD-LIS-AGP  2026-10-02 22:25->22:55 TP     EUR  99.00 stops=1 elapsed=1470m
```

Two things to know:

- **It needs `fast-flights >= 3.1`.** Version 3 replaced `FlightData`/`Result`
  with `create_query`/`FlightQuery`/`ResultList`; the adapter targets 3.x only.
- **The extra also pins `typing_extensions`**, which fast-flights imports but
  forgets to declare. Without it the package fails to import at all, and the
  error message says "not installed" about the wrong package.

If that CI job ever goes red, the upstream shape changed again and `_to_offer()`
is where to fix it. `ryanair` is independent of all this.

---

## 5. What to hand the next session

If you continue this with a fresh agent, the useful context is:

- **Architecture and design rationale**: `README.md` — every non-obvious
  decision has a "why" next to it.
- **Every tunable**: `policy.toml`. Nothing about the ranking is hard-coded.
- **The contract**: `tests/test_acceptance.py` encodes the original brief as
  executable criteria. If a change breaks one of those, it broke the product,
  not just a test.
- **The premise, asserted against live data**: `tests/test_ryanair.py` — Ryanair
  serves RBA, does not serve CMN, and westbound legs across the Morocco–Spain
  timezone boundary report sane elapsed times.

### The highest-value next steps, in order

1. **Run both providers by default.** `enabled = ["ryanair", "fast-flights"]`
   in `policy.toml`. Google Flights covers essentially every airline including
   Royal Air Maroc, but under-reports low-cost carriers -- CMN-AGP shows from
   EUR 150 there while Ryanair's own API has RBA-AGP at EUR 61. You need both.
   Beyond that, `FlightProvider` is ~40 lines to implement; Transavia and Wizz
   have semi-public endpoints worth adding.
2. **Real rail data.** Ground transport is currently estimated from road
   distance. ONCF publishes Moroccan timetables; a `TRAIN` mode with real times
   would sharpen every Casablanca↔Rabat decision.
3. **Let the price memory earn its keep.** Run a nightly search on your usual
   routes (`flightarb search ... --quiet >/dev/null`) so `observations` fills up.
   After a few weeks `flightarb memory` and the deal-score line become
   genuinely informative instead of silent.
4. **Bag fees.** `[bag_fees.by_carrier]` in `policy.toml` is hand-written and
   will go stale. It decides close calls.

### Known-weak spots, honestly

- `providers/browser.py` is also unexecuted (needs Playwright) and parses a page
  structure that changes without warning. Treat it as a sketch.
- `providers/airline_direct.py` duplicates some of what `ryanair.py` now does
  better; it is the generic verification framework, kept for adding other
  carriers' direct APIs.
- The offline road estimator averages ~12% error. `--osrm http://localhost:5000`
  against a self-hosted OSRM container removes that entirely, and a Vast.ai box
  is a fine place to run one.

---

## 6. Optional: self-hosted OSRM for exact road times

```bash
mkdir -p osrm && cd osrm
wget https://download.geofabrik.de/africa/morocco-latest.osm.pbf
docker run -t -v "${PWD}:/data" ghcr.io/project-osrm/osrm-backend \
  osrm-extract -p /opt/car.lua /data/morocco-latest.osm.pbf
docker run -t -v "${PWD}:/data" ghcr.io/project-osrm/osrm-backend \
  osrm-partition /data/morocco-latest.osrm
docker run -t -v "${PWD}:/data" ghcr.io/project-osrm/osrm-backend \
  osrm-customize /data/morocco-latest.osrm
docker run -d -p 5000:5000 -v "${PWD}:/data" ghcr.io/project-osrm/osrm-backend \
  osrm-routed --algorithm mld /data/morocco-latest.osrm
```

```bash
flightarb search Casablanca Malaga --depart 2026-09-18 --osrm http://localhost:5000
```

One region at a time — extracting a whole continent needs a lot of RAM.
