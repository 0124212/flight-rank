# flight-rank
Free flight-deals ranker: live prices via free APIs + MCP, miles/credit-card transfer math, ranked best options. Agent-first (OpenCode skill + local MCP).

**Version: v0.5.0** (see [CHANGELOG.md](CHANGELOG.md)). Zero keys required for everything except the three optional legs: SerpAPI fallback (`SERPAPI_KEY`), award search (`SEATS_AERO_API_KEY`).

`git clone + ./install.sh` wires a `flight-rank` MCP + skill into OpenCode on any Linux/Mac. No paid keys required.

## Free stack (and limits)

- **Live prices:** [`faster-flights`](https://pypi.org/project/faster-flights/) (fork of `fast-flights`) — $0, no key, live Google Flights scrape. Pinned in `requirements.txt`.
- **Cross-check:** [Skiplagged public MCP](https://mcp.skiplagged.com/mcp) (`sk_flights_search`, hidden-city included) — zero key, passthrough via `rank_compare`. Disputed when sources disagree >10%.
- **Fallback:** SerpAPI Google Flights (250 free/mo, optional `SERPAPI_KEY`) — price insights + backstop when the scraper is throttled. Without a key, empty results return a Google Flights deep-link.
- **Miles:** static data files (no live miles API exists — hardcoded by design):
  - `data/transfer_partners.json` (45 rows: Chase UR 10, Amex MR 17, CapOne 18 + alliances/ratios)
  - `data/transfer_bonuses.json` (active transfer bonuses `{from,to,pct,end_date,source}` — refresh weekly)
  - `data/cpp.json` (TPG-style cents-per-point: Bilt 2.2, Chase UR 2.05, Amex MR 2.0, Citi 1.9, CapOne 1.85, Hyatt 1.55, Marriott 0.8, Hilton 0.35 — illustrative snapshot, re-verify against current TPG valuations)
  - `data/ontime_sample.json` (BTS-style delay/cancel sample for JFK-LAX + SFO-ORD corridors; `ontime_full.json` drop-in for full BTS coverage)
- **Cards:** live vendor JSON ([andenacitelli/credit-card-bonuses-api](https://github.com/andenacitelli/credit-card-bonuses-api), 175 cards, cached to `data/cards_cache.json`) with 3-card offline fallback; transferable currencies only, vendor flat rates capped at 2.5%. Static math, no affiliate links.
- **Award search (gated):** [seats.aero](https://seats.aero) Cached Search via optional `SEATS_AERO_API_KEY` (baeleb/seats-MCP pattern). Without the key: PointsYeah free manual steps, never an error.
- **Bonus lists (webfetch, bonus pages only — never fares):** Going / Roame / PointsToThe transfer-bonus guides via self-host SearXNG (`SEARXNG_URL`, default `http://localhost:8080`, optional) → Jina Reader keyless (default, 20 RPM free, 24h cache stays far under it) → static `transfer_bonuses.json` fallback. Parsed `{program, partner, bonus_pct, end_date}`, merged over static, degrades gracefully.
- **Not used:** Amadeus (free tier shut 2026-07-17 — every Amadeus MCP is a dead backend).

## Install

```bash
git clone https://github.com/0124212/flight-rank && cd flight-rank && chmod +x install.sh && ./install.sh
```

What it does (idempotent): `pip install -r requirements.txt`, merges a `flight-rank` stdio MCP entry into `~/.config/opencode/opencode.json`, copies the skill to `~/.config/opencode/skills/flight-rank/SKILL.md` (+ legacy `skill/` path). `--dry-run` previews without changing anything. Never prints secrets.

## Use

Ask your agent: `ICN → NRT 2026-11-20 economy, rank cash vs Chase UR`.

Agent flow (see `.opencode/skills/flight-rank/SKILL.md`): `search_flights(origin, dest, date, cabin, adults)` → `rank(options, prefs)` (cash vs miles × 1.3¢ + taxes, bonus flag) → ranked cards with deep-links.

```python
# direct (no agent) smoke test:
from mcp_server.server import _search_impl, _rank_impl, _cpp_impl, _bonus_watch_impl
res = _search_impl("ICN", "NRT", "2026-11-20")
print(_rank_impl(res["options"], {"max_stops": 1})[:3])
print(_cpp_impl(60000, "Chase UR", 900))   # points worth $1230 vs $900 cash → points
print(_bonus_watch_impl()["active"])       # check before any transfer
```

More examples:

- `cpp_value(50000, "amex", 1100)` — are 50k MR worth it vs $1,100 cash?
- `cheap_hack("ICN", ["NRT", "HND"], ["2026-11-20", "2026-11-21"])` — nearby-airport/date matrix + positioning hint. Add `flags: {split_ticket: true}` for separate-ticket caveats.
- `rank_compare("JFK", "LAX", "2026-11-20")` — faster-flights vs Skiplagged MCP cross-check; `disputed: true` means verify before booking.
- `price_signal("JFK-LAX", "2026-11-20")` — SerpAPI price insights (cached-first) + SQLite history; pass `disputed: true` after a disputed compare for a fresh pull.
- `delay_risk("AA", "JFK", "LAX", "7")` — misconnect probability; feed into `rank` via `{"apply_delay_penalty": true, ...}` to penalize tight connections.
- `price_watch("JFK", "LAX", "2026-11-20", 250)` — check-on-query fare watch (Skiplagged + SQLite trend). No daemon, no key.
- `card_pick({"dining": 500, "travel": 800, "groceries": 600, "other": 1500})` — top-3 cards by spend match from the FOSS card-bonuses API (cached to `data/cards_cache.json`, 3-card offline fallback). Static math, no affiliate links.
- `award_search("ICN-NRT", "2026-11-20")` — live award seats via seats.aero (**optional** `SEATS_AERO_API_KEY`; without it returns PointsYeah free manual steps, never an error).
- `fetch_bonus()` — fresh transfer bonuses, zero-key: SearXNG → Jina keyless → static file. `fetch_bonus("amex")` filters; `refresh=True` busts the 24h cache. `rank_compare` quotes its `bonus_hint` automatically when disputed.

## Weekly refresh

- Automated skeleton: `.github/workflows/refresh-data.yml` (Mondays 09:00 UTC + manual dispatch, no secrets) re-fetches the cards vendor JSON and prints the human checklist. It pushes nothing — review the diff, merge by hand.
- `data/transfer_bonuses.json` — bonuses expire fast. Check Roame/AwardTravelFinder/Going weekly: update `{from,to,pct,end_date,source}`, drop expired, bump `last_checked`. `fetch_bonus` covers the gap between refreshes with live reads (24h cache).
- `data/cpp.json` — re-check against current TPG valuations when they move, bump `last_verified`.
- `data/ontime_sample.json` → `ontime_full.json` (monthly refresh plan — extend the Action above):
  1. Monthly cron/white (GitHub Action): download the BTS Airline On-Time Performance PREZIP for the latest month.
  2. Aggregate by carrier + origin + dest + month: flight count, avg delay minutes, cancel share, misconnect proxy.
  3. Write `data/ontime_full.json` (same row shape as the sample), bump `last_verified`, open a PR.
  4. `_load_bts()` picks up `ontime_full.json` automatically when present; sample stays the fallback.

## Fallback / ToS notes

- Scraper can break when Google changes markup (retry + exponential backoff built in, 1-hr `/tmp` cache). SerpAPI is the backstop.
- Scraping Google Flights may violate their ToS for heavy/automated use — light personal use + SerpAPI for anything sustained.
- Hidden-city ticketing violates most airlines' contract of carriage — the tool surfaces the caveat, never hides it.
- Webfetch is bonus-pages-only by project rule (see constraint comment in `server.py`): Going/Roame/PointsToThe lists. Fares never go through webfetch.
