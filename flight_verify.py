#!/usr/bin/env python3
"""
flight_verify.py — turn rows from flight_sweep's results CSV into real,
bookable flights: live price, flight numbers, the ACTUAL return flight for
round-trip / open-jaw fares, and a Google Flights link with those exact
flights selected (the page that lists booking options).

Uses the `faster-flights` fork, installed into its own folder so it does not
clash with `fast-flights` used by the sweep:

    python flight_verify.py --install
    python flight_verify.py results.csv --top 10 --max-hours 28 --out verified.csv

Options:
    --rows 3 7 12      verify specific row numbers (1 = first data row) instead of --top
    --kinds rt-fare    only rows of these kinds
    --exclude-airlines / --exclude-airports  same meaning as in flight_sweep
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime

FORK_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fork_lib")

DEFAULT_EXCLUDED_AIRLINES: set[str] = set()   # pass --exclude-airlines / --exclude-airports instead
DEFAULT_EXCLUDED_AIRPORTS: set[str] = set()


def install_fork():
    print(f"Installing faster-flights into {FORK_DIR} ...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "--upgrade", "--target", FORK_DIR,
                           "faster-flights", "typing_extensions"])
    print("Done.")


_FF = None


def load_fork():
    global _FF
    if _FF is not None:
        return _FF
    if not os.path.isdir(FORK_DIR):
        sys.exit(f"{FORK_DIR} not found. Run:  {sys.executable} {sys.argv[0]} --install")
    sys.path.insert(0, FORK_DIR)
    for m in [m for m in sys.modules if m.startswith("fast_flights")]:
        del sys.modules[m]
    import fast_flights as ff  # noqa: WPS433
    _FF = ff
    if not hasattr(ff, "select_flight"):
        sys.exit("The package in fork_lib is not faster-flights. Run --install again.")
    return ff


# ---------------------------------------------------------------------------

def _dt(sd) -> datetime:
    """SimpleDatetime -> datetime; Google omits trailing zero minutes, e.g. time=(21,)."""
    dt = list(sd.date or ()) + [1, 1, 1]
    tm = list(sd.time or ()) + [0, 0]
    return datetime(int(dt[0]), int(dt[1]), int(dt[2]), int(tm[0]), int(tm[1]))


def _fmt_dur(minutes: int) -> str:
    return f"{minutes // 60}h{minutes % 60:02d}"


def describe(flight) -> dict:
    """Route, flight numbers, times and total duration of a Flights object."""
    legs = flight.flights
    # Sum flight durations plus layovers (layover = same airport, so local times
    # are comparable). Never subtract cross-timezone local clock times.
    total = sum(int(l.duration or 0) for l in legs)
    for a, b in zip(legs, legs[1:]):
        gap = int((_dt(b.departure) - _dt(a.arrival)).total_seconds() // 60)
        total += gap + (24 * 60 if gap < 0 else 0)
    return {
        "route": "-".join([legs[0].from_airport.code] + [l.to_airport.code for l in legs]),
        "flights": ", ".join(f"{l.airline_code or ''}{l.flight_number or ''}".strip() for l in legs),
        "airlines": list(flight.airlines or []),
        "dep": _dt(legs[0].departure).strftime("%a %d %b %H:%M"),
        "arr": _dt(legs[-1].arrival).strftime("%a %d %b %H:%M"),
        "dep_dt": _dt(legs[0].departure).isoformat(),
        "total_minutes": total,
        "price": int(flight.price or 0),
    }


def matches(flight, ticket: dict) -> bool:
    try:
        d = describe(flight)
    except Exception:  # noqa: BLE001
        return False
    if d["route"] != ticket["route"]:
        return False
    want = datetime.fromisoformat(ticket["dep_dt"])
    have = datetime.fromisoformat(d["dep_dt"])
    return abs((want - have).total_seconds()) <= 15 * 60


def acceptable(d: dict, args, must_end_at: str | None) -> bool:
    if args.max_hours and d["total_minutes"] > args.max_hours * 60:
        return False
    if set(d["airlines"]) & args.excluded_airlines:
        return False
    if set(d["route"].split("-")[1:-1]) & args.excluded_airports:
        return False
    if must_end_at and not d["route"].endswith(must_end_at):
        return False
    return True


def search_with_fallback(ff, query, shop, args):
    """Shopping RPC (full price-sorted list) first; plain page search if it returns nothing."""
    err = None
    try:
        res = ff.get_flights(query, shopping=shop, proxy=args.proxy)
        if res:
            return res, f"rpc {len(res)} results"
    except Exception as e:  # noqa: BLE001
        err = f"rpc failed: {type(e).__name__}: {e}"[:80]
    time.sleep(args.delay)
    try:
        res = ff.get_flights(query, proxy=args.proxy)
        diag = getattr(res, "diagnostics", None)
        return res, f"page {len(res)} results" + (f", {diag.status}" if diag else "") + (f"; {err}" if err else "")
    except Exception as e:  # noqa: BLE001
        return None, f"search failed: {type(e).__name__}: {e}"[:120]


@dataclass
class Verified:
    row: int
    ticket: int
    trip: str
    status: str
    live_price: int = 0
    out_route: str = ""
    out_flights: str = ""
    out_dep: str = ""
    out_arr: str = ""
    ret_route: str = ""
    ret_flights: str = ""
    ret_dep: str = ""
    ret_arr: str = ""
    ret_total: str = ""
    booking_url: str = ""


def verify_ticket(ff, ticket: dict, args, row_no: int, tkt_no: int) -> Verified:
    trip = ticket["trip"]
    v = Verified(row=row_no, ticket=tkt_no, trip=trip, status="")
    legs = [ff.FlightQuery(date=ticket["dep"], from_airport=ticket["from"], to_airport=ticket["to"], max_stops=args.max_stops)]
    if trip != "one-way":
        legs.append(ff.FlightQuery(date=ticket["ret"], from_airport=ticket["to"], to_airport=ticket["ret_to"], max_stops=args.max_stops))
    query = ff.create_query(flights=legs, trip=trip, seat=args.seat, passengers=ff.Passengers(adults=args.adults),
                            currency=args.currency, language="en-GB")
    shop = ff.ShoppingOptions(ranking_mode="cheapest", result_sort="price")

    results, how = search_with_fallback(ff, query, shop, args)
    if results is None:
        v.status = how
        return v
    if not results:
        v.status = f"live search returned no flights at all ({how}) — Google blocked/throttled? try again later or --delay 5"
        return v

    match = next((f for f in results if matches(f, ticket)), None)
    if match is None:
        # closest candidate on the same route, for diagnosis
        descs = []
        for f in results:
            try:
                descs.append((describe(f), f))
            except Exception:  # noqa: BLE001
                continue
        same = [(abs((datetime.fromisoformat(dd["dep_dt"]) - datetime.fromisoformat(ticket["dep_dt"])).total_seconds()), f)
                for dd, f in descs if dd["route"] == ticket["route"]]
        if same:
            delta, f = min(same, key=lambda x: x[0])
            dd = describe(f)
            v.status = f"outbound not found at that time; nearest same-route flight departs {dd['dep']} ({int(delta // 60)} min off), {dd['price']} {args.currency} [{how}]"
        else:
            routes = sorted({dd["route"] for dd, _ in descs})[:6]
            v.status = f"outbound route not in {len(results)} live results [{how}]; routes seen: {', '.join(routes)}"
        return v
    d = describe(match)
    v.out_route, v.out_flights, v.out_dep, v.out_arr, v.live_price = d["route"], d["flights"], d["dep"], d["arr"], d["price"]

    if trip == "one-way":
        v.status = "ok (one-way; price refreshed)"
        v.booking_url = query.url()
        return v

    # Round trip / open-jaw: select the outbound and fetch real return options
    try:
        rq = ff.select_flight(query, match)
        rets = ff.get_return_flights(rq, shopping=shop, proxy=args.proxy)
        if not rets:
            time.sleep(args.delay)
            rets = ff.get_return_flights(rq, proxy=args.proxy)
    except Exception as e:  # noqa: BLE001
        v.status = f"outbound found; return lookup failed: {type(e).__name__}: {e}"[:120]
        v.booking_url = query.url()
        return v

    cands = []
    for f in rets:
        try:
            dd = describe(f)
        except Exception:  # noqa: BLE001
            continue
        if acceptable(dd, args, ticket.get("ret_to")):
            cands.append((dd, f))
    if not cands:
        v.status = f"outbound found but no return fits your limits ({len(rets)} returns offered)"
        v.booking_url = query.url()
        return v
    cands.sort(key=lambda x: (x[0]["price"] or 10**9, x[0]["total_minutes"]))
    dd, best = cands[0]
    v.ret_route, v.ret_flights, v.ret_dep, v.ret_arr, v.ret_total = dd["route"], dd["flights"], dd["dep"], dd["arr"], _fmt_dur(dd["total_minutes"])
    if dd["price"]:
        v.live_price = dd["price"]          # return-page prices are for the whole trip

    try:
        page = ff.get_selected_flight_page(rq, best, proxy=args.proxy)
        v.booking_url = page.url
        v.status = "ok (both flights selected; link opens booking options)"
    except Exception as e:  # noqa: BLE001
        try:
            v.booking_url = ff.build_selected_search_url(ff.select_flight(rq, best), currency=args.currency)
            v.status = "ok (return chosen; link opens selected search)"
        except Exception:  # noqa: BLE001
            v.booking_url = query.url()
            v.status = f"return chosen; could not build selected link ({type(e).__name__})"
    return v


# ---------------------------------------------------------------------------

def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("csv", nargs="?", help="results CSV from flight_sweep.py")
    p.add_argument("--install", action="store_true", help="install faster-flights into ./fork_lib and exit")
    p.add_argument("--top", type=int, default=10, help="verify the first N rows (CSV is already ranked)")
    p.add_argument("--rows", nargs="*", type=int, default=None, help="specific row numbers instead of --top")
    p.add_argument("--sort", default="csv", choices=["csv", "price", "time"],
                   help="which rows count as 'top': csv = the sweep's ranking (default), price = cheapest first, "
                        "time = shortest travel first")
    p.add_argument("--no-dedupe", action="store_true",
                   help="keep rows that repeat the same route on the same dates (default: verify each once)")
    p.add_argument("--kinds", nargs="*", default=None, help="only rows of these kinds")
    p.add_argument("--from", dest="origins", nargs="*", default=None, help="only rows departing from these airports, e.g. AMS")
    p.add_argument("--to", dest="dests", nargs="*", default=None, help="only rows arriving at these airports, e.g. BKK")
    p.add_argument("--back-to", nargs="*", default=None, help="only rows whose return lands at these airports")
    p.add_argument("--stay", nargs=2, type=int, metavar=("MIN", "MAX"), default=None,
                   help="only round-trip rows with a stay in this range of days, e.g. --stay 20 22")
    p.add_argument("--via", nargs="*", default=None, help="only rows connecting through any of these airports, e.g. IST")
    p.add_argument("--airlines", nargs="*", default=None, help="only rows flown by any of these airline codes, e.g. TK")
    p.add_argument("--max-hours", type=float, default=None)
    p.add_argument("--max-stops", type=int, default=2)
    p.add_argument("--exclude-airlines", nargs="*", default=None)
    p.add_argument("--exclude-airports", nargs="*", default=None)
    p.add_argument("--no-default-excludes", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--seat", default="economy")
    p.add_argument("--adults", type=int, default=1)
    p.add_argument("--currency", default="EUR")
    p.add_argument("--delay", type=float, default=2.0)
    p.add_argument("--proxy", default=None)
    p.add_argument("--out", default="verified.csv")
    a = p.parse_args(argv)
    if not a.install and not a.csv:
        p.error("give the results CSV, or --install")
    up = lambda xs: {c.upper() for c in xs}  # noqa: E731
    a.excluded_airlines = (set() if a.no_default_excludes else set(DEFAULT_EXCLUDED_AIRLINES)) | up(a.exclude_airlines or [])
    a.excluded_airports = (set() if a.no_default_excludes else set(DEFAULT_EXCLUDED_AIRPORTS)) | up(a.exclude_airports or [])
    return a


def main(argv=None):
    args = parse_args(argv)
    if args.install:
        install_fork()
        return
    ff = load_fork()

    with open(args.csv, encoding="utf-8-sig", newline="") as fh:
        head = fh.readline()
        fh.seek(0)
        delim = ";" if head.count(";") > head.count(",") else ("\t" if head.count("\t") > head.count(",") else ",")
        rows = list(csv.DictReader(fh, delimiter=delim))
    cols = list(rows[0].keys()) if rows else []
    if "tickets_json" not in cols:
        print(f"Columns found ({len(cols)}, delimiter {delim!r}): {', '.join(cols)[:300]}")
        if "url_1" in cols:
            sys.exit("No tickets_json column: the flight_sweep.py that wrote this CSV predates the verifier. "
                     "Download the latest flight_sweep.py, rerun the sweep command (cache only, no fetching) and retry.")
        sys.exit("This does not look like a flight_sweep results CSV (was it re-saved by Excel with a different layout?).")
    if delim != ",":
        print(f"Note: CSV was {delim!r}-separated (re-saved by Excel?) — parsed anyway.")

    indexed = list(enumerate(rows, 1))
    if args.sort == "price":
        indexed.sort(key=lambda x: (float(x[1].get("price") or 0), float(x[1].get("out_total_hours") or 0)))
    elif args.sort == "time":
        indexed.sort(key=lambda x: (float(x[1].get("out_total_hours") or 0) + float(x[1].get("ret_total_hours") or 0),
                                    float(x[1].get("price") or 0)))
    picked, seen = [], set()
    for i, r in indexed:
        if args.rows and i not in args.rows:
            continue
        if not args.no_dedupe:
            k = (r.get("route"), r.get("ret_route"), r.get("dep_date"), r.get("ret_date"), r.get("airlines"))
            if k in seen:
                continue
            seen.add(k)
        if args.kinds and r["kind"] not in args.kinds:
            continue
        if args.origins and r.get("origin", "").upper() not in {x.upper() for x in args.origins}:
            continue
        if args.dests and r.get("dest", "").upper() not in {x.upper() for x in args.dests}:
            continue
        if args.back_to and (r.get("ret_to") or r.get("origin", "")).upper() not in {x.upper() for x in args.back_to}:
            continue
        if args.stay and not (args.stay[0] <= int(r.get("stay_days") or 0) <= args.stay[1]):
            continue
        if args.via:
            hops = set(r.get("route", "").split("-")[1:-1]) | set((r.get("ret_route") or "").split("-")[1:-1])
            if not hops & {x.upper() for x in args.via}:
                continue
        if args.airlines and not set(r.get("airlines", "").split()) & {x.upper() for x in args.airlines}:
            continue
        picked.append((i, r))
        if not args.rows and len(picked) >= args.top:
            break

    if not picked:
        sys.exit("No rows matched your filters.")
    print(f"Verifying {len(picked)} rows from {args.csv}")
    out: list[Verified] = []

    def save():
        for attempt in range(3):
            try:
                _write(args.out)
                return
            except PermissionError:
                time.sleep(2)             # file open in Excel? give it a moment
        alt = args.out.rsplit(".", 1)[0] + "_" + datetime.now().strftime("%H%M%S") + ".csv"
        _write(alt)
        print(f"  ({args.out} is locked — open in Excel? — writing to {alt} instead)")
        args.out = alt

    def _write(path):
        with open(path, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["row", "ticket", "trip", "status", "live_price", "out_route", "out_flights", "out_dep", "out_arr",
                        "ret_route", "ret_flights", "ret_dep", "ret_arr", "ret_total", "booking_url"])
            for v in out:
                w.writerow([v.row, v.ticket, v.trip, v.status, v.live_price, v.out_route, v.out_flights, v.out_dep, v.out_arr,
                            v.ret_route, v.ret_flights, v.ret_dep, v.ret_arr, v.ret_total, v.booking_url])

    for i, r in picked:
        tickets = json.loads(r["tickets_json"] or "[]")
        print(f"\nRow {i}: {r['kind']} {r['price']} {r['currency']} {r['dep_date']}"
              + (f" → {r['ret_date']}" if r.get("ret_date") else "") + f" {r['route']}")
        total_live = 0
        for t_no, t in enumerate(tickets, 1):
            time.sleep(args.delay)
            try:
                v = verify_ticket(ff, t, args, i, t_no)
            except Exception as e:  # noqa: BLE001 — never let one row kill the run
                v = Verified(row=i, ticket=t_no, trip=t["trip"], status=f"error: {type(e).__name__}: {e}"[:120])
            out.append(v)
            save()
            total_live += v.live_price
            print(f"  ticket {t_no} ({t['trip']} {t['from']}→{t['to']}): {v.status}")
            if v.out_flights:
                print(f"     out: {v.out_route} {v.out_flights}  {v.out_dep} → {v.out_arr}")
            if v.ret_flights:
                print(f"     ret: {v.ret_route} {v.ret_flights}  {v.ret_dep} → {v.ret_arr} ({v.ret_total})")
            if v.live_price:
                print(f"     live price: {v.live_price} {args.currency}")
            if v.booking_url:
                print(f"     {v.booking_url}")
        if len(tickets) > 1:
            failed = [v for v in out[-len(tickets):] if not v.status.startswith("ok")]
            if failed:
                print(f"  incomplete: {len(failed)} of {len(tickets)} tickets could not be verified — no total")
            else:
                print(f"  total live price: {total_live} {args.currency} (was {r['price']})")

    save()
    print(f"\nWrote {len(out)} ticket verifications to {args.out}")


if __name__ == "__main__":
    main()
