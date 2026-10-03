# flight-rank skill

Rank flights by price/timing + miles transfer value. Free data only.

## Flow

1. Parse query → `origin` (IATA), `dest` (IATA), `date` (YYYY-MM-DD), `cabin`, `adults`.
   If missing/ambiguous, ask once (no guessing airports).
2. Call `search_flights(origin, dest, date, cabin, adults)`.
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

## Limits

- No live miles API exists; transfer ratios are static (`data/transfer_partners.json`).
- Scraper can break when Google changes markup → SerpAPI backstop needs `SERPAPI_KEY`.
- Dates must be exact; this skill does not do flexible-date search.
