# flight-rank
Free flight-deals ranker: live prices via free APIs + MCP, miles/credit-card transfer math, ranked best options. Agent-first (OpenCode skill + local MCP).

`git clone + ./install.sh` wires a `flight-rank` MCP + skill into OpenCode on any Linux/Mac. No paid keys required.

## Free stack (and limits)

- **Live prices:** [`faster-flights`](https://pypi.org/project/faster-flights/) (fork of `fast-flights`) — $0, no key, live Google Flights scrape. Pinned in `requirements.txt`.
- **Fallback:** SerpAPI Google Flights (250 free/mo, optional `SERPAPI_KEY`) — price insights + backstop when the scraper is throttled. Without a key, empty results return a Google Flights deep-link.
- **Miles:** static `data/transfer_partners.json` (45 rows: Chase UR 10, Amex MR 17, CapOne 18 + alliances/ratios) and `data/transfer_bonuses.json` (empty template + where to check). No live miles API exists — hardcoded by design.
- **Not used:** Amadeus (free tier shut 2026-07-17 — every Amadeus MCP is a dead backend).

## Install

```bash
git clone https://github.com/0124212/flight-rank && cd flight-rank && ./install.sh
```

What it does (idempotent): `pip install -r requirements.txt`, merges a `flight-rank` stdio MCP entry into `~/.config/opencode/opencode.json`, copies the skill to `~/.config/opencode/skills/flight-rank/SKILL.md` (+ legacy `skill/` path). `--dry-run` previews without changing anything. Never prints secrets.

## Use

Ask your agent: `ICN → NRT 2026-11-20 economy, rank cash vs Chase UR`.

Agent flow (see `.opencode/skills/flight-rank/SKILL.md`): `search_flights(origin, dest, date, cabin, adults)` → `rank(options, prefs)` (cash vs miles × 1.3¢ + taxes, bonus flag) → ranked cards with deep-links.

```python
# direct (no agent) smoke test:
from mcp_server.server import _search_impl, _rank_impl
res = _search_impl("ICN", "NRT", "2026-11-20")
print(_rank_impl(res["options"], {"max_stops": 1})[:3])
```

## Fallback / ToS notes

- Scraper can break when Google changes markup (retry + exponential backoff built in, 1-hr `/tmp` cache). SerpAPI is the backstop.
- Scraping Google Flights may violate their ToS for heavy/automated use — light personal use + SerpAPI for anything sustained.
