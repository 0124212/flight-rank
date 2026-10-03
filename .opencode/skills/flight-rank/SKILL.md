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

## Limits

- No live miles API exists; transfer ratios are static (`data/transfer_partners.json`).
- Scraper can break when Google changes markup → SerpAPI backstop needs `SERPAPI_KEY`.
- Dates must be exact; this skill does not do flexible-date search.
