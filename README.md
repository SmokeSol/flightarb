# flightarb

**An autonomous trip-arbitrage engine.** Not a flight finder.

Google Flights is already good at the obvious problem. What it will not do is
independently investigate a few hundred counterfactual journeys, price each one
door to door for *your* circumstances, and hand you the ones that matter with an
explanation of why the rest lost.

**Real prices. No API key, no account, no paid tier, nothing to install** — the
whole thing runs on the Python standard library.

---

## The case it was built for

Search "Casablanca → Málaga" anywhere and you get `CMN → AGP`. Here is the thing
every one of those searches hides from you, confirmed against live carrier data:

```
CMN  (Casablanca)   Ryanair routes:  none
RBA  (Rabat)        Ryanair routes:  22, including AGP
RAK  (Marrakesh)    Ryanair routes:  57, including AGP
```

Ryanair does not serve Casablanca **at all**. It serves Rabat, an hour up the
motorway. Anyone searching from Casablanca is structurally blind to the cheap
fare — not because it's hidden, but because they never thought to search from a
different city.

Real output, family of four, one checked bag, ±3 days:

```
 BEST VALUE + CHEAPEST + EASIEST
   €308 all-in · 12h21 door to door
   RBA>AGP / AGP>RBA · return · verified at carrier

     OUT   2026-09-18
           ↓ Casablanca → RBA (Rabat) · 96 min · €27 by car
           21:45 RBA → 23:50 AGP · FR · 1h05
           ↓ AGP (Málaga) → Málaga · 24 min · €6
     BACK  2026-09-22
           15:40 AGP → 14:45 RBA · FR · 1h05

     Fare              €171.92      (4 × €42.98 return)
     Bags               €70.00
     Ground             €66.37
     Cash total        €308.29

 INVESTIGATED AND REJECTED
   RBA>AGP / AGP>RAK · fare €156
     fare is €16 cheaper, but real cost is €217 worse once travel time is counted
```

That last section is the one that earns trust. It priced flying home into
Marrakesh instead, and tells you why it said no.

---

## Install and run

```bash
git clone <this repo> && cd flightarb
pip install -e .
flightarb data --refresh
```

`data --refresh` downloads two free, no-key datasets (~21 MB, once):

| Dataset | Licence | What it gives us |
|---|---|---|
| [OurAirports](https://ourairports.com/data/) | Public domain | Every airport on earth, rebuilt nightly |
| [GeoNames cities15000](https://download.geonames.org/export/dump/) | CC-BY | Cities with population and alternate spellings |

**Web UI** — stdlib `http.server`, no framework:

```bash
flightarb serve
```

Then open <http://127.0.0.1:8000>. The search streams its progress line by line
as the planner works, so you watch it think rather than staring at a spinner.

**Command line:**

```bash
flightarb search Casablanca Malaga --depart 2026-09-18 --return 2026-09-22 --adults 2 --children 2 --checked-bags 1 --flex 3 --max-origin-minutes 240
```

```bash
flightarb providers     # which adapters are usable right now
flightarb memory        # what the price memory has learned
```

Useful flags: `--value-of-time` (€/hour — the single number that decides
"cheaper but slower"), `--max-origin-minutes`, `--budget` (query cap),
`--osrm URL` (real road routing), `--json`, `--html`, `-v` (search trace).

---

## How it works

```
                    USER INTENT
                         v
                 Journey Specification
              (a goal, not an airport pair)
                         v
        Airport Resolver  +  Preference Engine
      OurAirports + GeoNames + road routing
                         v
                  Search Planner
              adaptive / best-first
                         v
       ryanair | fast-flights | browser | synthetic
                         v
                   Journey Graph
                         v
                    Cost Engine
       fare + bags + ground + hotel + time + risk
                         v
                  Pareto Optimizer
                         v
             CHEAPEST   BEST VALUE   EASIEST
```

### 1. Reachability is measured in minutes, not kilometres

100 km of Moroccan motorway is 55 minutes. 100 km of mountain road is two hours.
Every candidate airport is scored by road time and cost.

Two backends: an **offline estimator** (no network, calibrated against OSRM on
real city→airport routes to ~12% mean error with no bias) and **OSRM** for the
real number. Point `--osrm` at your own container; the engine refuses to hammer
the public demo server by accident.

### 2. The planner is a search agent, not a `for` loop

The naive approach is `4 origins × 5 destinations × 7 dates × 7 return dates =
980 searches`. Instead the space is treated as independent dimensions (origin,
destination, outbound shift, return shift, ticketing mode) and the planner
**learns online what each dimension is worth.** Probe Rabat once, find it better,
and every unexplored configuration containing Rabat is predicted good and visited
early. Dimensions that don't matter stop being explored.

It stops on whichever comes first — query budget, wall clock, patience, or an
exhausted space — and always tells you which.

### 3. Outbound and return are priced independently

Consumers think `CMN ↔ AGP`. The engine doesn't. It prices every airport pair
once in both directions, then recombines, so it can fly you out of one city and
back into another — which no round-trip search will ever show you, because no
round-trip search is willing to land you somewhere else. Round trips sold as one
ticket compete against two-one-way combinations on equal terms.

### 4. It ranks on generalised journey cost

```
JourneyCost = Fare + Baggage + Ground + Hotel + Fees
            + TimeCost + RiskPenalty + ConfidencePenalty
```

Ranking on airfare is the mistake every consumer tool makes. A €180 saving isn't
a saving if reaching the airport costs €100 and three hours, and the "cheapest"
fare that excludes the bag you're definitely bringing isn't the cheapest fare.

Cash and decision score are always reported separately. Cash is what leaves your
bank account; the decision score is cash plus monetised time and risk, and is
never presented as money owed.

### 5. Hard constraints and soft economics are separate

`violations()` decides whether a journey is *allowed*. `evaluate()` decides what
it's *worth*. Keeping them apart is what lets the engine say "I found something
cheaper and rejected it, here's why" instead of silently dropping it.

On top of the continuous ranking sits a second, human hurdle: an alternative must
clear `min_saving_per_extra_hour`, `min_saving_for_self_transfer` and friends
before it's recommended *over the route you actually asked for*.

### 6. It builds itineraries nobody sells

Self-transfer synthesis stitches `A → X` and `X → B` from separate tickets into
one journey, then prices what makes it dangerous: the connection isn't protected.
Miss it and no one owes you a seat. Overnight pairings are built deliberately *so
they can be rejected out loud* with the hotel night counted.

Hidden-city ticketing is **not** implemented. It breaks return legs, forbids
checked bags, and can get accounts closed — a different category of risk, and not
something to default anyone into.

### 7. The price memory compounds

Every search records what it saw. After a few weeks:

> €112 is unusually good for RBA-AGP: median €168 across 47 comparable departures
> we have observed — roughly the 8th percentile.

That's a statement about *your* observed market, not a language model guessing
about airfares. Simulated prices are never written to it.

---

## Providers

| Adapter | Role | Prices | Needs |
|---|---|---|---|
| **`ryanair`** *(default)* | Real fares, direct from the carrier | Real, **verified** | nothing |
| `fast-flights` | Broad multi-airline discovery | Real, unverified | `pip install -e ".[discovery]"` |
| `browser` | Fallback when discovery is empty | Real, unverified | `pip install playwright` |
| `synthetic` | Deterministic market simulator | **Simulated** | nothing |

```bash
flightarb search Casablanca Malaga --depart 2026-09-18 --providers ryanair,fast-flights
```

**On `ryanair`.** It calls the unauthenticated fare API the airline's own site
uses — no key, no scraping, no package. Its `cheapestPerDay` endpoint returns a
whole month of daily prices in one request, so a ±3-day flexible search costs
*one* HTTP call per airport pair rather than one per date.

**On `fast-flights`.** Targets the 3.x model, which hands over real per-segment
records — airport codes, local clocks, per-leg durations — and prices quoted
directly in EUR. So layovers are computed between two clocks at the *same*
airport, which is correct across timezones by construction, and the traveller's
bag counts are passed into the query rather than modelled. Verified by CI on
every push.

**On `synthetic`.** A deterministic fake market, built from real airport geography
so the engine can be tested and demonstrated with no network at all. Its prices
are stamped `SIMULATED` everywhere they surface and are never written to the price
memory. It's a physics engine for the ranker, not a source of bookable fares.

**On scraping.** Every network adapter gets a token-bucket rate limiter, a circuit
breaker, and a TTL cache. The browser adapter is an *escalation*, not a peer: it
only runs when cheaper adapters came back empty. If a site presents a consent wall
or a bot challenge, that adapter is marked unavailable and the search continues.
Nothing here attempts to solve a CAPTCHA or bypass an access control.

---

## Making it yours

Everything the engine trades off lives in [`policy.toml`](policy.toml), so the
ranking is auditable rather than a black box. Two people asking "Casablanca →
Málaga" *should* get different answers:

```toml
[traveler]
value_of_time_eur_hour = 25.0   # the number that decides cheaper-but-slower
group_time_discount = 0.5       # 4 people = 2.5x, not 4x

[airports]
max_origin_reposition_minutes = 120

[economics]
min_saving_per_extra_hour = 40.0
min_saving_for_self_transfer = 100.0
overnight_hotel_eur = 90.0

[bag_fees.by_carrier]
FR = 35.0    # Ryanair
AT = 0.0     # Royal Air Maroc includes a checked bag
```

---

## HTTP API

The web UI is stdlib. For a JSON API with OpenAPI docs:

```bash
pip install "flightarb[api]"
uvicorn flightarb.api:app --reload
```

## Tests

```bash
python -m pytest -q                    # 60 tests
python -m pytest -q -m "not network"   # 54, fully offline
```

[`tests/test_acceptance.py`](tests/test_acceptance.py) encodes the brief as
executable criteria: the engine *must* independently consider CMN and RBA, *must*
consider an alternative destination, *must* compare mixed outbound/return
airports, flexible dates and separate one-way pricing, *must* cost fare + bags +
ground + door-to-door time + self-transfer risk, and *must* return CHEAPEST /
BEST VALUE / EASIEST explained against `CMN → AGP`. Those run against the
deterministic simulator, so a failure means the engine changed — not that a fare
moved.

[`tests/test_ryanair.py`](tests/test_ryanair.py) asserts the premise against live
data: Ryanair serves RBA, does not serve CMN, and a westbound leg across the
Morocco–Spain timezone boundary reports a sane elapsed time.

---

## Honest limitations

**The zero-install default is one airline.** `ryanair` finds Ryanair fares and
nothing else. On the Morocco–Spain corridor that happens to be exactly the
carrier that creates the arbitrage — but Royal Air Maroc, Vueling and Transavia
are invisible to it. Add multi-airline coverage with one package:

```bash
pip install -e ".[discovery]"
flightarb search Casablanca Malaga --depart 2026-09-18 --providers ryanair,fast-flights
```

**`cheapestPerDay` gives the cheapest departure per day**, not every departure.
Fine for deciding *where and when* to fly; check the airline site for the exact
flight you want.

**Fares are lead-in prices per adult.** Children pay the adult fare on Ryanair;
seats, priority and infants are extra and are not modelled.

**Bag fees come from a hand-written table** in `policy.toml`, because carriers
don't expose them in fare feeds. They go stale, and since bag fees regularly
decide which flight is genuinely cheapest, a wrong number means a wrong winner.

**Ground transport is modelled, not booked.** Road times average ~12% error.
Public transport is estimated from distance — there's no free worldwide rail API —
so force a mode if your corridor has no service.

**Metro population is a proxy for hub status**, used because no free traffic
dataset exists. Right about Casablanca vs Rabat and Madrid vs Beauvais; wrong
somewhere.

## Licence

MIT. Airport data is public domain (OurAirports); city data is CC-BY (GeoNames)
and requires attribution if you redistribute it.
