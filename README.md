# flight_scanner

Search Google Flights across many dates, several departure airports and
constructed itineraries, rank the results by price *and* travel time, then
turn the best rows into confirmed flights with booking links.

Two scripts:

- `flight_sweep.py` — sweeps a date range × origins × destinations, builds
  self-transfer and two-round-trip combinations through hubs you choose,
  filters by total travel time and by airlines/airports you want to avoid,
  and writes a ranked CSV.
- `flight_verify.py` — takes rows from that CSV, re-searches them live,
  finds the actual return flight for round-trip fares, refreshes the price
  and produces a Google Flights link with the exact flights selected.

## Why

Google Flights shows one origin, one date pair and one price. Three things
it will not do for you:

1. **Origin arbitrage.** Airlines price by where the ticket starts, not
   where the long flight does. In two real searches from the Netherlands to
   Southeast Asia and to South America, the same long-haul flights were
   €150–200 cheaper per person when the ticket started in Paris or Madrid —
   a €40 train or low-cost hop away. For the first of those trips, an
   evening of searching Google Flights by hand from Amsterdam had produced
   nothing under about €900; the sweep found verified fares from €661.
   This tool sweeps all your candidate origins at once, so you see the gap.
2. **Stay ranges.** "Leave between 17 April and 15 May, come back 20–30 days
   later" is one command, not two hundred searches.
3. **Verified answers.** Google's list prices are "from" prices for a
   handful of seats. The verifier tells you which rows are still real, what
   the return flight actually is, and hands you a booking link.

It also builds two-ticket itineraries (cheap feeder + long-haul, or a
long-haul return to a hub + a separate hop) with a realistic self-transfer
buffer — and will happily show you that they lose, which on most long-haul
routes they do.

## Install

Python 3.10 or newer.

```
pip install -r requirements.txt
python flight_verify.py --install      # installs the verifier's fork into ./fork_lib
```

The two scripts use two different libraries that share a module name, which
is why the verifier keeps its own copy in a folder.

## Use

Sweep (Windows: keep it on one line):

```
python flight_sweep.py --from AMS BRU CDG --to BKK \
    --start 2027-10-01 --end 2027-10-31 --step 2 \
    --return-min-days 20 --return-max-days 30 \
    --max-hours 28 --checked-bags 1 \
    --cache cache.json --csv results.csv
```

Add constructed itineraries and single-ticket open-jaws:

```
    --self-transfer WAW HKG BKK --rt-hubs HKG BKK --open-jaw-fares
```

Exclude airlines or connecting airports (IATA codes):

```
    --exclude-airlines QR EK EY TK --exclude-airports DOH DXB AUH IST
```

Verify the best rows:

```
python flight_verify.py results.csv --from CDG --sort price --top 15 --max-hours 28 --out verified.csv
```

Useful verifier filters: `--from`, `--to`, `--back-to`, `--via`, `--airlines`,
`--stay MIN MAX`, `--kinds`, `--rows`, `--adults N` (prices for the real headcount).

`--cache` makes runs resumable (Ctrl-C and rerun) and lets you re-rank with
different filters without refetching. Entries older than `--max-cache-age-days`
(default 14) are refetched.

## How to read the output

- `kind` — `rt-fare` (one round-trip ticket), `open-jaw-fare` (one ticket,
  different return airport), `two-one-ways`, `two-round-trips` (long-haul
  return to a hub + separate hop return), `one-way`.
- `score` — price + hours × `--hour-value` (default €15/h) + a penalty per
  ticket change (default €60). `--sort price` or `--sort time` to ignore it.
- Durations are door to door including layovers, computed from flight
  durations so time zones do not distort them.
- Round-trip fares list only the outbound; the verifier finds the return.

## Caveats

- This uses Google Flights' internal endpoint through unofficial libraries
  (`fast-flights` and the `faster-flights` fork). It can stop working when
  Google changes something, and heavy use gets throttled: keep `--workers`
  low, raise `--delay`, or use `--proxy`. Use it for your own trips.
- Google does not carry every airline's fares completely (some low-cost
  carriers, consolidator prices). Check a winning low-cost hop on the
  airline's site.
- Nothing is held. A verified price is live at that moment and the booking
  link's session lasts about a day.
- Self-transfer itineraries are separate tickets: if the first flight is
  late, nobody protects the second. The default 3-hour minimum buffer is a
  floor, not a recommendation.

## Licence

MIT.
