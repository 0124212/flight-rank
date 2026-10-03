"""flight-rank MCP server (stdio) — free data only.

Tools:
  search_flights(origin, dest, date, cabin="economy", adults=1, currency="USD")
  rank(options, prefs={})
  cpp_value(points, program, cash_price)
  bonus_watch()
  cheap_hack(origin, dests, dates, flags={})
  rank_compare(origin, dest, date)
  price_signal(route, date)
  delay_risk(carrier, origin, dest, month="ALL")
  price_watch(origin, dest, date, target_price)
  card_pick(spend_profile)
  award_search(route, date)  # gated: needs SEATS_AERO_API_KEY

Primary: faster-flights (live Google Flights scrape, $0, no key).
Fallback: SerpAPI Google Flights (optional SERPAPI_KEY, 250 free/mo).
Cache: 1-hr JSON files in $TMPDIR/flight-rank/.
"""
import hashlib
import json
import os
import tempfile
import time
import urllib.parse
from pathlib import Path

import httpx

try:
    from mcp.server.fastmcp import FastMCP
except ImportError:  # pragma: no cover
    FastMCP = None  # type: ignore

CACHE_TTL = 3600
CACHE_DIR = Path(tempfile.gettempdir()) / "flight-rank"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

mcp = FastMCP("flight-rank") if FastMCP else None

_GOOGLE_FLIGHTS = "https://www.google.com/travel/flights"


def _cache_key(name: str, payload: dict) -> Path:
    h = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]
    return CACHE_DIR / f"{name}-{h}.json"


def _cache_get(p: Path):
    try:
        d = json.loads(p.read_text())
        if time.time() - d.get("_ts", 0) < CACHE_TTL:
            return d["data"]
    except Exception:
        pass
    return None


def _cache_put(p: Path, data) -> None:
    try:
        p.write_text(json.dumps({"_ts": time.time(), "data": data}))
    except Exception:
        pass


def _deep_link(origin: str, dest: str, date: str) -> str:
    q = urllib.parse.urlencode({"q": f"Flights {origin} to {dest} on {date}"})
    return f"{_GOOGLE_FLIGHTS}?{q}"


def _with_backoff(fn, tries: int = 3, base: float = 1.0):
    last = None
    for i in range(tries):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001 — scraper is flaky by nature
            last = e
            time.sleep(base * (2**i))
    raise last  # type: ignore[misc]


def _via_faster_flights(origin, dest, date, cabin, adults, currency):
    """faster-flights 3.8.0 real API (module name is fast_flights)."""
    from fast_flights import FlightQuery, Passengers, create_query, get_flights

    seat = {
        "economy": "economy",
        "premium": "premium-economy",
        "premium-economy": "premium-economy",
        "business": "business",
        "first": "first",
    }.get(cabin, "economy")
    q = create_query(
        flights=[FlightQuery(date=date, from_airport=origin, to_airport=dest)],
        seat=seat,  # type: ignore[arg-type]
        trip="one-way",
        passengers=Passengers(adults=adults),
        currency=currency or "USD",  # type: ignore[arg-type]
    )
    return _with_backoff(lambda: get_flights(q))


def _via_serpapi(origin, dest, date, cabin, adults):
    key = os.environ.get("SERPAPI_KEY", "")
    if not key:
        raise RuntimeError("no SERPAPI_KEY and scraper failed")
    params = {
        "engine": "google_flights",
        "departure_id": origin,
        "arrival_id": dest,
        "outbound_date": date,
        "travel_class": {"economy": 1, "premium": 2, "business": 3, "first": 4}.get(cabin, 1),
        "adults": adults,
        "currency": "USD",
        "api_key": key,
    }
    r = _with_backoff(
        lambda: httpx.get("https://serpapi.com/search.json", params=params, timeout=30)
    )
    r.raise_for_status()
    return r.json()


def _normalize(raw, origin, dest, date) -> list[dict]:
    """Accept scraper objects, dicts, or SerpAPI JSON → uniform cards."""
    items: list = []
    if isinstance(raw, dict) and ("best_flights" in raw or "other_flights" in raw):
        items = list(raw.get("best_flights", [])) + list(raw.get("other_flights", []))
    elif isinstance(raw, dict) and "flights" in raw:
        items = raw["flights"]
    elif isinstance(raw, list):
        items = raw
    else:
        items = []
    out = []
    link = _deep_link(origin, dest, date)
    for f in items[:15]:
        if isinstance(f, dict):
            price = f.get("price") or f.get("total_price") or f.get("amount")
            if isinstance(price, str):
                digits = "".join(c for c in price if c.isdigit() or c == ".")
                price = float(digits) if digits else None
            out.append(
                {
                    "price": price,
                    "airline": f.get("airline") or f.get("carrier") or "unknown",
                    "stops": f.get("stops", f.get("num_stops", "?")),
                    "duration": f.get("duration") or f.get("travel_time") or "?",
                    "depart": f.get("departure") or f.get("depart_time") or "?",
                    "arrive": f.get("arrival") or f.get("arrive_time") or "?",
                    "link": f.get("link") or link,
                }
            )
        else:  # faster-flights Flights dataclass (price int, airlines[], legs[])
            legs = getattr(f, "flights", None)
            if isinstance(legs, list) and legs:
                stops = len(legs) - 1
                mins = sum(getattr(leg, "duration", 0) or 0 for leg in legs)
                dur = f"{mins // 60}h {mins % 60:02d}m" if mins else "?"
                dep = getattr(getattr(legs[0], "departure", ""), "time", None) or getattr(
                    legs[0], "departure", "?"
                )
                arr = getattr(getattr(legs[-1], "arrival", ""), "time", None) or getattr(
                    legs[-1], "arrival", "?"
                )
                out.append(
                    {
                        "price": getattr(f, "price", None),
                        "airline": "+".join(getattr(f, "airlines", []) or ["unknown"]),
                        "stops": stops,
                        "duration": dur,
                        "depart": str(dep),
                        "arrive": str(arr),
                        "link": link,
                    }
                )
                continue
            out.append(  # unknown object — best-effort attr read
                {
                    "price": getattr(f, "price", None),
                    "airline": getattr(f, "airline", getattr(f, "carrier", "unknown")),
                    "stops": getattr(f, "stops", "?"),
                    "duration": getattr(f, "duration", "?"),
                    "depart": getattr(f, "departure", "?"),
                    "arrive": getattr(f, "arrival", "?"),
                    "link": link,
                }
            )
    return [o for o in out if o.get("price")]


def _search_impl(origin, dest, date, cabin="economy", adults=1, currency="USD"):
    origin, dest = origin.upper().strip(), dest.upper().strip()
    key = _cache_key("search", locals())
    hit = _cache_get(key)
    if hit is not None:
        return hit
    errors = []
    try:
        raw = _via_faster_flights(origin, dest, date, cabin, adults, currency)
        opts = _normalize(raw, origin, dest, date)
        if opts:
            res = {"source": "faster-flights", "options": opts}
            _cache_put(key, res)
            return res
        errors.append("scraper returned 0 priced options")
    except Exception as e:  # noqa: BLE001
        errors.append(f"scraper: {e}")
    try:
        raw = _via_serpapi(origin, dest, date, cabin, adults)
        opts = _normalize(raw, origin, dest, date)
        res = {"source": "serpapi", "options": opts, "note": "; ".join(errors)}
        _cache_put(key, res)
        return res
    except Exception as e:  # noqa: BLE001
        errors.append(f"serpapi: {e}")
    res = {
        "source": "none",
        "options": [],
        "error": "; ".join(errors),
        "deep_link": _deep_link(origin, dest, date),
    }
    return res


def _rank_impl(options, prefs=None):
    prefs = prefs or {}
    cpp = float((prefs.get("value_per_point_cents") or 1.3)) / 100.0  # $/pt
    bonus = float(prefs.get("transfer_bonus_pct") or 0) / 100.0
    max_stops = prefs.get("max_stops")
    apply_delay = prefs.get("apply_delay_penalty")
    delay_dollars = float(prefs.get("delay_dollars", 150))
    delay_month = prefs.get("month")
    ranked = []
    for o in options:
        cash = o.get("price")
        miles = o.get("miles")  # optional: award cost if caller supplies it
        taxes = float(o.get("taxes") or 0)
        eff_miles_cash = None
        if miles:
            eff_miles_cash = miles * cpp / (1 + bonus) + taxes if bonus else miles * cpp + taxes
        effective = cash
        via = "cash"
        if eff_miles_cash is not None and cash and eff_miles_cash < cash:
            effective = round(eff_miles_cash, 2)
            via = "miles"
        stops = o.get("stops")
        penalty = 0
        try:
            penalty = int(stops) * 25 if isinstance(stops, int) else 0
        except Exception:
            pass
        score = (effective or 1e9) + penalty
        if max_stops is not None and isinstance(stops, int) and stops > max_stops:
            continue
        delay = None
        if apply_delay:
            carrier = (o.get("airline") or "unknown").split("+")[0]
            delay = _delay_lookup(
                carrier, o.get("origin") or prefs.get("origin"),
                o.get("dest") or prefs.get("dest"), delay_month,
            )
            score += delay["misconnect_prob"] * delay_dollars
        item = {**o, "effective_cash": effective, "via": via, "score": round(score, 2)}
        if delay:
            item["delay"] = delay
        ranked.append(item)
    ranked.sort(key=lambda r: r["score"])
    return ranked


DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def _load_json(name):
    try:
        return json.loads((DATA_DIR / name).read_text())
    except Exception as e:  # noqa: BLE001
        return {"_error": str(e)}


def _cpp_impl(points, program, cash_price):
    d = _load_json("cpp.json")
    if "_error" in d:
        return {"error": f"cpp.json unreadable: {d['_error']}"}
    want = (program or "").strip().lower()
    hit = None
    for p in d.get("programs", []):
        names = [p.get("program", "").lower()] + [a.lower() for a in p.get("aliases", [])]
        if want in names:
            hit = p
            break
    if not hit:
        return {"error": f"unknown program '{program}'", "known": [p.get("program") for p in d.get("programs", [])]}
    cpp = float(hit["cpp_cents"]) / 100.0
    value = round(points * cpp, 2)
    verdict = "points" if value >= cash_price else "cash"
    return {
        "program": hit["program"],
        "points": points,
        "cpp_cents": hit["cpp_cents"],
        "points_value_usd": value,
        "cash_price_usd": cash_price,
        "verdict": verdict,
        "savings_usd": round(abs(value - cash_price), 2),
        "note": "Check bonus_watch() — an active transfer bonus lowers the points needed.",
    }


def _bonus_watch_impl():
    d = _load_json("transfer_bonuses.json")
    if "_error" in d:
        return {"error": f"transfer_bonuses.json unreadable: {d['_error']}"}
    today = time.strftime("%Y-%m-%d")
    active, expired = [], []
    for b in d.get("bonuses", []):
        (active if not b.get("end_date") or b["end_date"] >= today else expired).append(b)
    return {
        "last_checked": d.get("last_checked"),
        "active": active,
        "expired_count": len(expired),
        "note": "Bonuses are time-limited — verify live before transferring.",
    }


def _cheap_hack_impl(origin, dests, dates, flags=None):
    flags = flags or {}
    origin = (origin or "").upper().strip()
    matrix, errors = {}, []
    for dest in dests or []:
        for date in dates or []:
            try:
                r = _search_impl(origin, dest, date)
                opts = r.get("options", [])
                cheapest = min(opts, key=lambda o: o.get("price") or 1e18) if opts else None
                matrix.setdefault(dest.upper().strip(), {})[date] = cheapest or {"error": r.get("error", "no options")}
            except Exception as e:  # noqa: BLE001
                errors.append(f"{dest}@{date}: {e}")
    best = None
    for dest, by_date in matrix.items():
        for date, o in by_date.items():
            if isinstance(o.get("price"), (int, float)) and (best is None or o["price"] < best["price"]):
                best = {"dest": dest, "date": date, **o}
    out = {
        "matrix": matrix,
        "positioning_hint": (
            f"Cheapest: {best['dest']} on {best['date']} at ${best['price']} — "
            "price a positioning flight from home if that's not your airport."
            if best else "No priced options found."
        ),
    }
    if flags.get("hidden_city"):
        out["hidden_city"] = {"requested": True, "caveat": "Skiplagging violates most airlines' contract of carriage; can void miles/status and strand checked bags. Use at own risk."}
    if flags.get("split_ticket"):
        out["split_ticket"] = {"requested": True, "caveat": "Separate tickets = no misconnect protection; leave 3h+ between legs and re-check bags."}
    if errors:
        out["errors"] = errors
    return out


def _skiplagged_search(origin, dest, date):
    """Skiplagged public MCP (streamable HTTP) → option cards. Raises on any failure."""
    url = "https://mcp.skiplagged.com/mcp"
    headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    with httpx.Client(timeout=30) as c:
        init = c.post(url, headers=headers, json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                       "clientInfo": {"name": "flight-rank", "version": "1.0"}},
        })
        init.raise_for_status()
        sid = init.headers.get("mcp-session-id", "")
        if sid:
            headers["mcp-session-id"] = sid
        try:
            c.post(url, headers=headers, json={"jsonrpc": "2.0", "method": "notifications/initialized"})
        except Exception:
            pass
        call = c.post(url, headers=headers, json={
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "sk_flights_search",
                       "arguments": {"origin": origin, "destination": dest,
                                     "date": date, "departureDate": date,
                                     "departure_date": date, "includeHiddenCity": True}},
        })
        call.raise_for_status()
    payload = None
    for line in call.text.splitlines():
        if line.startswith("data:"):
            try:
                payload = json.loads(line[5:].strip())  # last complete data event wins
            except Exception:
                continue
    if payload is None:
        try:
            payload = call.json()
        except Exception:
            raise RuntimeError(f"skiplagged: non-JSON response ({call.text[:120]})")
    result = (payload or {}).get("result", payload or {})
    if result.get("isError"):
        texts = [b.get("text", "") for b in result.get("content", []) if isinstance(b, dict)]
        raise RuntimeError(f"skiplagged: {'; '.join(texts)[:200]}")
    texts = [b.get("text", "") for b in result.get("content", []) if isinstance(b, dict)]
    raw = None
    for t in texts:
        try:
            raw = json.loads(t)
            break
        except Exception:
            continue
    if raw is not None:
        opts = _normalize(raw, origin, dest, date)
    else:  # markdown table (Skiplagged's usual shape) → parse directly
        opts = []
        for t in texts:
            opts.extend(_parse_skiplagged_md(t, origin, dest, date))
            if opts:
                break
    if not opts:
        raise RuntimeError(f"skiplagged: 0 priced options ({call.text[:120]})")
    return {"source": "skiplagged", "options": opts}


def _parse_skiplagged_md(text, origin, dest, date):
    """Parse Skiplagged MCP markdown table → option cards (incl. hidden-city flag)."""
    import re
    out, link = [], _deep_link(origin, dest, date)
    for line in (text or "").splitlines():
        m = re.match(r"^\|\s*\$([\d,]+)\s*\|([^|]*)\|([^|]*)\|([^|]*)\|([^|]*)\|([^|]*)\|([^|]*)\|", line)
        if not m:
            continue
        price = float(m.group(1).replace(",", ""))
        dur, stops_s, typ, airline, segs, book = (g.strip() for g in m.groups()[1:])
        stops = 0 if stops_s.lower().startswith("nonstop") else (
            int(re.search(r"\d+", stops_s).group()) if re.search(r"\d+", stops_s) else "?")
        times = re.findall(r"\d{4}-\d{2}-\d{2} (\d{2}:\d{2})", segs) or re.findall(
            r"(?<![:\d])(\d{2}:\d{2})(?::\d{2})?(?![\d:])", segs)
        bl = re.search(r"\[Book\]\(([^)]+)\)", book)
        out.append({
            "price": price, "airline": airline or "unknown", "stops": stops,
            "duration": dur or "?", "depart": times[0] if times else "?",
            "arrive": times[-1] if times else "?",
            "link": bl.group(1) if bl else link,
            "skiplagging": typ.lower().startswith("skiplag"),
        })
    return [o for o in out if o.get("price")]


def _hhmm(s):
    """'08:00' or '8:00 AM' → minutes. None if unparseable (skip time check)."""
    import re
    if not isinstance(s, str):
        return None
    m = re.match(r"\s*(\d{1,2}):(\d{2})\s*([AaPp])?\.?\s*[Mm]?\.?\s*$", s)
    if not m:
        return None
    h, mi, ap = int(m.group(1)), int(m.group(2)), (m.group(3) or "").upper()
    if ap == "P" and h < 12:
        h += 12
    if ap == "A" and h == 12:
        h = 0
    return h * 60 + mi


def _rank_compare_impl(origin, dest, date, cabin="economy", adults=1):
    from concurrent.futures import ThreadPoolExecutor
    origin, dest = origin.upper().strip(), dest.upper().strip()
    key = _cache_key("compare", {"origin": origin, "dest": dest, "date": date,
                                 "cabin": cabin, "adults": adults})
    hit = _cache_get(key)
    if hit is not None:
        return hit
    with ThreadPoolExecutor(max_workers=2) as ex:
        f1 = ex.submit(_search_impl, origin, dest, date, cabin, adults)
        f2 = ex.submit(_skiplagged_search, origin, dest, date)
        try:
            primary = f1.result()
        except Exception as e:  # noqa: BLE001
            primary = {"source": "none", "options": [], "error": str(e)}
        secondary, serr = None, None
        try:
            secondary = f2.result()
        except Exception as e:  # noqa: BLE001
            serr = str(e)

    def cheapest(opts):
        priced = [o for o in opts if isinstance(o.get("price"), (int, float))]
        return min(priced, key=lambda o: o["price"]) if priced else None

    po = (primary or {}).get("options", [])
    so = (secondary or {}).get("options", []) if secondary else []
    pc, sc = cheapest(po), cheapest(so)
    disputed, reasons = False, []
    if sc is None:
        note = f"skiplagged unreachable ({serr}); single-source via {primary.get('source')}"
    else:
        note = f"compared {len(po)} faster-flights vs {len(so)} skiplagged options"
        if pc and sc and abs(pc["price"] - sc["price"]) / max(pc["price"], 1) > 0.10:
            disputed = True
            reasons.append(f"price diff >10%: faster-flights ${pc['price']} vs skiplagged ${sc['price']}")
        # time mismatch: same airline's cheapest offer departs >60min apart across sources
        def cheapest_depart(opts):
            best = {}
            for o in opts:
                t = _hhmm(o.get("depart"))
                if t is None or not isinstance(o.get("price"), (int, float)):
                    continue
                for a in str(o.get("airline", "")).split("+"):
                    if a not in best or o["price"] < best[a][0]:
                        best[a] = (o["price"], t)
            return best
        pa, sa = cheapest_depart(po), cheapest_depart(so)
        for a, (_, t1) in pa.items():
            if a in sa and abs(t1 - sa[a][1]) > 60:
                disputed = True
                reasons.append(f"time mismatch on {a or 'unknown carrier'}: cheapest departs differ >60min")
                break
    res = {"primary_source": primary.get("source"), "cheapest_primary": pc,
           "cheapest_secondary": sc, "secondary_count": len(so),
           "disputed": disputed, "reasons": reasons, "note": note}
    _cache_put(key, res)
    return res


def _price_history_db():
    import sqlite3
    con = sqlite3.connect(CACHE_DIR / "history.db")
    con.execute("CREATE TABLE IF NOT EXISTS price_signals"
                "(route TEXT, date TEXT, lowest_price REAL, fetched_at REAL, cached INTEGER,"
                " PRIMARY KEY (route, date))")
    con.execute("CREATE TABLE IF NOT EXISTS price_watches"
                "(route TEXT, date TEXT, target_price REAL, last_price REAL, last_checked REAL,"
                " PRIMARY KEY (route, date))")
    return con


def _price_signal_impl(route, date, disputed=False):
    route = (route or "").upper().strip()
    if "-" not in route:
        return {"error": "route must look like ORIG-DEST, e.g. ICN-NRT"}
    o, d = [p.strip() for p in route.split("-", 1)]
    key = os.environ.get("SERPAPI_KEY", "")
    if not key:
        return {"route": route, "date": date, "signal": "none",
                "message": "SERPAPI_KEY absent — price_signal needs SerpAPI; skipping gracefully."}
    def serp(fresh):
        params = {"engine": "google_flights", "departure_id": o, "arrival_id": d,
                  "outbound_date": date, "api_key": key,
                  "no_cache": "true" if fresh else "false"}
        r = _with_backoff(lambda: httpx.get("https://serpapi.com/search.json", params=params, timeout=30))
        r.raise_for_status()
        return r.json()
    data = serp(False)
    insights, cached = data.get("price_insights") or {}, True
    if not insights or disputed:  # miss or disputed → one fresh pull (costs a credit)
        data = serp(True)
        insights, cached = data.get("price_insights") or {}, False
    lowest = insights.get("lowest_price")
    con = _price_history_db()
    prev = con.execute("SELECT lowest_price FROM price_signals WHERE route=? AND date=?",
                       (route, date)).fetchone()
    if lowest is not None:
        try:
            con.execute("INSERT OR REPLACE INTO price_signals VALUES (?,?,?,?,?)",
                        (route, date, float(lowest), time.time(), int(cached)))
            con.commit()
        except Exception:
            pass
    con.close()
    trend = None
    if lowest is not None and prev and prev[0] != lowest:
        trend = f"moved ${prev[0]} → ${lowest} since last check"
    return {"route": route, "date": date, "lowest_price": lowest,
            "price_level": insights.get("price_level"),
            "typical_range": [insights.get("typical_price_range", [None, None])],
            "cached": cached, "stored": lowest is not None, "trend": trend,
            "message": None if lowest is not None else "no price_insights in SerpAPI response"}


def _load_bts():
    full = DATA_DIR / "ontime_full.json"  # BTS PREZIP drop-in (same shape, more rows)
    if full.exists():
        try:
            return json.loads(full.read_text())
        except Exception:
            pass
    return _load_json("ontime_sample.json")


def _delay_lookup(carrier, origin, dest, month=None):
    t = _load_bts()
    rows = t.get("routes", []) if isinstance(t, dict) and "_error" not in t else []
    want_c = (carrier or "").upper().strip()
    o, d = (origin or "").upper().strip(), (dest or "").upper().strip()
    m = str(month or "ALL").upper()
    glob = {"misconnect_prob": 0.03, "avg_delay_min": 15.0, "cancel_pct": 1.0, "match": "global"}
    route_rows = [r for r in rows if r.get("origin") == o and r.get("dest") == d]
    for r in route_rows:
        if r.get("carrier", "").upper() == want_c and str(r.get("month", "ALL")).upper() in (m, "ALL"):
            return {"misconnect_prob": float(r["misconnect_prob"]),
                    "avg_delay_min": float(r["avg_delay_min"]),
                    "cancel_pct": float(r["cancel_pct"]), "match": "exact"}
    if route_rows:
        r = route_rows[0]
        return {"misconnect_prob": float(r["misconnect_prob"]),
                "avg_delay_min": float(r["avg_delay_min"]),
                "cancel_pct": float(r["cancel_pct"]), "match": "route"}
    return glob


def _delay_risk_impl(carrier, origin, dest, month="ALL"):
    d = _delay_lookup(carrier, origin, dest, month)
    return {"carrier": (carrier or "").upper(), "origin": (origin or "").upper(),
            "dest": (dest or "").upper(), "month": month, **d,
            "note": "Static BTS sample — swap in ontime_full.json for full coverage."}


def _price_watch_impl(origin, dest, date, target_price):
    """Check-on-query fare watch: Skiplagged passthrough (+faster-flights fallback), SQLite trend."""
    origin, dest = (origin or "").upper().strip(), (dest or "").upper().strip()
    route = f"{origin}-{dest}"
    current, source, errors = None, None, []
    try:
        r = _skiplagged_search(origin, dest, date)
        priced = [o["price"] for o in r.get("options", []) if isinstance(o.get("price"), (int, float))]
        if priced:
            current, source = min(priced), "skiplagged"
    except Exception as e:  # noqa: BLE001
        errors.append(f"skiplagged: {e}")
    if current is None:
        try:
            r = _search_impl(origin, dest, date)
            priced = [o["price"] for o in r.get("options", []) if isinstance(o.get("price"), (int, float))]
            if priced:
                current, source = min(priced), r.get("source")
        except Exception as e:  # noqa: BLE001
            errors.append(f"scraper: {e}")
    con = _price_history_db()
    prev = con.execute("SELECT last_price FROM price_watches WHERE route=? AND date=?",
                       (route, date)).fetchone()
    if current is not None:
        con.execute("INSERT OR REPLACE INTO price_watches VALUES (?,?,?,?,?)",
                    (route, date, float(target_price), float(current), time.time()))
        con.commit()
    con.close()
    if current is None:
        return {"route": route, "date": date, "target_price": target_price,
                "current_price": None, "below_target": False, "trend": None,
                "message": f"no live prices ({'; '.join(errors)[:200]})"}
    trend = None
    if prev and prev[0] != current:
        trend = f"moved ${prev[0]} → ${current} since last check"
    return {"route": route, "date": date, "target_price": target_price,
            "current_price": current, "source": source,
            "below_target": current <= target_price,
            "savings_usd": round(target_price - current, 2), "trend": trend,
            "message": None if current <= target_price else
            f"${current} is above your ${target_price} target — watch continues on next query (no daemon)."}


_SAMPLE_CARDS = [
    {"name": "Chase Sapphire Preferred", "issuer": "Chase", "currency": "CHASE", "annual_fee": 95,
     "rewards": {"dining": 3, "travel": 3, "groceries": 1, "other": 1},
     "bonus": "60k UR after $4k/3mo", "notes": "sample — refresh via card_pick live fetch"},
    {"name": "Amex Gold", "issuer": "Amex", "currency": "AMERICAN_EXPRESS", "annual_fee": 325,
     "rewards": {"dining": 4, "groceries": 4, "travel": 3, "other": 1},
     "bonus": "60k MR after $6k/6mo", "notes": "sample — refresh via card_pick live fetch"},
    {"name": "Capital One Venture X", "issuer": "CapOne", "currency": "CAPITAL_ONE", "annual_fee": 395,
     "rewards": {"travel": 5, "other": 2, "dining": 2, "groceries": 2},
     "bonus": "75k miles after $4k/3mo", "notes": "sample — refresh via card_pick live fetch"},
]


def _fetch_cards():
    """Vendor credit-card-bonuses-api JSON (raw first, GitHub discovery fallback, else samples)."""
    cache_paths = [DATA_DIR / "cards_cache.json", CACHE_DIR / "cards_cache.json"]
    for cp in cache_paths:
        try:
            d = json.loads(cp.read_text())
            if isinstance(d.get("cards"), list) and d["cards"]:
                return d["cards"], f"cache:{cp.name}"
        except Exception:
            continue
    cards, origin = None, "samples"
    raw_candidates = [
        "https://raw.githubusercontent.com/andenacitelli/credit-card-bonuses-api/main/exports/data.json",
        "https://raw.githubusercontent.com/andenacitelli/credit-card-bonuses-api/master/exports/data.json",
    ]
    for url in raw_candidates:
        try:
            r = httpx.get(url, timeout=20)
            r.raise_for_status()
            d = r.json()
            lst = d if isinstance(d, list) else d.get("cards", d.get("data"))
            if isinstance(lst, list) and lst:
                cards, origin = lst, "vendor:andenacitelli/credit-card-bonuses-api"
                break
        except Exception:
            continue
    if cards is None:
        try:
            idx = _with_backoff(lambda: httpx.get(
                "https://api.github.com/repos/andenacitelli/credit-card-bonuses-api/contents/exports",
                timeout=20, headers={"Accept": "application/vnd.github+json"}))
            idx.raise_for_status()
            blobs = [e.get("download_url") for e in idx.json()
                     if isinstance(e, dict) and (e.get("name", "").endswith(".json"))]
            for url in blobs[:3]:
                try:
                    r = httpx.get(url, timeout=20)
                    r.raise_for_status()
                    d = r.json()
                    lst = d if isinstance(d, list) else d.get("cards", d.get("data"))
                    if isinstance(lst, list) and lst:
                        cards, origin = lst, "vendor:andenacitelli/credit-card-bonuses-api"
                        break
                except Exception:
                    continue
        except Exception:
            pass
    if cards is None:
        return list(_SAMPLE_CARDS), "samples:offline-fallback"
    try:
        payload = {"last_fetched": time.strftime("%Y-%m-%d"), "cards": cards[:500]}
        for cp in cache_paths:
            try:
                cp.write_text(json.dumps(payload))
                origin += f"+cached:{cp.name}"
                break
            except Exception:
                continue
    except Exception:
        pass
    return cards, origin


_ISSUERS = {"CHASE": "Chase", "AMERICAN_EXPRESS": "Amex", "CAPITAL_ONE": "CapOne",
             "CITI": "Citi", "WELLS_FARGO": "Wells", "BANK_OF_AMERICA": "BofA"}
_TRANSFERABLE = {"CHASE", "AMERICAN_EXPRESS", "CAPITAL_ONE", "CITI", "WELLS_FARGO", "US_BANK"}


def _issuer_cpp(issuer):
    try:
        d = _load_json("cpp.json")
        for p in d.get("programs", []):
            names = [p.get("program", "")] + p.get("aliases", [])
            if any((issuer or "").lower() in str(n).lower() or str(n).lower() in (issuer or "").lower()
                   for n in names if n):
                return float(p["cpp_cents"]) / 100.0
    except Exception:
        pass
    return 0.01


def _score_cards(cards, profile):
    """Annual rewards value (points × issuer cpp) minus annual fee. No affiliate links."""
    scored = []
    for c in cards:
        if not isinstance(c, dict) or c.get("discontinued"):
            continue
        if str(c.get("currency", "")).upper() not in _TRANSFERABLE:
            continue  # co-brand currency: vendor flat rate isn't all-spend — skip, say so in note
        name = c.get("name") or c.get("cardName") or "unknown card"
        issuer = c.get("issuer") or c.get("bank") or ""
        issuer = _ISSUERS.get(str(issuer).upper(), issuer)
        if not issuer:
            for k in ("chase", "amex", "capital one", "capone", "citi", "bilt", "wells"):
                if k in name.lower():
                    issuer = k
                    break
        rw = c.get("rewards") or c.get("categories") or c.get("earn") or {}
        mults, basis = {}, None
        if isinstance(rw, dict) and rw:
            mults = {str(k).lower(): (v if isinstance(v, (int, float)) else 1) for k, v in rw.items()}
        elif isinstance(rw, list):
            for e in rw:
                if isinstance(e, dict):
                    cat = str(e.get("category") or e.get("type") or "other").lower()
                    mults[cat] = e.get("multiplier", e.get("rate", e.get("points", 1)))
        flat = min(c.get("universalCashbackPercent", 1) or 1, 2.5)  # cap: no all-spend card pays more
        if not mults:
            basis = f"flat {flat}% (vendor has no per-category rates; capped — high vendor rates are category-specific)"
        cpp = _issuer_cpp(issuer or name)
        pts = sum(float(profile.get(cat, 0)) * 12 * float(mults.get(cat, mults.get("other", flat)))
                  for cat in profile)
        try:
            fee = float(c.get("annual_fee", c.get("annualFee", c.get("fee", 0))) or 0)
        except Exception:
            fee = 0
        bonus = c.get("bonus") or c.get("signupBonus") or c.get("welcome_offer")
        if not bonus and isinstance(c.get("offers"), list) and c["offers"]:
            o = c["offers"][0]
            amt = o.get("amount")
            amt = amt[0].get("amount") if isinstance(amt, list) and amt else amt
            bonus = f"{amt} pts after ${o.get('spend')}/{o.get('days')}d" if amt else None
        item = {"name": name, "issuer": issuer or "unknown",
                "annual_rewards_usd": round(pts * cpp, 2),
                "annual_fee": fee, "net_usd": round(pts * cpp - fee, 2), "bonus": bonus}
        if basis:
            item["rewards_basis"] = basis
        scored.append(item)
    scored.sort(key=lambda s: s["net_usd"], reverse=True)
    return scored


def _card_pick_impl(spend_profile):
    profile = {str(k).lower(): float(v) for k, v in (spend_profile or {}).items()}
    if not profile or sum(profile.values()) <= 0:
        return {"error": "spend_profile needed, e.g. {dining: 500, travel: 800, groceries: 600, other: 1500} (monthly $)"}
    cards, origin = _fetch_cards()
    top = _score_cards(cards, profile)[:3]
    return {"spend_profile_monthly": spend_profile, "cards_considered": len(cards),
            "source": origin, "top_3": top,
            "note": ("Static math, no affiliate links. Transferable currencies only "
                     "(co-brand skipped: vendor flat rate isn't all-spend). "
                     "Bonuses change — verify current offers before applying.")}


def _award_search_impl(route, date):
    """seats.aero Cached Search passthrough (gated). No key → message + PointsYeah manual steps."""
    route = (route or "").upper().strip()
    if "-" not in route:
        return {"error": "route must look like ORIG-DEST, e.g. ICN-NRT"}
    o, d = [p.strip() for p in route.split("-", 1)]
    key = os.environ.get("SEATS_AERO_API_KEY", "")
    if not key:
        return {"route": route, "date": date, "source": "none", "availability": [],
                "message": "SEATS_AERO_API_KEY absent — award search needs a seats.aero key; nothing broke.",
                "manual_crosscheck": {
                    "tool": "PointsYeah.com (free plan shows ±4 days around your date)",
                    "steps": ["Search your route + date on PointsYeah",
                              "Note which programs show award seats",
                              "Check transfer_partners.json: which of your bank points transfer there",
                              "Call bonus_watch() before transferring — a bonus cuts the points needed",
                              "Set SEATS_AERO_API_KEY for live award_search results"]}}
    cache_key = _cache_key("awards", {"route": route, "date": date})
    hit = _cache_get(cache_key)
    if hit is not None:
        return hit
    try:
        r = _with_backoff(lambda: httpx.get(
            "https://api.seats.aero/partnerapi/search",
            params={"origin_airport": o, "destination_airport": d,
                    "start_date": date, "end_date": date},
            headers={"Partner-Authorization": f"Bearer {key}"}, timeout=30))
        r.raise_for_status()
        res = {"route": route, "date": date, "source": "seats.aero",
               "availability": _normalize_awards(r.json(), route, date)}
        _cache_put(cache_key, res)
        return res
    except Exception as e:  # noqa: BLE001 — never break: key present but call failed
        return {"route": route, "date": date, "source": "seats.aero",
                "availability": [], "message": f"seats.aero call failed ({e}); try again or use PointsYeah steps"}


def _normalize_awards(raw, route, date):
    """seats.aero response {data:[...]} → uniform award cards."""
    items = raw.get("data", []) if isinstance(raw, dict) else []
    out = []
    for f in items[:15]:
        if not isinstance(f, dict):
            continue
        out.append({
            "program": f.get("mileage_program") or f.get("source") or "unknown",
            "cabin": f.get("cabin") or f.get("cabin_class") or "?",
            "date": f.get("date") or f.get("departure_date") or date,
            "seats": f.get("remaining_seats", f.get("availability_count", "?")),
            "route": route,
            "link": f"https://seats.aero/search?min_seats=1&applicable_cabin=any&additional_days=false&additional_days_roundtrip=false&origin_airport={route.split('-')[0]}&destination_airport={route.split('-')[1]}&date={date}",
        })
    return out


if mcp:  # pragma: no cover — thin MCP wrappers over tested impls

    @mcp.tool()
    def search_flights(
        origin: str, dest: str, date: str,
        cabin: str = "economy", adults: int = 1, currency: str = "USD",
    ) -> dict:
        """Live flight prices (free scrape first, SerpAPI fallback). date=YYYY-MM-DD."""
        return _search_impl(origin, dest, date, cabin, adults, currency)

    @mcp.tool()
    def rank(options: list, prefs: dict = None) -> list:
        """Rank flight options by cash vs miles math. prefs: value_per_point_cents, transfer_bonus_pct, max_stops."""
        return _rank_impl(options, prefs or {})

    @mcp.tool()
    def cpp_value(points: int, program: str, cash_price: float) -> dict:
        """Point value math: points × program cpp vs cash price. program e.g. 'Chase UR'."""
        return _cpp_impl(points, program, cash_price)

    @mcp.tool()
    def bonus_watch() -> dict:
        """Active transfer bonuses from data/transfer_bonuses.json (refresh weekly)."""
        return _bonus_watch_impl()

    @mcp.tool()
    def cheap_hack(origin: str, dests: list, dates: list, flags: dict = None) -> dict:
        """Nearby-airport/date matrix via search_flights. flags: hidden_city, split_ticket (caveats only)."""
        return _cheap_hack_impl(origin, dests, dates, flags or {})

    @mcp.tool()
    def rank_compare(origin: str, dest: str, date: str, cabin: str = "economy", adults: int = 1) -> dict:
        """Cross-check faster-flights vs Skiplagged MCP. disputed=true when price>10% or times mismatch."""
        return _rank_compare_impl(origin, dest, date, cabin, adults)

    @mcp.tool()
    def price_signal(route: str, date: str, disputed: bool = False) -> dict:
        """SerpAPI price_insights (cached first, fresh only on miss/dispute) + SQLite history."""
        return _price_signal_impl(route, date, disputed)

    @mcp.tool()
    def delay_risk(carrier: str, origin: str, dest: str, month: str = "ALL") -> dict:
        """Misconnect probability from static BTS table (route e.g. JFK-LAX, month 1-12 or ALL)."""
        return _delay_risk_impl(carrier, origin, dest, month)

    @mcp.tool()
    def price_watch(origin: str, dest: str, date: str, target_price: float) -> dict:
        """Check-on-query fare watch (Skiplagged + fallback, SQLite trend). No daemon."""
        return _price_watch_impl(origin, dest, date, target_price)

    @mcp.tool()
    def card_pick(spend_profile: dict = None) -> dict:
        """Top-3 cards by spend match (vendor API cached, offline samples). No affiliate links."""
        return _card_pick_impl(spend_profile or {})

    @mcp.tool()
    def award_search(route: str, date: str) -> dict:
        """Award seats via seats.aero (needs SEATS_AERO_API_KEY; without it returns PointsYeah steps)."""
        return _award_search_impl(route, date)


if __name__ == "__main__" and mcp:
    mcp.run()
