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
  award_calendar(route, date=None, dates=None, window=7, max_miles=30000, home=None)
  award_watch(route, date, max_miles=30000, program=None, action="check")
  award_vs_cash(route, date, program)
  delta_scan(route, date)  # gated: needs DELTA_CURL_FILE
  search_legs(legs, cabin="economy", adults=1)
  fetch_bonus(program=None, refresh=False)  # webfetch: bonus pages ONLY, never fares

Primary: faster-flights (live Google Flights scrape, $0, no key).
Fallback: SerpAPI Google Flights (optional SERPAPI_KEY, 250 free/mo).
Cache: 1-hr JSON files in $TMPDIR/flight-rank/.
"""
import hashlib
import json
import os
import re
import tempfile
import time
import datetime
import functools
import urllib.parse
from pathlib import Path


def _httpx():
    """Lazy httpx import — keeps cold-start/RAM low on boxes that only use static paths."""
    import httpx  # local import: no network lib loaded until a live call runs

    return httpx

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


def _cache_get(p, ttl=CACHE_TTL):
    try:
        d = json.loads(p.read_text())
        if time.time() - d.get("_ts", 0) < ttl:
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
        lambda: _httpx().get("https://serpapi.com/search.json", params=params, timeout=30)
    )
    r.raise_for_status()
    return r.json()


def _to_float(x):
    """'$1,234' / 1234 / 1234.5 → float. None if unparseable (bool never counts)."""
    if isinstance(x, bool):
        return None
    if isinstance(x, (int, float)):
        return float(x)
    if isinstance(x, str):
        digits = "".join(c for c in x if c.isdigit() or c == ".")
        try:
            return float(digits) if digits else None
        except ValueError:
            return None
    return None


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
    for f in items:
        if isinstance(f, dict):
            price = _to_float(f.get("price") or f.get("total_price") or f.get("amount"))
            segs, mixed = _mixed_flag(f.get("segments") or f.get("legs"), "?")
            out.append(
                {
                    "price": price,
                    "airline": f.get("airline") or f.get("carrier") or "unknown",
                    "stops": f.get("stops", f.get("num_stops", "?")),
                    "duration": f.get("duration") or f.get("travel_time") or "?",
                    "depart": f.get("departure") or f.get("depart_time") or "?",
                    "arrive": f.get("arrival") or f.get("arrive_time") or "?",
                    "segments": segs, "mixed_cabin": mixed,
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
                dsegs = []
                for leg in legs:
                    lo = getattr(leg, "origin", None) or getattr(leg, "origin_airport", None)
                    ld = getattr(leg, "destination", None) or getattr(leg, "destination_airport", None)
                    if lo or ld:
                        dsegs.append({"origin": str(lo) if lo else None,
                                      "dest": str(ld) if ld else None})
                dsegs, dmixed = _mixed_flag(dsegs, "?")
                out.append(
                    {
                        "price": _to_float(getattr(f, "price", None)),
                        "airline": "+".join(getattr(f, "airlines", []) or ["unknown"]),
                        "stops": stops,
                        "duration": dur,
                        "depart": str(dep),
                        "arrive": str(arr),
                        "segments": dsegs, "mixed_cabin": dmixed,
                        "link": link,
                    }
                )
                continue
            out.append(  # unknown object — best-effort attr read
                {
                    "price": _to_float(getattr(f, "price", None)),
                    "airline": getattr(f, "airline", getattr(f, "carrier", "unknown")),
                    "stops": getattr(f, "stops", "?"),
                    "duration": getattr(f, "duration", "?"),
                    "depart": getattr(f, "departure", "?"),
                    "arrive": getattr(f, "arrival", "?"),
                    "link": link,
                }
            )
    priced = [o for o in out if isinstance(o.get("price"), (int, float))]
    priced.sort(key=lambda o: o["price"])
    return priced[:15]  # truncate AFTER sort — cheapest survive, not first-scraped


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


def _search_legs_impl(legs, cabin="economy", adults=1):
    """Multi-city: chain _search_impl per {o,d,date} leg, cheapest each + trip total."""
    if isinstance(legs, dict):
        legs = [legs]
    out, total, errors = [], 0.0, []
    for i, leg in enumerate(legs or []):
        if not isinstance(leg, dict):
            continue
        try:
            o, d, dt = (leg.get("o") or leg.get("origin", "")).upper().strip(), \
                       (leg.get("d") or leg.get("dest", "")).upper().strip(), leg.get("date")
            r = _search_impl(o, d, dt, cabin, adults)
            priced = [x for x in r.get("options", []) if isinstance(x.get("price"), (int, float))]
            best = min(priced, key=lambda x: x["price"]) if priced else {"error": r.get("error", "no options")}
            if isinstance(best.get("price"), (int, float)):
                total += best["price"]
            out.append({"leg": i + 1, "route": f"{o}-{d}", "date": dt, "cheapest": best})
        except Exception as e:  # noqa: BLE001 — one leg failing never kills the trip
            errors.append(f"leg {i + 1}: {e}")
            out.append({"leg": i + 1, "route": "?", "date": None, "cheapest": {"error": str(e)[:120]}})
    res = {"legs": out, "trip_total_usd": round(total, 2)}
    if errors:
        res["errors"] = errors
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


@functools.lru_cache(maxsize=8)  # ponytail: static files only; restart to pick up edits
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


_MILES_EXPIRY = {  # static rules — verify at booking time; Asiana cutoff per wind-down
    "korean air": "expire 10y from accrual",
    "asiana": "expire 10y from accrual; EARNING ENDS 2026-12-16 — book by 2026-12-01",
    "american": "expire after 24mo inactivity (any earn/redeem resets)",
    "aa": "expire after 24mo inactivity (any earn/redeem resets)",
    "delta": "never expire",
}
_OZ_CUTOFF = "2026-12-16"


def _oz_guard(row):
    """Block/warn OZ-earning bonus rows around the earn cutoff. Returns note or None."""
    blob = " ".join(str((row or {}).get(k, "")) for k in ("program", "partner", "to", "from")).lower()
    if "asiana" not in blob:
        return None
    end = (row or {}).get("end_date")
    if end and end <= _OZ_CUTOFF:
        return "OZ-earning bonus ends before the 2026-12-16 cutoff — book by 2026-12-01"
    return ("BLOCKED after 2026-12-16: Asiana Club earning ends — "
            "do not transfer for OZ post-cutoff dates; book by 2026-12-01")


def _bonus_expiry_sort(bonuses):
    """Attach days_left + oz_guard; soonest-expiring first (no end_date last)."""
    today = time.strftime("%Y-%m-%d")
    out = []
    for b in bonuses or []:
        end = (b or {}).get("end_date")
        try:
            dl = (datetime.date.fromisoformat(end) - datetime.date.fromisoformat(today)).days if end else None
        except ValueError:
            dl = None
        b2 = {**(b or {}), "days_left": dl}
        g = _oz_guard(b)
        if g:
            b2["oz_guard"] = g
        out.append(b2)
    out.sort(key=lambda b: (b["days_left"] is None, b["days_left"] or 0))
    return out


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
        "active": _bonus_expiry_sort(active),
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
    with _httpx().Client(timeout=30) as c:
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
    origin, dest = origin.upper().strip(), dest.upper().strip()
    key = _cache_key("compare", {"origin": origin, "dest": dest, "date": date,
                                 "cabin": cabin, "adults": adults})
    hit = _cache_get(key)
    if hit is not None:
        return hit
    # ponytail: serial, not ThreadPool — low-RAM box; two HTTP calls, no CPU gain from threads
    try:
        primary = _search_impl(origin, dest, date, cabin, adults)
    except Exception as e:  # noqa: BLE001
        primary = {"source": "none", "options": [], "error": str(e)}
    secondary, serr = None, None
    try:
        secondary = _skiplagged_search(origin, dest, date)
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
    if disputed and pc:  # attach live bonus math so the reply can price the dispute in points
        try:
            fb = _fetch_bonus_impl()
            act = [b for b in fb.get("bonuses", []) if isinstance(b.get("bonus_pct"), (int, float))]
            if act:
                top = max(act, key=lambda b: b["bonus_pct"])
                base_pts = round(pc["price"] / 0.013)
                eff_pts = round(base_pts / (1 + top["bonus_pct"] / 100))
                res["bonus_hint"] = {
                    "text": (f"{top['program']}→{top['partner']} +{top['bonus_pct']}% active, "
                             f"effective points price drops to ~{eff_pts:,} pts "
                             f"(vs ~{base_pts:,} at 1.3¢)"),
                    "via": fb.get("answered_by"),
                    "caveat": "assumes 1.3¢ point value and award availability — verify before transferring",
                }
        except Exception:
            pass
    _cache_put(key, res)
    return res


def _price_history_db():
    import sqlite3
    con = sqlite3.connect(CACHE_DIR / "history.db")
    try:  # ponytail: WAL for low-RAM concurrent reads; ignore on exotic filesystems
        con.execute("PRAGMA journal_mode=WAL")
    except Exception:
        pass
    con.execute("CREATE TABLE IF NOT EXISTS price_signals"
                "(route TEXT, date TEXT, lowest_price REAL, fetched_at REAL, cached INTEGER,"
                " PRIMARY KEY (route, date))")
    con.execute("CREATE TABLE IF NOT EXISTS price_watches"
                "(route TEXT, date TEXT, target_price REAL, last_price REAL, last_checked REAL,"
                " PRIMARY KEY (route, date))")
    con.execute("CREATE TABLE IF NOT EXISTS award_watches"
                "(route TEXT, date TEXT, program TEXT, max_miles REAL, last_hit REAL, last_checked REAL,"
                " PRIMARY KEY (route, date, program))")
    con.execute("CREATE TABLE IF NOT EXISTS award_miles"
                "(route TEXT, cabin TEXT, miles REAL, seen_at REAL)")
    for col, typ in (("remind_every_h", "REAL DEFAULT 24.0"), ("last_notified", "REAL DEFAULT 0")):
        cols = [r[1] for r in con.execute("PRAGMA table_info(award_watches)").fetchall()]
        if col not in cols:
            con.execute(f"ALTER TABLE award_watches ADD COLUMN {col} {typ}")
    con.commit()
    return con


def _price_signal_impl(route, date, disputed=False):
    route = (route or "").upper().strip()
    if "-" not in route:
        return {"error": "route must look like ORIG-DEST, e.g. ICN-NRT"}
    o, d = [p.strip() for p in route.split("-", 1)]
    key = os.environ.get("SERPAPI_KEY", "")
    if not key:
        return {"route": route, "date": date, "signal": "none",
                "message": "SERPAPI_KEY absent — price_signal needs SerpAPI; skipping gracefully.",
                "need_from_you": _need_from_you("serpapi_key")}
    def serp(fresh):
        params = {"engine": "google_flights", "departure_id": o, "arrival_id": d,
                  "outbound_date": date, "api_key": key,
                  "no_cache": "true" if fresh else "false"}
        r = _with_backoff(lambda: _httpx().get("https://serpapi.com/search.json", params=params, timeout=30))
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


@functools.lru_cache(maxsize=1)  # ponytail: static file only; restart to pick up edits
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


@functools.lru_cache(maxsize=1)  # ponytail: file+24h-cache backed; restart to force refetch
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
        # 2nd-vendor leg: same dataset via jsDelivr CDN (different infra, zero new trust):
        "https://cdn.jsdelivr.net/gh/andenacitelli/credit-card-bonuses-api@main/exports/data.json",
        "https://cdn.jsdelivr.net/gh/andenacitelli/credit-card-bonuses-api@master/exports/data.json",
    ]
    for url in raw_candidates:
        try:
            r = _httpx().get(url, timeout=20)
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
            idx = _with_backoff(lambda: _httpx().get(
                "https://api.github.com/repos/andenacitelli/credit-card-bonuses-api/contents/exports",
                timeout=20, headers={"Accept": "application/vnd.github+json"}))
            idx.raise_for_status()
            blobs = [e.get("download_url") for e in idx.json()
                     if isinstance(e, dict) and (e.get("name", "").endswith(".json"))]
            for url in blobs[:3]:
                try:
                    r = _httpx().get(url, timeout=20)
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


@functools.lru_cache(maxsize=64)  # ponytail: cpp.json is static; per-issuer memo
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
        waived = c.get("annual_fee_waived_yr1", c.get("first_year_fee_waived",
                       c.get("fee_waived_first_year", c.get("annualFeeWaived"))))
        if waived in (True, 1, "true", "yes", "waived"):
            item["net_yr1_usd"] = round(pts * cpp, 2)  # yr-1 fee waived → fee counts 0
            item["yr1_fee_waiver"] = True
        bend = c.get("bonus_end_date", c.get("bonusExpires", c.get("offer_end_date",
                 c.get("bonusEndDate"))))
        try:
            bl = (datetime.date.fromisoformat(str(bend)) - datetime.date.today()).days \
                if bend else None
        except ValueError:
            bl = None
        if bl is not None and bl <= 90:  # bonus-expiry weighting: urgency flag + rank input
            item["bonus_urgency"] = "urgent" if bl <= 30 else "expiring"
            item["bonus_days_left"] = bl
        scored.append(item)
    scored.sort(key=lambda s: (s.get("net_yr1_usd", s["net_usd"]),
                               1 if s.get("bonus_urgency") == "urgent" else 0), reverse=True)
    return scored


def _card_pick_impl(spend_profile):
    profile = {str(k).lower(): float(v) for k, v in (spend_profile or {}).items()}
    if not profile or sum(profile.values()) <= 0:
        return {"error": "spend_profile needed, e.g. {dining: 500, travel: 800, groceries: 600, other: 1500} (monthly $)",
                "need_from_you": _need_from_you("spend")}
    cards, origin = _fetch_cards()
    top = _score_cards(cards, profile)[:3]
    return {"spend_profile_monthly": spend_profile, "cards_considered": len(cards),
            "source": origin, "top_3": top,
            "note": ("Static math, no affiliate links. Transferable currencies only "
                     "(co-brand skipped: vendor flat rate isn't all-spend). "
                     "Bonuses change — verify current offers before applying.")}


_NEED_GUIDE = {
    "route": {"field": "route", "example": "ICN-LAX",
              "why": "origin + destination drive every award lookup",
              "how": "paste ORIG-DEST (nearby airports auto-expanded)"},
    "date": {"field": "date", "example": "2026-11-20",
             "why": "award space changes daily; calendar scans ±window around it",
             "how": "paste outbound YYYY-MM-DD (add return date for round trips)"},
    "program": {"field": "program", "example": "aeroplan",
                "why": "cpp math + transfer routing need the program",
                "how": "paste program name or code (aeroplan / united / delta / asiana)"},
    "cabin": {"field": "cabin", "example": "economy",
              "why": "saver ceilings differ by cabin",
              "how": "paste economy / premium / business / first (default economy)"},
    "seats_key": {"field": "SEATS_AERO_API_KEY",
                  "why": "unlocks live seats.aero cached search (free path stays: bookable deep links)",
                  "how": "paste key from seats.aero/settings (Pro ~$9.99/mo) as env SEATS_AERO_API_KEY"},
    "duffel_key": {"field": "DUFFEL_API_KEY_LIVE",
                   "why": "GDS cash anchor beats scrape estimates",
                   "how": "paste key from duffel.com dashboard as env DUFFEL_API_KEY_LIVE (scrape fallback otherwise)"},
    "delta_curl": {"field": "DELTA_CURL_FILE",
                  "why": "Delta cookie-replay borrows your own delta.com session",
                  "how": "devtools Network → copy the rm-offer-gql request as cURL → save to a file → set DELTA_CURL_FILE to its path (cookies expire ~30 min)"},
    "serpapi_key": {"field": "SERPAPI_KEY",
                    "why": "unlocks SerpAPI price_insights fallback/cross-check (250 free/mo)",
                    "how": "paste key from serpapi.com dashboard as env SERPAPI_KEY (scrape fallback otherwise)"},
    "spend": {"field": "spend_profile", "example": "{dining: 500, travel: 800, groceries: 600, other: 1500}",
              "why": "card math is spend-weighted; no profile means no ranking",
              "how": "paste monthly $ per category (dining / travel / groceries / other)"},
}


def _need_from_you(*fields):
    """Intake-first UX: structured ask-Juni blocks. Never invent availability."""
    return [_NEED_GUIDE[f] for f in fields if f in _NEED_GUIDE]


def _award_search_impl(route, date):
    """seats.aero Cached Search passthrough (gated). No key → message + PointsYeah manual steps."""
    route = (route or "").upper().strip()
    if "-" not in route:
        return {"error": "route must look like ORIG-DEST, e.g. ICN-NRT",
                "need_from_you": _need_from_you("route")}
    o, d = [p.strip() for p in route.split("-", 1)]
    if not (date or "").strip():
        return {"route": route, "error": "date missing",
                "need_from_you": _need_from_you("date")}
    key = os.environ.get("SEATS_AERO_API_KEY", "")
    if not key:
        return {"route": route, "date": date, "source": "none", "availability": [],
                "message": "SEATS_AERO_API_KEY absent — award search needs a seats.aero key; nothing broke.",
                "need_from_you": _need_from_you("seats_key"),
                "free_links": _award_free_links(route, date),
                "manual_crosscheck": {
                    "tool": "PointsYeah.com (free plan shows ±4 days around your date)",
                    "steps": ["Search your route + date on PointsYeah",
                              "Or open free_links.aa_award / free_links.southwest_points for live AA/WN award results",
                              "Note which programs show award seats",
                              "Check transfer_partners.json: which of your bank points transfer there",
                              "Call bonus_watch() before transferring — a bonus cuts the points needed",
                              "Set SEATS_AERO_API_KEY for live award_search results"]}}
    cache_key = _cache_key("awards", {"route": route, "date": date})
    hit = _cache_get(cache_key)
    if hit is not None:
        return hit
    try:
        r = _with_backoff(lambda: _httpx().get(
            "https://api.seats.aero/partnerapi/search",
            params={"origin_airport": o, "destination_airport": d,
                    "start_date": date, "end_date": date},
            headers={"Partner-Authorization": f"Bearer {key}"}, timeout=30))
        r.raise_for_status()
        res = {"route": route, "date": date, "source": "seats.aero",
               "availability": _filter_awards(_normalize_awards(r.json(), route, date))}
        _record_award_miles(route, res["availability"])
        _cache_put(cache_key, res)
        return res
    except Exception as e:  # noqa: BLE001 — never break: key present but call failed
        return {"route": route, "date": date, "source": "seats.aero",
                "availability": [], "message": f"seats.aero call failed ({e}); try again or use PointsYeah steps"}


def _mixed_flag(segments, top_cabin="?"):
    """segments[] → (trimmed segments, mixed_cabin bool). Cabins differ → mixed."""
    segs = []
    for s in (segments or [])[:4]:
        if not isinstance(s, dict):
            continue
        segs.append({k: s.get(k) for k in ("origin", "dest", "cabin", "flight")
                     if s.get(k) is not None})
    cabins = {s.get("cabin", top_cabin) for s in segs} | {top_cabin}
    cabins.discard("?")
    return segs, len(cabins) > 1


def _normalize_awards(raw, route, date):
    """seats.aero response {data:[...]} → uniform award cards."""
    items = raw.get("data", []) if isinstance(raw, dict) else []
    out = []
    for f in items[:15]:
        if not isinstance(f, dict):
            continue
        cabin = f.get("cabin") or f.get("cabin_class") or "?"
        segs, mixed = _mixed_flag(f.get("segments") or f.get("trips") or f.get("legs"), cabin)
        out.append({
            "program": f.get("mileage_program") or f.get("source") or "unknown",
            "cabin": cabin,
            "date": f.get("date") or f.get("departure_date") or date,
            "miles": f.get("mileage_cost") or f.get("miles") or f.get("YMileageCost"),
            "seats": f.get("remaining_seats", f.get("availability_count", "?")),
            "route": route,
            "segments": segs, "mixed_cabin": mixed,
            "link": f"https://seats.aero/search?min_seats=1&applicable_cabin=any&additional_days=false&additional_days_roundtrip=false&origin_airport={route.split('-')[0]}&destination_airport={route.split('-')[1]}&date={date}",
        })
    return out


def _award_free_links(route, date):
    """No-key bookable deep links. URL patterns from tszumowski/aa_flight_search_tool
    generate_url + borski/sw-fares build_url (both MIT). Pure stdlib, zero deps."""
    parts = (route or "").upper().split("-", 1)
    o = parts[0].strip() if parts else ""
    d = parts[1].strip() if len(parts) > 1 else ""
    aa = (f"https://www.aa.com/booking/search?locale=en_US&pax=1&adult=1&child=0&type=OneWay"
          f"&searchType=Award&cabin=&carriers=ALL&slices=%5B%7B%22orig%22:%22{o}%22,"
          f"%22origNearby%22:true,%22dest%22:%22{d}%22,%22destNearby%22:true,"
          f"%22date%22:%22{date}%22%7D%5D&maxAwardSegmentAllowed=2")
    wn = (f"https://www.southwest.com/air/booking/select.html?adultPassengersCount=1"
          f"&departureDate={date}&departureTimeOfDay=ALL_DAY&destinationAirportCode={d}"
          f"&fareType=POINTS&originationAirportCode={o}&passengerType=ADULT"
          f"&returnTimeOfDay=ALL_DAY&tripType=oneway")
    return {"aa_award": aa,
            "delta_search": "https://www.delta.com/flightsearch/book-a-flight",
            "southwest_points": wn,
            "pointsyeah": "https://www.pointsyeah.com"}


def _filter_awards(cards, max_miles=None, min_seats=1, cabins=None):
    """Port of tszumowski filter_flights (MIT) to award-card dicts. Unknown values pass."""
    out = []
    for c in cards or []:
        if not isinstance(c, dict):
            continue
        if cabins and str(c.get("cabin", "?")) not in cabins:
            continue
        try:
            if max_miles is not None and c.get("miles") is not None \
                    and float(c["miles"]) > max_miles:
                continue
        except (TypeError, ValueError):
            pass
        try:
            s = c.get("seats")
            if s is not None and not isinstance(s, bool) and str(s).strip() != "?" \
                    and int(s) < min_seats:
                continue
        except (TypeError, ValueError):
            pass
        out.append(c)
    return out


_NEARBY = {  # pfei-sa/seats-aero-viz CITY_TO_IATA pattern (MIT): metro expansion
    "ICN": ["ICN", "GMP"], "GMP": ["GMP", "ICN"], "SEL": ["ICN", "GMP"],
    "NRT": ["NRT", "HND"], "HND": ["HND", "NRT"], "TYO": ["NRT", "HND"],
    "JFK": ["JFK", "EWR", "LGA"], "EWR": ["EWR", "JFK", "LGA"], "LGA": ["LGA", "JFK", "EWR"],
}

_SAVER_TOTAL_CAPS = {"american": 100000, "aa": 100000, "aadvantage": 100000,
                      "alaska": 150000, "as": 150000,
                      # ponytail: conservative one-way totals, not per-cabin — tighten per cabin when data exists
                      "delta": 100000, "skymiles": 100000,
                      "united": 100000, "mileageplus": 100000, "ua": 100000,
                      "korean air": 120000, "ke": 120000, "skypass": 120000,
                      "asiana": 120000, "oz": 120000}  # borski thresholds (MIT) + safe defaults


def _saver_flag(program, miles, max_miles=30000):
    """saver/dynamic/unknown per card. Totals from borski; else max_miles ceiling."""
    if miles is None:
        return "unknown"
    try:
        m = float(miles)
    except (TypeError, ValueError):
        return "unknown"
    cap = _SAVER_TOTAL_CAPS.get((program or "").strip().lower())
    if cap is None:  # "American Airlines" → token "american"; exact first so "as" never fuzzy-hits
        for tok in (program or "").strip().lower().replace("/", " ").split():
            if tok in _SAVER_TOTAL_CAPS:
                cap = _SAVER_TOTAL_CAPS[tok]
                break
    if cap is not None:
        return "saver" if m < cap else "dynamic"
    return "saver" if m <= max_miles else "dynamic"


def _transfer_options(program):
    """transfer_partners.json rows matching a program name (substring, case-insensitive)."""
    d = _load_json("transfer_partners.json")
    if "_error" in d:
        return []
    want = (program or "").strip().lower()
    return [t for t in d.get("transfers", []) if want and want in str(t.get("airline", "")).lower()]


def _korea_block(o, d):
    """KE/OZ earn-via rows + SkyTeam note for Korea-touching routes."""
    if not ({o, d} & {"ICN", "GMP", "SEL"}):
        return {}
    return {"korea_programs": [
                {"program": "Korean Air SKYPASS", "earn_via": _transfer_options("korean air")},
                {"program": "Asiana Club", "earn_via": _transfer_options("asiana")}],
            "skyteam_note": ("KE is SkyTeam: the same seat is often bookable via Delta SkyMiles, "
                             "Virgin Atlantic, or Flying Blue — compare before transferring.")}


def _saver_counts(cards):
    """Per-day saver/dynamic/unknown tallies (pfei-sa get_route_df grouping pattern, MIT)."""
    n = {"saver": 0, "dynamic": 0, "unknown": 0}
    for c in cards or []:
        n[c.get("saver", "unknown") if c.get("saver") in n else "unknown"] += 1
    return n


def _positioning_leg(home, origin, date):
    """Cheapest home→origin cash card via _search_impl. None when unset/same/empty."""
    home = (home or "").upper().strip()
    if not home or home == origin:
        return None
    try:
        opts = _search_impl(home, origin, date).get("options", [])
        priced = [x for x in opts if isinstance(x.get("price"), (int, float))]
        if not priced:
            return None
        best = min(priced, key=lambda x: x["price"])
        return {"from": home, "to": origin, "date": date, **best}
    except Exception:
        return None


def _award_calendar_impl(route, date=None, dates=None, window=7, max_miles=30000, home=None):
    """±window saver scan (borski award-calendar orchestration, MIT). Keyed loop; no key → links."""
    route = (route or "").upper().strip()
    if "-" not in route:
        return {"error": "route must look like ORIG-DEST, e.g. ICN-NRT",
                "need_from_you": _need_from_you("route")}
    o, d = [p.strip() for p in route.split("-", 1)]
    if dates:
        days = sorted({str(x) for x in dates})
    elif date:
        try:
            c = datetime.date.fromisoformat(str(date))
        except ValueError:
            return {"error": "date must be YYYY-MM-DD",
                    "need_from_you": _need_from_you("date")}
        window = max(0, min(int(window), 14))
        days = [(c + datetime.timedelta(days=i)).isoformat() for i in range(-window, window + 1)]
    else:
        return {"error": "pass date=YYYY-MM-DD or dates=[...]",
                "need_from_you": _need_from_you("date")}
    pairs = [(a, b) for a in _NEARBY.get(o, [o]) for b in _NEARBY.get(d, [d])]
    key = os.environ.get("SEATS_AERO_API_KEY", "")
    base = {"route": route, "pairs": [f"{a}-{b}" for a, b in pairs],
            "max_miles": max_miles, "home": (home or "").upper().strip() or None,
            "positioning": _positioning_leg(home, o, days[len(days) // 2]),
            **_korea_block(o, d)}
    if not key:
        by_day = {day: {"availability": [], "saver_counts": _saver_counts([]),
                        "free_links": _award_free_links(f"{pairs[0][0]}-{pairs[0][1]}", day)}
                  for day in days}
        return _calendar_delta_merge(
                {**base, "source": "none", "by_day": by_day,
                 "density": [{"date": day, "saver": 0, "dynamic": 0, "unknown": 0, "total": 0}
                             for day in days],
                 "need_from_you": _need_from_you("seats_key"),
                 "message": "SEATS_AERO_API_KEY absent — per-date bookable links above; set key for live saver scan."},
                route, days)
    ckey = _cache_key("awardcal", {"route": route, "days": days, "max_miles": max_miles})
    hit = _cache_get(ckey)
    if hit is not None:
        return _calendar_delta_merge(hit, route, days)
    by_day, errors = {}, []
    for day in days:
        cards = []
        for a, b in pairs:
            try:
                r = _award_search_impl(f"{a}-{b}", day)
                for c in r.get("availability", []):
                    cards.append({**c, "pair": f"{a}-{b}",
                                  "saver": _saver_flag(c.get("program"), c.get("miles"), max_miles)})
            except Exception as e:  # noqa: BLE001 — one pair failing never kills the day
                errors.append(f"{a}-{b}@{day}: {e}")
        cards = _filter_awards(cards, max_miles=max_miles)
        def _mc(c):
            try:
                return float(c.get("miles"))
            except (TypeError, ValueError):
                return float("inf")
        best = min(cards, key=_mc, default=None)
        if best is not None and _mc(best) == float("inf"):
            best = None
        by_day[day] = {"availability": cards, "saver_counts": _saver_counts(cards),
                       "cheapest": ({k: best[k] for k in ("program", "cabin", "miles", "pair")}
                                    if best else None)}
    res = {**base, "source": "seats.aero", "by_day": by_day,
           "density": [{"date": day, **_saver_counts(by_day[day]["availability"]),
                        "total": len(by_day[day]["availability"])} for day in days]}
    if errors:
        res["errors"] = errors
    _cache_put(ckey, res)
    return _calendar_delta_merge(res, route, days)


def _calendar_delta_merge(out, route, days):
    """One-shot Delta replay (center date) merged when DELTA_CURL_FILE is set. Never breaks."""
    if not os.environ.get("DELTA_CURL_FILE", "") or not days:
        return out
    try:
        out["delta_replay"] = _award_delta_scan(route, days[len(days) // 2])
    except Exception as e:  # noqa: BLE001
        out["delta_replay"] = {"error": str(e)[:120]}
    return out


_DELTA_OFFER_URL = "https://offer-api-prd.delta.com/prd/rm-offer-gql"

_DELTA_GQL = """query ($offerSearchCriteria: OfferSearchCriteriaInput!) {
  gqlSearchOffers(offerSearchCriteria: $offerSearchCriteria) {
    gqlOffersSets {
      trips { tripId originAirportCode destinationAirportCode stopCnt }
      offers {
        offerId soldOut
        additionalOfferProperties { fareType soldOut unavailableForSale }
        offerItems { retailItems { retailItemMetaData { fareInformation {
          availableSeatCnt
          farePrice { totalFarePrice { milesEquivalentPrice { mileCnt } } }
        } } } }
      }
    }
  }
}"""


def _delta_parse_curl(curl_text):
    """Port of jeremyyma parse_curl (MIT): cookies + headers out of a copied curl."""
    m = re.search(r"-b '([^']+)'", curl_text) or re.search(r'--cookie "([^"]+)"', curl_text)
    cookies = m.group(1) if m else ""
    headers = {}
    for pat in (r"-H '([^']+)'", r'-H "([^"]+)"'):
        for h in re.finditer(pat, curl_text):
            k, _, v = h.group(1).partition(": ")
            if v:
                headers[k.lower()] = v
    return cookies, headers


def _delta_payload(origin, dest, date, seats=1):
    """One-way single-date port of jeremyyma build_payload (MIT)."""
    dt = datetime.datetime.strptime(date, "%Y-%m-%d").strftime("%Y-%m-%dT00:00:00")
    return {"variables": {"offerSearchCriteria": {
        "productGroups": [{"productCategoryCode": "FLIGHTS"}],
        "offersCriteria": {
            "resultsPageNum": 1, "resultsPerRequestNum": 30,
            "preferences": {"refundableOnly": False, "nonStopOnly": False, "excludeBrandTypes": []},
            "pricingCriteria": {"priceableIn": ["MILES"]},
            "flightRequestCriteria": {"searchOriginDestination": [
                {"departureLocalTs": dt, "destinations": [{"airportCode": dest}],
                 "origins": [{"airportCode": origin}]}]}},
        "customers": [{"passengerTypeCode": "ADT", "passengerId": str(i)}
                      for i in range(1, seats + 1)]}},
        "query": _DELTA_GQL}


def _delta_extract(data, route, date, max_miles=30000):
    """Port of jeremyyma extract_results (MIT): GraphQL walk → uniform cards."""
    try:
        sets = data["data"]["gqlSearchOffers"]["gqlOffersSets"]
    except (KeyError, TypeError):
        return []
    cards = []
    for s in sets or []:
        trips = s.get("trips") or []
        t0 = trips[0] if trips and isinstance(trips[0], dict) else {}
        for offer in s.get("offers") or []:
            props = offer.get("additionalOfferProperties") or {}
            if offer.get("soldOut") or props.get("soldOut") or props.get("unavailableForSale"):
                continue
            cabin = props.get("fareType", "?")
            segs, mixed = _mixed_flag(
                [{"origin": t0.get("originAirportCode"), "dest": t0.get("destinationAirportCode"),
                  "cabin": cabin}] if t0 else [], cabin)
            for item in offer.get("offerItems") or []:
                for retail in item.get("retailItems") or []:
                    fis = (retail.get("retailItemMetaData") or {}).get("fareInformation") or []
                    for fi in ([fis] if isinstance(fis, dict) else fis):
                        seats = fi.get("availableSeatCnt") or 0
                        fps = fi.get("farePrice") or []
                        for fp in ([fps] if isinstance(fps, dict) else fps):
                            miles = ((fp.get("totalFarePrice") or {})
                                     .get("milesEquivalentPrice", {}).get("mileCnt", 0))
                            if miles and miles <= max_miles:
                                cards.append({"program": "Delta SkyMiles",
                                              "cabin": cabin, "date": date,
                                              "miles": miles, "seats": seats, "route": route,
                                              "segments": segs, "mixed_cabin": mixed,
                                              "saver": _saver_flag("delta", miles, max_miles),
                                              "link": "https://www.delta.com/flightsearch/book-a-flight"})
    return cards


def _award_delta_scan(route, date, max_miles=30000):
    """Opt-in Delta cookie-replay saver scan (jeremyyma/AwardFlightSearch, MIT).
    Stub: gated on DELTA_CURL_FILE, curl_cffi soft-imported. No hard deps."""
    base = {"route": route, "date": date, "source": "delta-replay", "availability": []}
    curl_file = os.environ.get("DELTA_CURL_FILE", "")
    if not curl_file or not Path(curl_file).exists():
        return {**base, "message": "DELTA_CURL_FILE absent — paste a fresh delta.com "
                "rm-offer-gql curl (cookies expire ~30 min).",
                "need_from_you": _need_from_you("route", "date", "delta_curl")}
    try:
        from curl_cffi import requests as _cffi  # noqa: F401 — soft dep only
    except ImportError:
        return {**base, "message": "curl_cffi missing — pip install curl-cffi to enable "
                "the Delta replay scan."}
    route = (route or "").upper().strip()
    if "-" not in route:
        return {"error": "route must look like ORIG-DEST, e.g. ICN-NRT",
                "need_from_you": _need_from_you("route")}
    o, d = [p.strip() for p in route.split("-", 1)]
    cookies, bh = _delta_parse_curl(Path(curl_file).read_text())
    if not cookies:
        return {**base, "message": "No cookies parsed from DELTA_CURL_FILE — re-copy the "
                "rm-offer-gql curl from Chrome."}
    mc = re.search(r"mc_cache_key=([^;]+)", cookies)
    headers = {"accept": "application/json, text/plain, */*", "airline": "DL",
               "applicationid": "DC", "authorization": bh.get("authorization", "GUEST"),
               "channelid": "DCOM", "content-type": "application/json",
               "origin": "https://www.delta.com", "referer": "https://www.delta.com/",
               "transactionid": f"{mc.group(1) if mc else 'frankenstein'}_{int(time.time() * 1000)}",
               "user-agent": bh.get("user-agent", "Mozilla/5.0"),
               "x-app-route": "search", "x-app-type": "dcom-shop", "Cookie": cookies}
    try:
        resp = _cffi.post(_DELTA_OFFER_URL, headers=headers,
                          json=_delta_payload(o, d, date), impersonate="chrome124", timeout=30)
    except Exception as e:  # noqa: BLE001 — network wobble, never break
        return {**base, "message": f"Delta replay request failed ({e})."}
    if resp.status_code == 444:
        return {**base, "message": "HTTP 444: session expired — copy a fresh curl and retry."}
    if resp.status_code != 200:
        return {**base, "message": f"Delta replay HTTP {resp.status_code}."}
    cards = _delta_extract(resp.json(), route, date, max_miles)
    return {**base, "availability": cards,
            "message": None if cards else f"No Delta saver seats ≤ {max_miles:,} miles."}


def _record_award_miles(route, cards):
    """Append route-cabin-miles samples to history (prune >90d). Never raises."""
    try:
        con = _price_history_db()
        now = time.time()
        for c in cards or []:
            try:
                m = float(c.get("miles"))
            except (TypeError, ValueError):
                continue
            con.execute("INSERT INTO award_miles VALUES (?,?,?,?)",
                        (route, str(c.get("cabin", "?")), m, now))
        con.execute("DELETE FROM award_miles WHERE seen_at < ?", (now - 90 * 86400,))
        con.commit()
        con.close()
    except Exception:
        pass


def _rarity_impl(route, cabin=None, miles=None, days=30):
    """Percentile of miles vs last N days same route-cabin. p<=10 grab, p>=90 wait. Unknown-safe."""
    try:
        cur = float(miles)
    except (TypeError, ValueError):
        return {"pctl": None, "label": "unknown", "samples": 0}
    try:
        con = _price_history_db()
        q = "SELECT miles FROM award_miles WHERE route=? AND seen_at > ?"
        args = [route, time.time() - days * 86400]
        if cabin is not None:
            q += " AND cabin=?"
            args.append(cabin)
        hist = [r[0] for r in con.execute(q, args).fetchall()]
        con.close()
    except Exception:
        return {"pctl": None, "label": "unknown", "samples": 0}
    n = len(hist)
    if n < 2:
        return {"pctl": None, "label": "unknown", "samples": n}
    p = round(100.0 * sum(1 for h in hist if h <= cur) / n, 1)
    return {"pctl": p, "label": "grab" if p <= 10 else ("wait" if p >= 90 else "fair"),
            "samples": n}


def _award_watch_impl(route, date, max_miles=30000, program=None, action="check",
                      remind_every_h=24.0):
    """Check-on-query award watch (price_watch precedent — no daemon). Hit → live bonus wiring."""
    route = (route or "").upper().strip()
    if "-" not in route:
        return {"error": "route must look like ORIG-DEST, e.g. ICN-NRT",
                "need_from_you": _need_from_you("route")}
    if not (date or "").strip():
        return {"route": route, "error": "date missing",
                "need_from_you": _need_from_you("date")}
    program = (program or "").strip()
    con = _price_history_db()
    if action == "add":
        con.execute("INSERT OR REPLACE INTO award_watches VALUES (?,?,?,?,?,?,?,?)",
                    (route, date, program, float(max_miles), None, time.time(),
                     float(remind_every_h), 0))
        con.commit()
        con.close()
        return {"route": route, "date": date, "program": program or "any",
                "max_miles": max_miles, "watching": True, "remind_every_h": float(remind_every_h)}
    if action == "list":
        rows = con.execute("SELECT route, date, program, max_miles, last_hit, last_checked,"
                           " remind_every_h, last_notified FROM award_watches").fetchall()
        con.close()
        return {"watches": [{"route": r[0], "date": r[1], "program": r[2] or "any",
                             "max_miles": r[3], "last_hit": r[4],
                             "remind_every_h": r[6], "last_notified": r[7]} for r in rows]}
    if action == "remove":
        con.execute("DELETE FROM award_watches WHERE route=? AND date=? AND program=?",
                    (route, date, program))
        con.commit()
        con.close()
        return {"route": route, "date": date, "program": program or "any", "watching": False}
    r = _award_search_impl(route, date)  # action == "check"
    cards = [c for c in r.get("availability", [])
             if not program or program.lower() in str(c.get("program", "")).lower()]
    cards = _filter_awards(cards, max_miles=max_miles)
    hit = min((c.get("miles") for c in cards if c.get("miles") is not None),
              key=lambda m: float(m), default=None)
    now = time.time()
    prev = con.execute("SELECT remind_every_h, last_notified FROM award_watches"
                       " WHERE route=? AND date=? AND program=?",
                       (route, date, program)).fetchone()
    cadence, last_n = (prev or (float(remind_every_h), 0))
    con.execute("INSERT OR REPLACE INTO award_watches VALUES (?,?,?,?,?,?,?,?)",
                (route, date, program, float(max_miles),
                 float(hit) if hit is not None else None, now, float(cadence), last_n))
    con.commit()
    out = {"route": route, "date": date, "program": program or "any",
           "max_miles": max_miles, "hit": hit is not None,
           "best_miles": hit, "matches": len(cards)}
    if hit is not None:
        out["live_bonuses"] = _fetch_bonus_impl(program=program or None).get("bonuses", [])
        out["rarity"] = _rarity_impl(route, None, hit)
        notify = (now - (last_n or 0)) >= float(cadence or 0) * 3600
        out["notify"] = notify
        if notify:
            con.execute("UPDATE award_watches SET last_notified=? WHERE route=? AND date=? AND program=?",
                        (now, route, date, program))
            con.commit()
        out["message"] = ("Under ceiling — check live_bonuses before transferring."
                          + ("" if notify else " (reminder throttled: already notified this period)"))
    con.close()
    return out


def _ratio_mult(ratio):
    """'5:4' → 1.25 bank pts per mile. Unparseable → 1.0."""
    try:
        a, b = str(ratio or "1:1").split(":")
        return float(a) / float(b)
    except (ValueError, ZeroDivisionError):
        return 1.0


_DUFFEL_CABINS = {"economy": "economy", "premium": "premium_economy",
                  "business": "business", "first": "first"}  # Duffel cabin_class enum


def _duffel_anchor(origin, dest, date, cabin="economy", adults=1):
    """Opt-in Duffel GDS cash anchor (borski duffel skill, MIT). No key → None. 1h cache."""
    key = os.environ.get("DUFFEL_API_KEY_LIVE", "")
    if not key:
        return None
    ckey = _cache_key("duffel", {"o": origin, "d": dest, "date": date,
                                 "cabin": cabin, "adults": adults})
    hit = _cache_get(ckey)
    if hit is not None:
        return hit
    try:
        r = _with_backoff(lambda: _httpx().post(
            "https://api.duffel.com/air/offer_requests?return_offers=true&supplier_timeout=15000",
            headers={"Accept": "application/json", "Duffel-Version": "v2",
                     "Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={"data": {"slices": [{"origin": origin, "destination": dest,
                                       "departure_date": date}],
                              "passengers": [{"type": "adult"}] * max(1, int(adults)),
                              "cabin_class": _DUFFEL_CABINS.get(cabin, "economy")}},
            timeout=30))
        r.raise_for_status()
        offers = (r.json().get("data") or {}).get("offers") or []
        priced = [_to_float(x["total_amount"]) for x in offers
                  if isinstance(x, dict) and x.get("total_amount") not in (None, "")]
        priced = [p for p in priced if p is not None]
        if not priced:
            return None
        anchor = {"price": min(priced), "source": "duffel",
                  "ts": time.strftime("%Y-%m-%dT%H:%M:%S")}
        _cache_put(ckey, anchor)
        return anchor
    except Exception:
        return None  # down/bad key → caller falls back to labeled scrape price


def _award_vs_cash_impl(route, date, program, cabin="economy", adults=1):
    """Cheapest cash (_search_impl) vs program award cards, each scored by _cpp_impl."""
    route = (route or "").upper().strip()
    if "-" not in route:
        return {"error": "route must look like ORIG-DEST, e.g. ICN-NRT",
                "need_from_you": _need_from_you("route")}
    if not (date or "").strip():
        return {"route": route, "error": "date missing",
                "need_from_you": _need_from_you("date")}
    if not (program or "").strip():
        return {"route": route, "date": date, "error": "program missing",
                "need_from_you": _need_from_you("program")}
    o, d = [p.strip() for p in route.split("-", 1)]
    cash_res = _search_impl(o, d, date, cabin, adults)
    cash_opts = [x for x in cash_res.get("options", []) if isinstance(x.get("price"), (int, float))]
    cash = min((x["price"] for x in cash_opts), default=None)
    cash_source = cash_res.get("source")
    anchor = _duffel_anchor(o, d, date, cabin, adults)
    if anchor is not None:  # GDS anchor wins over scrape when opted in
        cash, cash_source = anchor["price"], "duffel"
    cash_anchor = (anchor or {"price": cash, "source": cash_source or "none",
                              "ts": time.strftime("%Y-%m-%dT%H:%M:%S")})
    awards = [c for c in _award_search_impl(route, date).get("availability", [])
              if program.lower() in str(c.get("program", "")).lower()]
    banks = _transfer_options(program)  # program → bank currency for cpp math
    bank = banks[0]["bank"] if banks else None
    cpp_bank = bank.split()[0] if bank else None  # "Marriott Bonvoy" → "Marriott" (cpp.json key)
    mult = _ratio_mult(banks[0].get("ratio")) if banks else 1.0
    rows = []
    for c in awards:
        try:
            miles = int(float(c.get("miles")))
        except (TypeError, ValueError):
            rows.append({**c, "verdict": "unknown", "note": "no miles figure — compare manually"})
            continue
        if cash is None:
            rows.append({**c, "cash_price_usd": None, "verdict": "unknown",
                         "note": "no cash fare found — award price stands alone"})
            continue
        if not bank:
            rows.append({**c, "cash_price_usd": cash, "verdict": "unknown",
                         "note": f"no transfer row for '{program}' — compare {miles} miles vs ${cash} manually"})
            continue
        v = _cpp_impl(round(miles * mult), cpp_bank, cash)
        if "error" in v:
            rows.append({**c, "cash_price_usd": cash, "verdict": "unknown",
                         "note": f"{v['error']} — compare {miles} miles vs ${cash} manually"})
        else:
            rows.append({**c, "via": bank, "cash_price_usd": cash,
                         "points_value_usd": v["points_value_usd"],
                         "verdict": v["verdict"], "savings_usd": v["savings_usd"]})
    return {"route": route, "date": date, "program": program, "via": bank,
            "cash_price_usd": cash, "cash_source": cash_source, "cash_anchor": cash_anchor,
            "rows": rows}


# PROJECT CONSTRAINT — webfetch scope: fetch_bonus reads ONLY static bonus/promo
# pages (the three lists below). NEVER webfetch for fares: fares come ONLY from
# the faster-flights scrape, the Skiplagged MCP, or SerpAPI. Scraped fare pages
# are volatile and mislead; bonus lists are slow-moving editorial content.
BONUS_SOURCES = [
    "https://www.going.com/guides/credit-card-transfer-bonuses",
    "https://roame.travel/guides/points-transfer-bonuses",
    "https://www.pointstothet.com/transfer-bonuses",
    # bank-direct (partner-authoritative, bonus-opportunistic; parser takes what matches):
    "https://creditcards.chase.com/rewards-credit-cards/ultimate-rewards",
    "https://www.americanexpress.com/en-us/rewards/membership-rewards/transfer-partners",
]
BONUS_TTL = 86400  # 24h — bonus lists move slowly; Jina keyless is 20 RPM, stay far under it

_BANK_ALIASES = {
    "membership rewards": "Amex MR", "amex": "Amex MR",
    "ultimate rewards": "Chase UR", "chase": "Chase UR",
    "capital one": "CapOne", "citi thankyou": "Citi", "thankyou": "Citi",
    "thank you": "Citi", "citi": "Citi", "bilt": "Bilt",
}
_MONTHS = {"january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
           "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
           "december": 12, "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6,
           "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12}


def _strip_html(html):
    from html.parser import HTMLParser

    class _T(HTMLParser):
        def __init__(self):
            super().__init__()
            self.parts = []

        def handle_data(self, d):
            self.parts.append(d)

    t = _T()
    t.feed(html or "")
    return " ".join(" ".join(t.parts).split())


def _searxng_read(url):
    """Self-host SearXNG reader (SEARXNG_URL, default localhost:8080). Raises on failure."""
    base = os.environ.get("SEARXNG_URL", "http://localhost:8080").rstrip("/")
    q = urllib.parse.quote(url, safe="")
    for u in (f"{base}/read?url={q}", f"{base}/search?format=json&q={q}"):
        r = _httpx().get(u, timeout=20, headers={"Accept": "application/json, text/html, text/plain"})
        r.raise_for_status()
        if "json" in r.headers.get("content-type", ""):
            d = r.json()
            res = d.get("results", d if isinstance(d, list) else [])
            host = url.split("/")[2]
            for e in res if isinstance(res, list) else []:
                if isinstance(e, dict) and host in str(e.get("url", "")) and e.get("content"):
                    return e["content"], "searxng"
        elif len(r.text) > 500:
            t = r.text
            return (_strip_html(t) if "<html" in t[:500].lower() else t), "searxng"
    raise RuntimeError("searxng: no readable result")


def _jina_read(url):
    """Jina Reader keyless (20 RPM free). Raises on failure."""
    r = _httpx().get("https://r.jina.ai/" + url, timeout=30, headers={"Accept": "text/plain"})
    r.raise_for_status()
    if len(r.text) < 500:
        raise RuntimeError(f"jina: suspiciously short ({len(r.text)} chars)")
    return r.text, "jina"


def _parse_bonus_rows(text):
    """'Transfer <Bank> to <Partner> with a NN% bonus [through <date>]' → rows."""
    import re
    banks = "|".join(sorted(_BANK_ALIASES, key=len, reverse=True))
    rows = []
    pat = re.compile(
        r"(" + banks + r")\b.{0,80}?\bto\b\s+"
        r"([A-Z][A-Za-z.&'\-]*(?:\s+[A-Za-z.&'\-]+){0,5})"
        r"\s*(?:[:\u2013\u2014-]\s*)?(?:with\s+an?\s+)?(\d{1,3})\s*%\s*(?:transfer\s+)?bonus", re.I | re.S)
    months = "|".join(sorted(_MONTHS, key=len, reverse=True))
    dpat = re.compile(
        r"(?:through|until|till|ends?|expir\w*|valid\s+(?:through|until))\b[^.\n]{0,50}?"
        r"(" + months + r")\s+(\d{1,2})(?:\D{0,10}?(\d{4}))?", re.I)
    seen = set()
    for m in pat.finditer(text or ""):
        pct = int(m.group(3))
        if not 5 <= pct <= 100:
            continue
        bank = _BANK_ALIASES[m.group(1).lower()]
        partner = re.sub(r"\s+", " ", m.group(2)).strip(" .,-")
        _prev = None
        while _prev != partner:  # strip filler words the greedy match swallowed
            _prev = partner
            partner = re.sub(r"\s+(for|with|and|plus|an?|to|of|a)$", "", partner, flags=re.I).strip()
        dm = dpat.search(text[m.end():m.end() + 200])
        end_date = None
        if dm:
            y = int(dm.group(3)) if dm.group(3) else time.localtime().tm_year
            end_date = f"{y}-{_MONTHS[dm.group(1).lower()]:02d}-{int(dm.group(2)):02d}"
        k = (bank.lower(), partner.lower())
        if k in seen or len(partner) < 3:
            continue
        seen.add(k)
        rows.append({"program": bank, "partner": partner, "bonus_pct": pct, "end_date": end_date})
    return rows


def _fetch_bonus_impl(program=None, refresh=False):
    """Live bonus lists (SearXNG → Jina → static). Never raises — degrades to static file."""
    tried, parsed = [], []
    ckey = _cache_key("fetch_bonus", {"program": program or "ALL"})
    if not refresh:
        hit = _cache_get(ckey, BONUS_TTL)
        if hit is not None:
            return {**hit, "cached": True}
    answered_by = None
    for url in BONUS_SOURCES:
        text, via = None, None
        for reader in (_searxng_read, _jina_read):
            try:
                text, via = reader(url)
                break
            except Exception as e:  # noqa: BLE001
                tried.append(f"{reader.__name__}:{e}"[:140])
        if not text:
            continue
        rows = _parse_bonus_rows(text)
        for r in rows:
            parsed.append({**r, "source": url})
        tried.append(f"{url} via {via}: {len(rows)} rows")
        if rows:
            answered_by = f"{via}:{url.split('/')[2]}"
            break  # cascade: first source with rows wins (24h cache makes this cheap)
    static = _bonus_watch_impl()
    merged, seen = [], set()
    for b in parsed:
        k = (str(b.get("program", "")).lower(), str(b.get("partner", "")).lower())
        if k not in seen:
            seen.add(k)
            merged.append(b)
    for b in static.get("active", []):
        k = (str(b.get("from", "")).lower(), str(b.get("to", "")).lower())
        if k not in seen:
            seen.add(k)
            merged.append({"program": b.get("from"), "partner": b.get("to"),
                           "bonus_pct": b.get("pct"), "end_date": b.get("end_date"),
                           "source": "static:transfer_bonuses.json"})
    if program:
        want = program.strip().lower()
        merged = [b for b in merged
                  if want in str(b.get("program", "")).lower()
                  or want in str(b.get("partner", "")).lower()]
    prev = _cache_get(ckey, ttl=10 * 86400) or {}  # stale copy OK — only for deval compare
    old_pct = {(str(b.get("program", "")).lower(), str(b.get("partner", "")).lower()): b.get("bonus_pct")
               for b in prev.get("bonuses", []) if isinstance(b, dict)}
    for b in merged:
        was = old_pct.get((str(b.get("program", "")).lower(), str(b.get("partner", "")).lower()))
        if isinstance(was, (int, float)) and isinstance(b.get("bonus_pct"), (int, float)) \
                and b["bonus_pct"] < was:
            b["deval_watch"] = True
            b["deval_note"] = f"pct dropped {was}% → {b['bonus_pct']}% since last fetch"
    res = {"bonuses": _bonus_expiry_sort(merged), "answered_by": answered_by or "static-file",
           "tried": tried, "cached": False,
           "last_checked": static.get("last_checked"),
           "note": "Fares never come from webfetch — bonus/promo pages only."}
    _cache_put(ckey, res)
    return res


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
        """Award seats. Intake-first: ask Juni for route + date (YYYY-MM-DD) first; without SEATS_AERO_API_KEY returns free deep-links + key paste guide (seats.aero/settings Pro ~$9.99/mo)."""
        return _award_search_impl(route, date)

    @mcp.tool()
    def award_calendar(route: str, date: str = None, dates: list = None,
                       window: int = 7, max_miles: int = 30000, home: str = None) -> dict:
        """±window saver scan. Intake-first: ask Juni for route + date/dates + home airport first; key optional (free links + density without it)."""
        return _award_calendar_impl(route, date, dates, window, max_miles, home)

    @mcp.tool()
    def award_watch(route: str, date: str, max_miles: int = 30000,
                    program: str = None, action: str = "check",
                    remind_every_h: float = 24.0) -> dict:
        """Check-on-query award watch. Intake-first: ask Juni for route + date + mile ceiling + program first. Hit attaches live bonuses. No daemon."""
        return _award_watch_impl(route, date, max_miles, program, action, remind_every_h)

    @mcp.tool()
    def award_vs_cash(route: str, date: str, program: str) -> dict:
        """Cheapest cash vs program award cards. Intake-first: ask Juni for route + date + program first; DUFFEL_API_KEY_LIVE optional (scrape fallback otherwise)."""
        return _award_vs_cash_impl(route, date, program)

    @mcp.tool()
    def delta_scan(route: str, date: str, max_miles: int = 30000) -> dict:
        """Delta cookie-replay saver scan. Intake-first: ask Juni for route + date first; needs DELTA_CURL_FILE paste (devtools cURL of rm-offer-gql, cookies expire ~30 min)."""
        return _award_delta_scan(route, date, max_miles)

    @mcp.tool()
    def search_legs(legs: list, cabin: str = "economy", adults: int = 1) -> dict:
        """Multi-city: cheapest cash per {o,d,date} leg + trip total."""
        return _search_legs_impl(legs, cabin, adults)

    @mcp.tool()
    def fetch_bonus(program: str = None, refresh: bool = False) -> dict:
        """Live transfer bonuses: SearXNG → Jina (keyless) → static file. Bonus pages only, never fares."""
        return _fetch_bonus_impl(program, refresh)


if __name__ == "__main__" and mcp:
    mcp.run()
