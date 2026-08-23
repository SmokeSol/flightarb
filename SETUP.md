# Running it entirely on GitHub

No server, no VPS, nothing installed on your machine, nothing you pay for.

```
GitHub Actions   the engine    cron + on-demand, runs the searches
GitHub Pages     the front end static dashboard reading the results
the repository   the memory    price history, committed, compounds daily
```

**GitHub Pages cannot run Python** — it serves static files only. So the engine
runs in Actions and publishes JSON; Pages just displays it. That constraint
turns out to suit the product: a standing watch that tells you when a route gets
cheap is more useful than a search box you have to remember to visit.

---

## 1. It is already set up

| | |
|---|---|
| Dashboard | <https://smokesol.github.io/flightarb/> |
| Schedule | daily at 06:15 UTC |
| On demand | Actions → **watch** → *Run workflow* |
| What it watches | [`watchlist.toml`](watchlist.toml) |
| Price history | `history/observations.jsonl`, committed each run |

### ⚠️ The Pages site is public

A GitHub Pages site is readable by anyone with the URL **even though this
repository is private**. Your watchlist and results — where you are going and
when — are visible to anyone who finds it.

If that is not what you want, disable it in **Settings → Pages → Source: None**.
The workflow keeps running and the results stay in the repo as JSON; you just
lose the hosted dashboard.

---

## 2. Change what is watched

Edit [`watchlist.toml`](watchlist.toml) and push. Dates may be absolute
(`2026-09-18`) or relative (`+26d`) — use relative for a standing watch, or a
fixed date silently drifts into the past and the watch quietly dies.

```toml
[[trip]]
name = "Casablanca -> Malaga, family"
origin = "Casablanca"
destination = "Malaga"
depart = "+26d"
return = "+30d"
adults = 2
children = 2
checked_bags = 1
flex = 3
max_origin_minutes = 240   # far enough to reach Rabat and Marrakesh
```

`value_of_time` can be set per trip, which is how the same route gives a
different answer for a backpacker and for a family of four.

## 3. Search for anything

There is a search box on the dashboard. Type a trip, hit Search, and the page
asks GitHub Actions to run the engine, follows the run, and shows the result
when it lands — roughly two minutes end to end. A static page cannot price a
flight itself; this is the closest thing to a search box that exists without a
server.

The first search asks for a token, once. Create a **fine-grained personal access
token**, scoped to **only this repository**, with **Actions: Read and write** and
nothing else, and give it a short expiry. It is kept in your browser's
localStorage, is never committed, and is only ever sent to github.com. Anyone
holding it could run workflows in this one repo — which is exactly why it is
scoped to this one repo.

No token, or prefer not to use one? Actions → **watch** → *Run workflow* does
the same thing through GitHub's own UI. Either way the ad-hoc entry is never
written back into `watchlist.toml`.

## 4. Change the economics

Everything the ranking trades off is in [`policy.toml`](policy.toml) — value of
time, how far you will drive to another airport, what a checked bag costs on
each carrier, how much a saving must be before a self-transfer is worth it.
Push a change and the next run uses it.

---

## Local use, if you want it

```bash
git clone https://github.com/SmokeSol/flightarb.git && cd flightarb
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,discovery]"
flightarb data --refresh
flightarb doctor
```

```bash
flightarb serve                    # interactive web UI on localhost:8000
flightarb watch --site site        # build the dashboard locally
flightarb search Casablanca Malaga --depart 2026-09-18 --return 2026-09-22 \
  --adults 2 --children 2 --checked-bags 1 --flex 3 --max-origin-minutes 240
```

`flightarb doctor` is the bring-up check: Python version, datasets, carrier API
reachability, optional packages, usable providers. Fix anything marked `FAIL`.

---

## Providers

Run **both**. They are complementary, and the numbers show why:

```
CMN-AGP  via Google Flights   Iberia  from EUR 150
RBA-AGP  via Ryanair direct   Ryanair from EUR  61
```

Aggregators have breadth but under-report low-cost carriers. The carrier's own
API has the cheap fare but only its own flights. Neither alone finds the answer.

| Adapter | Prices | Needs |
|---|---|---|
| `ryanair` | Real, **verified at carrier** | nothing |
| `fast-flights` | Real, unverified, ~every airline | `pip install -e ".[discovery]"` |
| `browser` | Real, unverified | playwright — **never executed, treat as a sketch** |
| `synthetic` | **Simulated** | nothing; for tests and offline demos only |

Google Flights throttles datacenter IPs, so `fast-flights` sometimes returns
nothing from Actions. `ryanair` calls the airline directly and is unaffected —
another reason to run both.

---

## Handing this to a fresh session

- **Design rationale**: `README.md`. Every non-obvious decision has its "why".
- **Every tunable**: `policy.toml`. Nothing about the ranking is hard-coded.
- **The contract**: `tests/test_acceptance.py` encodes the original brief as
  executable criteria. Break one and you broke the product, not just a test.
- **The premise, against live data**: `tests/test_ryanair.py` — Ryanair serves
  RBA, does not serve CMN, and westbound legs across the Morocco–Spain timezone
  boundary report sane elapsed times.

### Highest-value next steps

1. **Let the history accumulate.** The daily cron is already running. After two
   or three weeks the dashboard's "cheapest we have seen" badge becomes a real
   signal instead of silence. Nothing to build.
2. **Alerting.** The history file makes this easy: when a watched trip drops
   below its 10th percentile, open a GitHub issue from the workflow. You get an
   email for free, no notification service.
3. **More carriers.** `FlightProvider` is ~40 lines. Transavia and Wizz have
   semi-public endpoints; each one added is another blind spot removed.
4. **Bag fees.** `[bag_fees.by_carrier]` is hand-written and will go stale. It
   decides close calls.
5. **Exact road times.** The offline estimator averages ~12% error. A hosted
   OSRM instance and `ground.router = "osrm"` removes that.

### Known-weak spots, honestly

- `providers/browser.py` has never been executed and parses a page structure
  that changes without warning.
- `providers/airline_direct.py` overlaps with `ryanair.py`, which does the job
  better. It is the generic verification framework, kept for adding other
  carriers' direct APIs.
- Public transport times and costs are modelled from road distance, not routed.
  There is no free worldwide rail API.
