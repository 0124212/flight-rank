# Changelog

## v0.5.0 — award_search (gated) + refresh automation + changelog
- `award_search(route, date)`: seats.aero Cached Search passthrough, gated on `SEATS_AERO_API_KEY`. No key → clear message + PointsYeah free 4-day manual cross-check steps. Never breaks without the key.
- `.github/workflows/refresh-data.yml`: weekly skeleton re-fetching the cards vendor JSON + reminding cpp/bonuses refresh (manual dispatch, no secrets).
- README version notes; this file.

## v0.4.0 — price_watch + card_pick (zero keys)
- `price_watch(origin, dest, date, target_price)`: check-on-query fare watch (Skiplagged + fallback, SQLite trend).
- `card_pick(spend_profile)`: top-3 cards from andenacitelli/credit-card-bonuses-api (cached, offline samples, transferable-only, 2.5% flat cap).

## v0.3.0 — accuracy trio
- `rank_compare` (faster-flights vs Skiplagged MCP, disputed flag), `price_signal` (SerpAPI cached-first + SQLite), `delay_risk` (BTS sample + rank penalty). `data/ontime_sample.json`.

## v0.2.0 — cpp_value + bonus_watch + cheap_hack
- `cpp_value`, `bonus_watch`, `cheap_hack` matrix. `data/cpp.json`, real bonus rows.

## v0.1.0 — scaffold
- `search_flights` (faster-flights → SerpAPI), `rank` (cash vs miles). `data/transfer_partners.json`. `install.sh` + skill + README.
