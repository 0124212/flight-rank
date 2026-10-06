# flight-rank skill

Rank flights by price/timing + miles transfer value. Free data only.

## Flow

1. Parse query → `origin` (IATA), `dest` (IATA), `date` (YYYY-MM-DD), `cabin`, `adults`.
   If missing/ambiguous, ask once (no guessing airports).
2. Call `search_flights(origin, dest, date, cabin, adults)`.
    - Intake-first: if the tools return `need_from_you`, ask Juni those exact fields first — never invent availability.
   - `source` tells you which leg served it (`faster-flights` | `serpapi`).
   - Empty `options` + `error` → give the `deep_link` and say the scraper is throttled.
3. Call `rank(options, prefs)` with:
   - `value_per_point_cents`: default 1.3 (ask if user values points differently).
   - `transfer_bonus_pct`: look up `data/transfer_bonuses.json` if a bonus applies.
   - `max_stops`: from user prefs (default: no filter).
4. Reply as ranked cards, cheapest-effective first:
   `airline · price · stops · duration · via cash|miles · Book: link`
   Flag when miles beat cash. Never invent prices — only rank what tools returned.

## More tools

- `cpp_value(points, program, cash_price)` — "are my 60k Chase UR worth it vs $900 cash?" Returns point value + buy-vs-points verdict. Program names are fuzzy (`chase`, `ur`, `amex` all work).
- `bonus_watch()` — active transfer bonuses. Always call before recommending a transfer; a +30% bonus changes the math. If the bonus you need expired, say so.
- `cheap_hack(origin, dests, dates, flags)` — nearby-airport/date matrix (e.g. dests `[NRT,HND]`, dates `[2026-11-20,2026-11-21]`). Returns cheapest cell + positioning hint. `flags: {hidden_city: true}` / `{split_ticket: true}` add contract-of-carriage caveats — never recommend skiplagging without the warning.

## Weekly refresh (data files)

- `data/transfer_bonuses.json` — bonuses expire. Refresh weekly: check Roame → update rows `{from,to,pct,end_date,source}`, drop expired, bump `last_checked`. Stale bonuses cost real points.
- `data/cpp.json` — TPG-style valuations drift. Re-check against current TPG valuations monthly-ish, bump `last_verified`.

## Accuracy trio (trust, but verify)

- `rank_compare(origin, dest, date)` — cross-checks faster-flights vs Skiplagged MCP in parallel. `disputed: true` (price diff >10% or depart times >60min apart on the same airline) means don't trust either number — run `price_signal(route, date, disputed=true)` for a fresh SerpAPI pull. Skiplagged down → single-source + note, still usable.
- `price_signal(route, date)` — SerpAPI `price_insights`, cached read first (free), fresh pull only on miss/dispute. History lands in SQLite (`/tmp/flight-rank/history.db`). No `SERPAPI_KEY` → skips gracefully, say so.
- `delay_risk(carrier, origin, dest, month)` — BTS-sample misconnect probability. For tight connections add `rank` prefs `{"apply_delay_penalty": true, "origin": ..., "dest": ..., "month": ...}` — score gains `misconnect_prob × $150` (tune via `delay_dollars`). Sample covers JFK-LAX + SFO-ORD corridors only; anything else falls back to route/global average and says so (`match` field).

## Watch + cards (zero keys)

- `price_watch(origin, dest, date, target_price)` — check-on-query fare watch (Skiplagged + fallback, SQLite trend in `/tmp/flight-rank/history.db`). No daemon: below-target → "book now", above → state current vs target and re-check next query. Trend (`moved $X → $Y`) only appears after 2+ checks.
- `card_pick(spend_profile)` — top-3 cards by monthly spend match, e.g. `{dining: 500, travel: 800, groceries: 600, other: 1500}`. Live vendor data (andenacitelli/credit-card-bonuses-api, cached) or 3-card offline fallback. Transferable currencies only; vendor flat rates capped at 2.5% and labeled. Static math, no affiliate links — verify bonuses before applying.

## Award search (intake-first, never breaks)

Ask Juni these 3 first when anything is missing (tools echo them back as `need_from_you` with paste instructions):
1. Route + dates — `ICN-LAX`, outbound `YYYY-MM-DD` (+ return for round trips), cabin (default economy).
2. Programs — which miles (aeroplan / united / delta / asiana); KE/OZ rows appear automatically on Korea routes.
3. Keys (all optional, free fallback always works) — `SEATS_AERO_API_KEY` (live saver scan; from seats.aero/settings Pro ~$9.99/mo), `DUFFEL_API_KEY_LIVE` (GDS cash anchor; duffel.com dashboard), `DELTA_CURL_FILE` (Delta replay; devtools Network → copy rm-offer-gql as cURL to a file, expires ~30 min).

- `award_search(route, date)` — live seats via seats.aero with key; without it, bookable deep links (AA award, Southwest points, Delta, PointsYeah) + which key to paste.
- `award_calendar(route, date|dates, window, max_miles, home)` — ±window saver scan + density grid + positioning leg from `home`; flexible-date search lives here.
- `award_watch(route, date, max_miles, program, remind_every_h)` — check-on-query watch; hits attach live bonuses + rarity label; notify throttled per cadence.
- `award_vs_cash(route, date, program)` — cheapest cash (Duffel anchor when keyed, scrape otherwise) vs program cards via cpp math.
- `delta_scan(route, date)` — Delta cookie-replay; without the curl file it tells Juni exactly what to paste.

## Live bonus fetch (webfetch: bonus pages only, never fares)

- `fetch_bonus(program=None, refresh=False)` — fresh transfer bonuses, zero-key cascade: self-host SearXNG (`SEARXNG_URL`, default `http://localhost:8080`) → Jina Reader keyless (default, 20 RPM — cached 24h so we never get near it) → static `transfer_bonuses.json`. Returns `{program, partner, bonus_pct, end_date}` + `answered_by`. Never raises.
- `rank_compare` auto-attaches `bonus_hint` when disputed (`Amex→BA +30% active, effective points price drops to ~X pts`) — quote it in the reply.
- Hard rule: webfetch touches bonus/promo lists ONLY. Fares always come from faster-flights / Skiplagged MCP / SerpAPI — scraped fare pages mislead.

## Limits

- No live miles API exists; transfer ratios are static (`data/transfer_partners.json`).
- Scraper can break when Google changes markup → SerpAPI backstop needs `SERPAPI_KEY`.
- Dates must be exact for cash search; award calendar does flexible ±window scans.
