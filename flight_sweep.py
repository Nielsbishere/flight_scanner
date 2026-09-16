#!/usr/bin/env python3
"""
flight_sweep.py — sweep a date range on Google Flights, build self-transfer and
two-round-trip combinations through chosen hubs, and rank everything by price
and total travel time (incl. layovers), with airline / airport blocklists.

Data source: Google Flights' internal endpoint via the `fast-flights` package.
    pip install fast-flights typing_extensions

What gets searched
  * Google one-way fares and round-trip fares (same airport both ways)
  * --open-jaw-fares   single-ticket "out of BRU, back into AMS" fares (multi-city query)
  * --self-transfer    two one-way tickets glued at a hub (origin->hub + hub->dest)
  * --rt-hubs          a round-trip long-haul origin<->hub PLUS a round-trip hop
                       hub<->dest (worth checking on long-haul routes with cheap hops)
  * in round-trip mode, any outbound one-way (incl. combos) paired with any
    return one-way, open-jaw allowed across the listed origins
"""

from __future__ import annotations

import argparse
import csv
import itertools
import json
import os
import random
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta

if sys.version_info < (3, 10):
    sys.exit(f"This script needs Python 3.10+, you are running {sys.version.split()[0]} ({sys.executable})")
try:
    from fast_flights import FlightQuery, Passengers, create_query
    from fast_flights.model import Airport, CarbonEmission, Flights, SimpleDatetime, SingleFlight
    from fast_flights.parser import _parse_time
    from primp import Client
    from selectolax.lexbor import LexborHTMLParser
except ImportError as e:
    sys.exit(f"Could not import a dependency ({e}).\n"
             f"Install into THIS interpreter with:\n    {sys.executable} -m pip install fast-flights typing_extensions")

# ---------------------------------------------------------------------------
# Defaults — edit to taste. IATA codes.
# ---------------------------------------------------------------------------

# Airlines / connecting airports excluded by default. Empty on purpose: pass
# --exclude-airlines / --exclude-airports (IATA codes) for your own preferences,
# e.g.  --exclude-airlines QR EK EY TK --exclude-airports DOH DXB AUH IST
DEFAULT_EXCLUDED_AIRLINES: set[str] = set()
DEFAULT_EXCLUDED_AIRPORTS: set[str] = set()
DEFAULT_MIN_SELF_TRANSFER_H = 3.0     # collect bags, re-check, security, delay buffer
DEFAULT_MAX_SELF_TRANSFER_H = 20.0

GOOGLE_URL = "https://www.google.com/travel/flights"
# EU cookie-consent cookies; without them EU IPs get a consent page with no data.
CONSENT_COOKIES = {"SOCS": "CAISNQgDEitib3FfaWRlbnRpdHlmcm9udGVuZHVpc2VydmVyXzIwMjQwMjI3LjA2X3AwGgJlbiACGgYIgLzXrgY",
                   "CONSENT": "PENDING+987"}


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Itinerary:
    dep_date: str
    origin: str
    dest: str
    price: int                  # total for everything on this row
    currency: str
    airlines: list[str]
    route: str                  # AMS-HEL-BKK
    stops: int
    flight_minutes: int
    layover_minutes: int        # incl. self-transfer waits
    total_minutes: int          # door to door, outbound
    dep_dt: str                 # ISO local time
    arr_dt: str
    layovers: str               # "HEL 2h15, BKK 4h05*"  (* = change of ticket)
    tickets: int = 1
    self_transfer_at: str = ""
    google_url: str = ""
    ret_url: str = ""
    ret_date: str = ""
    ret_to: str = ""            # airport the return lands at
    stay_days: int = 0
    ret_route: str = ""         # "" when only the fare's URL knows
    ret_total_minutes: int = 0  # 0 = unknown
    kind: str = "one-way"       # one-way | rt-fare | open-jaw-fare | two-one-ways | two-round-trips
    note: str = ""
    tickets_json: str = ""      # machine-readable ticket list for flight_verify.py

    @property
    def dep(self) -> datetime:
        return datetime.fromisoformat(self.dep_dt)

    @property
    def arr(self) -> datetime:
        return datetime.fromisoformat(self.arr_dt)


def _dt(sd) -> datetime:
    (y, m, d), (hh, mm) = sd.date, sd.time
    return datetime(y, m, d, hh, mm)


def _fmt_dur(minutes: int) -> str:
    return f"{minutes // 60}h{minutes % 60:02d}" if minutes else "-"


def _fmt_dt(iso: str) -> str:
    return datetime.fromisoformat(iso).strftime("%a %d %b %H:%M") if iso else ""


def _gap(a_arr: str, b_dep: str) -> int:
    """Minutes between landing (a) and next departure (b) at the same airport."""
    return int((datetime.fromisoformat(b_dep) - datetime.fromisoformat(a_arr)).total_seconds() // 60)


def _join_route(a: str, b: str) -> str:
    return a + "-" + b.split("-", 1)[1]


def parse_itinerary(f, dep_date: str, origin: str, dest: str, currency: str) -> Itinerary | None:
    legs = f.flights
    if not legs:
        return None
    flight_minutes = sum(int(l.duration or 0) for l in legs)
    layover_minutes, desc = 0, []
    for a, b in zip(legs, legs[1:]):
        gap = int((_dt(b.departure) - _dt(a.arrival)).total_seconds() // 60)
        if gap < 0:
            gap += 24 * 60
        layover_minutes += gap
        desc.append(f"{a.to_airport.code} {_fmt_dur(gap)}")
    return Itinerary(
        dep_date=dep_date, origin=origin, dest=dest, price=int(f.price or 0), currency=currency,
        airlines=list(f.airlines or []),
        route="-".join([legs[0].from_airport.code] + [l.to_airport.code for l in legs]),
        stops=len(legs) - 1, flight_minutes=flight_minutes, layover_minutes=layover_minutes,
        total_minutes=flight_minutes + layover_minutes,
        dep_dt=_dt(legs[0].departure).isoformat(), arr_dt=_dt(legs[-1].arrival).isoformat(),
        layovers=", ".join(desc) or "nonstop",
        tickets_json=json.dumps([{"trip": "one-way", "from": origin, "to": dest, "dep": dep_date,
                                  "dep_dt": _dt(legs[0].departure).isoformat(),
                                  "route": "-".join([legs[0].from_airport.code] + [l.to_airport.code for l in legs]),
                                  "airlines": list(f.airlines or []), "price": int(f.price or 0)}]),
    )


# ---------------------------------------------------------------------------
# Fetching and tolerant parsing
# ---------------------------------------------------------------------------

_debug_dumps = 0


def _dump(name: str, text: str):
    global _debug_dumps
    if _debug_dumps >= 5:
        return
    _debug_dumps += 1
    try:
        with open("".join(c if c.isalnum() or c in "-_." else "_" for c in name), "w", encoding="utf-8") as fh:
            fh.write(text)
    except OSError:
        pass


def fetch_html(query, args) -> str:
    client = Client(impersonate="chrome_145", impersonate_os="windows", referer=True,
                    proxy=args.proxy, cookie_store=True, cookies=CONSENT_COOKIES, timeout=30)
    params = query.params()
    params.setdefault("gl", "US")
    res = client.get(GOOGLE_URL, params=params)
    html = res.text
    if "ds:1" not in html:
        why = "consent page" if "consent" in html[:5000].lower() else f"HTTP {res.status_code}"
        _dump(f"debug_response_{_debug_dumps + 1}.html", f"<!-- {res.status_code} {res.url} -->\n{html}")
        raise RuntimeError(f"no flight data in Google response ({why})")
    return html


def _g(x, *idx, default=None):
    for i in idx:
        try:
            x = x[i]
        except (IndexError, TypeError, KeyError):
            return default
        if x is None:
            return default
    return x


class ParsedPage:
    def __init__(self):
        self.flights: list = []
        self.airline_names: dict[str, str] = {}
        self.skipped = 0


def parse_page(html: str, label: str) -> ParsedPage:
    node = LexborHTMLParser(html).css_first(r"script.ds\:1")
    if node is None:
        raise RuntimeError("no ds:1 data script in page")
    data = node.text().split("data:", 1)[1].rsplit(",", 1)[0]
    page = ParsedPage()
    if data.endswith("errorHasStatus: true"):
        return page
    try:
        payload = json.loads(data)
    except json.JSONDecodeError as e:
        _dump(f"debug_payload_{label}.txt", data)
        raise RuntimeError(f"could not decode payload ({e})")
    for pair in _g(payload, 7, 1, 1, default=[]) or []:
        if isinstance(pair, list) and len(pair) >= 2:
            page.airline_names[str(pair[0])] = str(pair[1])
    groups = _g(payload, 3, 0)
    if groups is None:
        return page
    for k in groups:
        try:
            flight = k[0]
            legs = [SingleFlight(
                from_airport=Airport(code=sf[3], name=sf[4]), to_airport=Airport(code=sf[6], name=sf[5]),
                departure=SimpleDatetime(date=tuple(sf[20]), time=_parse_time(sf[8])),
                arrival=SimpleDatetime(date=tuple(sf[21]), time=_parse_time(sf[10])),
                duration=sf[11] or 0, plane_type=_g(sf, 17, default="") or "",
            ) for sf in flight[2]]
            page.flights.append(Flights(
                type=flight[0], price=int(_g(k, 1, 0, 1, default=0) or 0), airlines=list(flight[1] or []),
                flights=legs, carbon=CarbonEmission(typical_on_route=0, emission=0)))
        except Exception:  # noqa: BLE001
            page.skipped += 1
    if page.skipped and not page.flights:
        _dump(f"debug_payload_{label}.txt", data)
    return page


# ---------------------------------------------------------------------------
# Queries: a job is (dep, origin, dest, ret_date|None, ret_to|None)
# ---------------------------------------------------------------------------

_KEY_SUFFIX = ""   # set from args: cache entries priced with bags must not mix with bag-free ones


def job_key(job) -> str:
    dep, o, d, ret, ret_to = job
    k = f"{o}>{d}@{dep.isoformat()}"
    if ret:
        k += f"|back@{ret.isoformat()}"
        if ret_to and ret_to != o:
            k += f">{ret_to}"
    return k + _KEY_SUFFIX


def _leg(d: date, a: str, b: str, args) -> FlightQuery:
    return FlightQuery(
        date=d.isoformat(), from_airport=a, to_airport=b, max_stops=args.max_stops,
        max_duration_minutes=int(args.max_hours * 60) if args.max_hours else None,
        connecting_airports=args.via or None,
        max_layover_minutes=int(args.max_layover_hours * 60) if args.max_layover_hours else None,
    )


def search(job, args):
    dep, o, d, ret, ret_to = job
    key = job_key(job)
    open_jaw = bool(ret and ret_to and ret_to != o)
    legs = [_leg(dep, o, d, args)]
    if ret:
        legs.append(_leg(ret, d, ret_to if open_jaw else o, args))
    query = create_query(
        flights=legs, trip="multi-city" if open_jaw else ("round-trip" if ret else "one-way"),
        seat=args.seat, passengers=Passengers(adults=args.adults), currency=args.currency, language="en-GB",
        checked_bags=args.checked_bags, hide_separate_and_self_transfer=False,
    )
    url = query.url()
    last_err = None
    for attempt in range(args.retries + 1):
        try:
            page = parse_page(fetch_html(query, args), key)
            if page.skipped:
                print(f"    ({key}: skipped {page.skipped} malformed)")
            rows = []
            for f in page.flights:
                it = parse_itinerary(f, dep.isoformat(), o, d, args.currency)
                if not it:
                    continue
                it.google_url = url
                if ret:
                    it.kind = "open-jaw-fare" if open_jaw else "rt-fare"
                    it.ret_date, it.ret_to, it.stay_days = ret.isoformat(), (ret_to if open_jaw else o), (ret - dep).days
                    t = json.loads(it.tickets_json)[0]
                    t.update({"trip": "multi-city" if open_jaw else "round-trip", "ret": ret.isoformat(), "ret_to": it.ret_to})
                    it.tickets_json = json.dumps([t])
                rows.append(it)
            return key, rows, page.airline_names, None
        except Exception as e:  # noqa: BLE001
            last_err = f"{type(e).__name__}: {e}"
            time.sleep(args.delay * (attempt + 1) + random.random())
    return key, [], {}, last_err


def ensure_ticket(it: Itinerary) -> Itinerary:
    """Rows cached by older versions have no tickets_json; derive it for single-ticket rows."""
    if not it.tickets_json and it.kind in ("one-way", "rt-fare", "open-jaw-fare"):
        t = {"trip": {"one-way": "one-way", "rt-fare": "round-trip", "open-jaw-fare": "multi-city"}[it.kind],
             "from": it.origin, "to": it.dest, "dep": it.dep_date, "dep_dt": it.dep_dt, "route": it.route,
             "airlines": it.airlines, "price": it.price}
        if it.ret_date:
            t.update({"ret": it.ret_date, "ret_to": it.ret_to or it.origin})
        it.tickets_json = json.dumps([t])
    return it


class Cache:
    def __init__(self, path, max_age_days: float | None = None):
        self.path, self.data, self.max_age = path, {}, max_age_days
        if path and os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                self.data = json.load(fh)

    def get(self, key):
        v = self.data.get(key)
        if v is None:
            return None
        if self.max_age and v.get("fetched"):
            age = datetime.now() - datetime.fromisoformat(v["fetched"])
            if age.total_seconds() > self.max_age * 86400:
                return None
        rows = []
        for dct in v["rows"]:
            rows.append(ensure_ticket(Itinerary(**{k: x for k, x in dct.items() if k in Itinerary.__dataclass_fields__})))
        return rows, v["names"]

    def put(self, key, rows, names):
        self.data[key] = {"rows": [asdict(r) for r in rows], "names": names, "fetched": datetime.now().isoformat()}

    def save(self):
        if self.path:
            with open(self.path + ".tmp", "w", encoding="utf-8") as fh:
                json.dump(self.data, fh)
            os.replace(self.path + ".tmp", self.path)


def run_queries(jobs, args, cache: Cache):
    results, names, errors, todo = {}, {}, [], []
    for j in jobs:
        hit = cache.get(job_key(j))
        if hit:
            results[job_key(j)], n = hit
            names.update(n)
        else:
            todo.append(j)
    print(f"  {len(jobs)} queries, {len(jobs) - len(todo)} from cache, {len(todo)} to fetch")

    def worker(job):
        time.sleep(args.delay + random.random() * 0.5)
        return search(job, args)

    done = 0
    ex = ThreadPoolExecutor(max_workers=args.workers)
    futs = [ex.submit(worker, j) for j in todo]
    try:
        for fut in as_completed(futs):
            key, rows, n, err = fut.result()
            done += 1
            if err:
                errors.append((key, err))
                print(f"  [{done:>4}/{len(todo)}] {key}: ERROR {err[:70]}", flush=True)
            else:
                results[key] = rows
                names.update(n)
                cache.put(key, rows, n)
                if done % 10 == 0:
                    cache.save()
                print(f"  [{done:>4}/{len(todo)}] {key}: {len(rows)} results", flush=True)
    except KeyboardInterrupt:
        print(f"\nInterrupted after {done}/{len(todo)} — cancelling the rest and saving cache "
              f"(rerun the same command to resume).", flush=True)
        ex.shutdown(wait=False, cancel_futures=True)
        cache.save()
        raise SystemExit(130)
    ex.shutdown(wait=True)
    cache.save()
    return results, names, errors


# ---------------------------------------------------------------------------
# Filters and combining
# ---------------------------------------------------------------------------

def passes_filters(it: Itinerary, args, check_price: bool = True) -> bool:
    if check_price and it.price < args.min_price:   # Google lists some itineraries without a fare (price 0)
        return False
    lim = args.max_hours * 60 if args.max_hours else None
    if lim and (it.total_minutes > lim or it.ret_total_minutes > lim):
        return False
    if args.max_price and it.price > args.max_price:
        return False
    if set(it.airlines) & args.excluded_airlines:
        return False
    hops = set(it.route.split("-")[1:-1]) | (set(it.ret_route.split("-")[1:-1]) if it.ret_route else set())
    return not (hops & args.excluded_airports)


def _gap_ok(a_arr: str, b_dep: str, args) -> int | None:
    g = _gap(a_arr, b_dep)
    return g if args.min_self_transfer * 60 <= g <= args.max_self_transfer * 60 else None


def glue(a: Itinerary, b: Itinerary, hub: str, gap: int) -> Itinerary:
    """origin->hub ticket a + hub->dest ticket b (outbound direction)."""
    return Itinerary(
        dep_date=a.dep_date, origin=a.origin, dest=b.dest, price=a.price + b.price, currency=a.currency,
        airlines=sorted(set(a.airlines) | set(b.airlines)), route=_join_route(a.route, b.route),
        stops=a.stops + b.stops + 1, flight_minutes=a.flight_minutes + b.flight_minutes,
        layover_minutes=a.layover_minutes + b.layover_minutes + gap,
        total_minutes=a.total_minutes + b.total_minutes + gap, dep_dt=a.dep_dt, arr_dt=b.arr_dt,
        layovers=", ".join(x for x in [a.layovers if a.stops else "", f"{hub} {_fmt_dur(gap)}*",
                                      b.layovers if b.stops else ""] if x),
        tickets=a.tickets + b.tickets, self_transfer_at=hub, google_url=a.google_url, ret_url=b.google_url,
        tickets_json=json.dumps(json.loads(a.tickets_json or "[]") + json.loads(b.tickets_json or "[]")),
    )


def oneway_jobs(dep: date, o: str, d: str, args):
    jobs = [(dep, o, d, None, None)]
    for hub in args.self_transfer:
        if hub not in (o, d):
            jobs += [(dep, o, hub, None, None), (dep, hub, d, None, None), (dep + timedelta(days=1), hub, d, None, None)]
    return jobs


def _get(R: dict, key: str) -> list:
    return R.get(key + _KEY_SUFFIX, [])


def oneway_candidates(dep: date, o: str, d: str, args, R: dict) -> list[Itinerary]:
    rows = [r for r in _get(R, f"{o}>{d}@{dep.isoformat()}") if passes_filters(r, args)]
    for hub in args.self_transfer:
        if hub in (o, d):
            continue
        first = [r for r in _get(R, f"{o}>{hub}@{dep.isoformat()}") if passes_filters(r, args)]
        second = [r for d2 in (dep, dep + timedelta(days=1))
                  for r in _get(R, f"{hub}>{d}@{d2.isoformat()}") if passes_filters(r, args)]
        for a in first:
            for b in second:
                g = _gap_ok(a.arr_dt, b.dep_dt, args)
                if g is not None:
                    c = glue(a, b, hub, g)
                    if passes_filters(c, args):
                        rows.append(c)
    return rows


def rt_hub_jobs(dep: date, ret: date, o: str, d: str, args):
    """Two-round-trips mode: long-haul RT o<->hub, hop RT hub<->d, plus one-way
    schedule lookups for the return legs (to check the return connection)."""
    jobs = []
    for hub in args.rt_hubs:
        if hub in (o, d):
            continue
        jobs.append((dep, o, hub, ret, None))                                  # long-haul RT
        for hd in (dep, dep + timedelta(days=1)):
            for hr in (ret - timedelta(days=1), ret):
                jobs.append((hd, hub, d, hr, None))                            # hop RT
        for hr in (ret - timedelta(days=1), ret):
            jobs.append((hr, d, hub, None, None))                              # hop return schedule
        jobs.append((ret, hub, o, None, None))                                 # long-haul return schedule
    return jobs


def rt_hub_candidates(dep: date, ret: date, o: str, d: str, args, R: dict) -> list[Itinerary]:
    out = []
    for hub in args.rt_hubs:
        if hub in (o, d):
            continue
        longs = [r for r in _get(R, f"{o}>{hub}@{dep.isoformat()}|back@{ret.isoformat()}") if passes_filters(r, args)]
        if not longs:
            continue
        # return schedules (one-way results used for timing only)
        long_ret = [r for r in _get(R, f"{hub}>{o}@{ret.isoformat()}") if passes_filters(r, args, check_price=False)]
        hops = []
        for hd in (dep, dep + timedelta(days=1)):
            for hr in (ret - timedelta(days=1), ret):
                for h in _get(R, f"{hub}>{d}@{hd.isoformat()}|back@{hr.isoformat()}"):
                    if passes_filters(h, args):
                        hops.append((hr, h))
        for A in longs:
            for hr, B in hops:
                g_out = _gap_ok(A.arr_dt, B.dep_dt, args)
                if g_out is None:
                    continue
                # return: does ANY hop-return on hr connect to ANY long-haul return on ret?
                hop_ret = [r for r in _get(R, f"{d}>{hub}@{hr.isoformat()}") if passes_filters(r, args, check_price=False)]
                best = None
                for x in hop_ret:
                    for y in long_ret:
                        g = _gap_ok(x.arr_dt, y.dep_dt, args)
                        if g is not None and (best is None or x.total_minutes + g + y.total_minutes < best[0]):
                            best = (x.total_minutes + g + y.total_minutes, _join_route(x.route, y.route), g)
                c = glue(A, B, hub, g_out)
                c.kind, c.tickets = "two-round-trips", 2
                c.ret_date, c.ret_to, c.stay_days = ret.isoformat(), o, (ret - dep).days
                c.price = A.price + B.price
                if best:
                    c.ret_total_minutes, c.ret_route = best[0], best[1]
                    c.note = f"return: hop {hr.isoformat()} then {ret.isoformat()}, ~{_fmt_dur(best[2])} at {hub}; confirm exact flights via urls"
                elif hop_ret and long_ret:
                    c.note = (f"WARNING: none of Google's listed return flights connect at {hub} "
                              f"(hop {hr.isoformat()}, long-haul {ret.isoformat()}) — check the full timetable via urls")
                else:
                    c.note = f"return timing unverified (hop {hr.isoformat()}, long-haul {ret.isoformat()})"
                if passes_filters(c, args):
                    out.append(c)
    return out


def pair_oneways(a: Itinerary, b: Itinerary, ret: date) -> Itinerary:
    return Itinerary(
        dep_date=a.dep_date, origin=a.origin, dest=a.dest, price=a.price + b.price, currency=a.currency,
        airlines=sorted(set(a.airlines) | set(b.airlines)), route=a.route, stops=a.stops,
        flight_minutes=a.flight_minutes, layover_minutes=a.layover_minutes, total_minutes=a.total_minutes,
        dep_dt=a.dep_dt, arr_dt=a.arr_dt, layovers=a.layovers, tickets=a.tickets + b.tickets,
        self_transfer_at=",".join(x for x in [a.self_transfer_at, b.self_transfer_at] if x),
        google_url=a.google_url, ret_url=b.google_url, ret_date=ret.isoformat(), ret_to=b.dest,
        stay_days=(ret - date.fromisoformat(a.dep_date)).days, ret_route=b.route,
        ret_total_minutes=b.total_minutes, kind="two-one-ways",
        tickets_json=json.dumps(json.loads(a.tickets_json or "[]") + json.loads(b.tickets_json or "[]")),
    )


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def score(it: Itinerary, args) -> float:
    hours = it.total_minutes / 60
    if it.ret_date:
        hours += (it.ret_total_minutes or it.total_minutes) / 60
    return it.price + hours * args.hour_value + (args.self_transfer_penalty if it.self_transfer_at else 0)


def print_table(rows, names, args):
    if not rows:
        print("\nNo itineraries matched your filters.")
        return
    rt = any(r.ret_date for r in rows)
    hdr = (f"{'Out':<11}" + (f"{'Back':<11}{'Stay':>4} {'Kind':<15}" if rt else "") +
           f"{'Price':>10} {'Tkts':>4} {'Out total':>9} {'Route':<24}" +
           (f"{'Return route':<20}{'Ret tot':>7} " if rt else "") +
           f"{'Airlines':<28}{'Dep':<17}{'Arr':<17}Layovers (* = ticket change)")
    print("\n" + hdr + "\n" + "-" * len(hdr))
    for it in rows[:args.top]:
        al = ", ".join(names.get(c, c) for c in it.airlines)[:27]
        line = f"{it.dep_date:<11}"
        if rt:
            line += f"{(it.ret_date or '-'):<11}{it.stay_days:>4} {it.kind:<15}"
        line += f"{it.price:>6} {it.currency:<3} {it.tickets:>4} {_fmt_dur(it.total_minutes):>9} {it.route:<24}"
        if rt:
            rr = it.ret_route or (f"..->{it.ret_to} (url)" if it.ret_to else "")
            line += f"{rr:<20}{_fmt_dur(it.ret_total_minutes):>7} "
        line += f"{al:<28}{_fmt_dt(it.dep_dt):<17}{_fmt_dt(it.arr_dt):<17}{it.layovers}"
        print(line)
    if len(rows) > args.top:
        print(f"... {len(rows) - args.top} more (see CSV)")


def write_csv(rows, path, args):
    fields = ["dep_date", "ret_date", "stay_days", "kind", "tickets", "self_transfer_at", "origin", "dest", "ret_to",
              "price", "currency", "score", "airlines", "route", "stops", "out_total_hours", "flight_minutes",
              "layover_minutes", "depart", "arrive", "layovers", "ret_route", "ret_total_hours", "note",
              "url_1", "url_2", "tickets_json"]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(fields)
        for it in rows:
            w.writerow([it.dep_date, it.ret_date, it.stay_days, it.kind, it.tickets, it.self_transfer_at, it.origin,
                        it.dest, it.ret_to, it.price, it.currency, round(score(it, args)), " ".join(it.airlines),
                        it.route, it.stops, round(it.total_minutes / 60, 1), it.flight_minutes, it.layover_minutes,
                        _fmt_dt(it.dep_dt), _fmt_dt(it.arr_dt), it.layovers, it.ret_route,
                        round(it.ret_total_minutes / 60, 1) if it.ret_total_minutes else "", it.note,
                        it.google_url, it.ret_url, it.tickets_json])


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def daterange(start: date, end: date, step: int):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=step)


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--from", dest="origins", nargs="+", required=True)
    p.add_argument("--to", dest="dests", nargs="+", required=True)
    p.add_argument("--start", type=date.fromisoformat, required=True)
    p.add_argument("--end", type=date.fromisoformat, required=True)
    p.add_argument("--step", type=int, default=1, help="days between sampled outbound dates")
    p.add_argument("--return-min-days", type=int, default=None, help="round trip: minimum stay")
    p.add_argument("--return-max-days", type=int, default=None, help="round trip: maximum stay (default min+14)")
    p.add_argument("--return-step", type=int, default=2, help="days between sampled return dates")
    p.add_argument("--no-open-jaw", action="store_true", help="return must land where you left from")
    p.add_argument("--no-rt-fares", action="store_true", help="skip Google round-trip fares")
    p.add_argument("--open-jaw-fares", action="store_true",
                   help="also query single-ticket open-jaw fares (out of X, back into Y) for every origin pair")
    p.add_argument("--rt-hubs", nargs="*", default=[], metavar="HUB",
                   help="two-round-trips mode: RT long-haul origin<->HUB + RT hop HUB<->dest")

    p.add_argument("--max-hours", type=float, default=None, help="max door-to-door per direction")
    p.add_argument("--max-layover-hours", type=float, default=None)
    p.add_argument("--max-stops", type=int, default=2, help="max connections per ticket")
    p.add_argument("--max-price", type=int, default=None)
    p.add_argument("--min-price", type=int, default=20,
                   help="drop itineraries priced below this (default 20): Google returns unpriced results as 0")
    p.add_argument("--via", nargs="*", default=[], help="only allow connections through these airports")
    p.add_argument("--exclude-airlines", nargs="*", default=None, help="IATA codes never to fly, e.g. QR EK")
    p.add_argument("--exclude-airports", nargs="*", default=None, help="IATA codes never to connect through, e.g. DOH DXB")
    p.add_argument("--no-default-excludes", action="store_true", help=argparse.SUPPRESS)

    p.add_argument("--self-transfer", nargs="*", default=[], metavar="HUB", help="two one-way tickets via HUB")
    p.add_argument("--min-self-transfer", type=float, default=DEFAULT_MIN_SELF_TRANSFER_H, help="hours")
    p.add_argument("--max-self-transfer", type=float, default=DEFAULT_MAX_SELF_TRANSFER_H, help="hours")
    p.add_argument("--self-transfer-penalty", type=float, default=60, help="score penalty for any ticket change")

    p.add_argument("--checked-bags", type=int, default=0,
                   help="price with N checked bags (default 0). Matters for LCC hops; changes every query, so it "
                        "uses separate cache entries")
    p.add_argument("--seat", default="economy", choices=["economy", "premium-economy", "business", "first"])
    p.add_argument("--adults", type=int, default=1)
    p.add_argument("--currency", default="EUR")
    p.add_argument("--sort", default="score", choices=["score", "price", "time"])
    p.add_argument("--hour-value", type=float, default=15.0)
    p.add_argument("--top", type=int, default=40)
    p.add_argument("--csv", default=None)
    p.add_argument("--cache", default=None, help="JSON file caching raw results")
    p.add_argument("--max-cache-age-days", type=float, default=14, help="refetch cache entries older than this (0 = never)")
    p.add_argument("--workers", type=int, default=2)
    p.add_argument("--delay", type=float, default=1.5)
    p.add_argument("--retries", type=int, default=2)
    p.add_argument("--proxy", default=None)

    a = p.parse_args(argv)
    if a.end < a.start:
        p.error("--end must be on/after --start")
    a.round_trip = a.return_min_days is not None
    if a.round_trip and a.return_max_days is None:
        a.return_max_days = a.return_min_days + 14
    if a.rt_hubs and not a.round_trip:
        p.error("--rt-hubs needs --return-min-days")
    up = lambda xs: [c.upper() for c in xs]  # noqa: E731
    a.excluded_airlines = (set() if a.no_default_excludes else set(DEFAULT_EXCLUDED_AIRLINES)) | set(up(a.exclude_airlines or []))
    a.excluded_airports = (set() if a.no_default_excludes else set(DEFAULT_EXCLUDED_AIRPORTS)) | set(up(a.exclude_airports or []))
    a.origins, a.dests, a.via = up(a.origins), up(a.dests), up(a.via)
    a.self_transfer = [h for h in up(a.self_transfer) if h not in a.excluded_airports]
    a.rt_hubs = [h for h in up(a.rt_hubs) if h not in a.excluded_airports]
    return a


def main(argv=None):
    global _KEY_SUFFIX
    args = parse_args(argv)
    _KEY_SUFFIX = f"|bags{args.checked_bags}" if args.checked_bags else ""
    cache = Cache(args.cache, args.max_cache_age_days or None)
    out_dates = list(daterange(args.start, args.end, args.step))

    def rets_for(dep):
        return list(daterange(dep + timedelta(days=args.return_min_days), dep + timedelta(days=args.return_max_days), args.return_step))

    print(f"Outbound {args.start} → {args.end} ({len(out_dates)} dates), {'/'.join(args.origins)} → {'/'.join(args.dests)}"
          + (f", stay {args.return_min_days}-{args.return_max_days} days" if args.round_trip else ", one-way")
          + f", {args.checked_bags} checked bag(s)")
    print(f"Excluding airlines: {' '.join(sorted(args.excluded_airlines)) or '-'}")
    print(f"Excluding hubs:     {' '.join(sorted(args.excluded_airports)) or '-'}")
    if args.self_transfer:
        print(f"Self-transfer via:  {' '.join(args.self_transfer)} ({args.min_self_transfer:g}-{args.max_self_transfer:g}h between tickets)")
    if args.rt_hubs:
        print(f"Two-round-trips via {' '.join(args.rt_hubs)}")

    # ---- jobs -------------------------------------------------------------
    jobs = []
    for dep, o, d in itertools.product(out_dates, args.origins, args.dests):
        jobs += oneway_jobs(dep, o, d, args)
    ret_dates = sorted({r for dep in out_dates for r in rets_for(dep)}) if args.round_trip else []
    if args.round_trip:
        for r, o, d in itertools.product(ret_dates, args.origins, args.dests):
            jobs += oneway_jobs(r, d, o, args)
        for dep, o, d in itertools.product(out_dates, args.origins, args.dests):
            for r in rets_for(dep):
                if not args.no_rt_fares:
                    jobs.append((dep, o, d, r, None))
                if args.open_jaw_fares:
                    jobs += [(dep, o, d, r, o2) for o2 in args.origins if o2 != o]
                jobs += rt_hub_jobs(dep, r, o, d, args)
    jobs = list(dict.fromkeys(jobs))
    R, names, errors = run_queries(jobs, args, cache)

    # ---- candidates ------------------------------------------------------
    final = []
    outs = {(dep, o, d): oneway_candidates(dep, o, d, args, R) for dep, o, d in itertools.product(out_dates, args.origins, args.dests)}
    if not args.round_trip:
        final = [x for rows in outs.values() for x in rows]
    else:
        rets = {(r, d, o): oneway_candidates(r, d, o, args, R) for r, d, o in itertools.product(ret_dates, args.dests, args.origins)}
        for dep, o, d in itertools.product(out_dates, args.origins, args.dests):
            for r in rets_for(dep):
                final += [x for x in R.get(job_key((dep, o, d, r, None)), []) if passes_filters(x, args)]
                for o2 in args.origins:
                    if o2 != o:
                        final += [x for x in R.get(job_key((dep, o, d, r, o2)), []) if passes_filters(x, args)]
                final += rt_hub_candidates(dep, r, o, d, args, R)
                back_to = [o] if args.no_open_jaw else args.origins
                best_out = sorted(outs[(dep, o, d)], key=lambda x: score(x, args))[:5]
                best_ret = sorted((x for o2 in back_to for x in rets[(r, d, o2)]), key=lambda x: score(x, args))[:5]
                final += [pair_oneways(a, b, r) for a in best_out for b in best_ret]

    seen, unique = set(), []
    for it in final:
        k = (it.dep_date, it.ret_date, it.kind, it.route, it.ret_route, it.ret_to, tuple(it.airlines), it.price, it.dep_dt)
        if k not in seen:
            seen.add(k)
            unique.append(it)
    if args.sort == "price":
        unique.sort(key=lambda r: (r.price, r.total_minutes + r.ret_total_minutes))
    elif args.sort == "time":
        unique.sort(key=lambda r: (r.total_minutes + r.ret_total_minutes, r.price))
    else:
        unique.sort(key=lambda r: score(r, args))

    print_table(unique, names, args)
    kinds = {}
    for r in unique:
        kinds.setdefault(r.kind, []).append(r)
    print("\nCheapest per kind:")
    for k, rows in sorted(kinds.items()):
        b = min(rows, key=lambda r: r.price)
        print(f"  {k:<15} {len(rows):>4} found, cheapest {b.price} {b.currency}: {b.dep_date}"
              + (f" → {b.ret_date}" if b.ret_date else "") + f" {b.route}"
              + (f" / {b.ret_route}" if b.ret_route else "") + (f" via {b.self_transfer_at}" if b.self_transfer_at else ""))
    if args.csv:
        try:
            write_csv(unique, args.csv, args)
            print(f"\nWrote {len(unique)} itineraries to {args.csv}")
        except PermissionError:
            alt = args.csv.rsplit(".", 1)[0] + "_" + datetime.now().strftime("%H%M%S") + ".csv"
            write_csv(unique, alt, args)
            print(f"\n{args.csv} is locked (open in Excel?) — wrote {len(unique)} itineraries to {alt} instead")
    if errors:
        print(f"\n{len(errors)} queries failed (rerun with the same --cache to retry only those):")
        for k, e in errors[:10]:
            print(f"  {k}: {e}")


if __name__ == "__main__":
    main()
