# flight-rank
Free flight-deals ranker: live prices via free APIs + MCP, miles/credit-card transfer math, ranked best options. Agent-first (OpenCode skill + local MCP).

`git clone + ./install.sh` wires a `flight-rank` MCP + skill into OpenCode on any Linux/Mac. No paid keys required.

## Free stack (and limits)

- **Live prices:** [`faster-flights`](https://pypi.org/project/faster-flights/) (fork of `fast-flights`) — $0, no key, live Google Flights scrape. Pinned in `requirements.txt`.
- **Fallback:** SerpAPI Google Flights (250 free/mo, optional `SERPAPI_KEY`) — price insights + backstop when the scraper is throttled. Without a key, empty results return a Google Flights deep-link.
- **Miles:** static data files (no live miles API exists — hardcoded by design):
  - `data/transfer_partners.json` (45 rows: Chase UR 10, Amex MR 17, CapOne 18 + alliances/ratios)
  - `data/transfer_bonuses.json` (active transfer bonuses `{from,to,pct,end_date,source}` — refresh weekly)
  - `data/cpp.json` (TPG-style cents-per-point: Bilt 2.2, Chase UR 2.05, Amex MR 2.0, Citi 1.9, CapOne 1.85, Hyatt 1.55, Marriott 0.8, Hilton 0.35 — illustrative snapshot, re-verify against current TPG valuations)
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

## Weekly refresh

- `data/transfer_bonuses.json` — bonuses expire fast. Check Roame/AwardTravelFinder/Going weekly: update `{from,to,pct,end_date,source}`, drop expired, bump `last_checked`.
- `data/cpp.json` — re-check against current TPG valuations when they move, bump `last_verified`.

## Fallback / ToS notes

- Scraper can break when Google changes markup (retry + exponential backoff built in, 1-hr `/tmp` cache). SerpAPI is the backstop.
- Scraping Google Flights may violate their ToS for heavy/automated use — light personal use + SerpAPI for anything sustained.
- Hidden-city ticketing violates most airlines' contract of carriage — the tool surfaces the caveat, never hides it.
