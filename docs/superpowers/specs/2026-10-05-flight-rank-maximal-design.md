# flight-rank maximal award design (2026-10-05)

Goal: free-first award search. No-key path returns bookable deep links (done:
`_award_free_links`); keyed path stays seats.aero passthrough. Stdlib + httpx
only — no pandas/streamlit/selenium/curl_cffi hard deps. MIT sources only,
inline attribution, no GPL.

## Pack A — saver-scan (`award_calendar`)

- `_award_calendar_impl(route, date=None, dates=None, window=7, max_miles=30000)`:
  explicit `dates[]` wins; else center `date` ± `window` days (cap 31).
- Nearby expansion `_NEARBY` (pfei-sa expand_route pattern, static dict):
  ICN/GMP/SEL ↔ ICN,GMP · NRT/HND/TYO ↔ NRT,HND · JFK/EWR/LGA ↔ all three.
  Keyed loop = pairs × dates via `_award_search_impl` (1-hr `awardcal` cache).
- Saver flags `_saver_flag(program, miles)`: borski totals (AA Y <100K,
  Alaska <150K) override; else ≤ `max_miles` → `saver`, above → `dynamic`,
  missing → `unknown`. Never invent per-hour caps (no duration data).
- KE/OZ rows: `transfer_partners.json` gains Korean Air SKYPASS (Chase UR 1:1,
  SkyTeam) + Asiana Club (no direct US bank; Marriott 3:1, Star Alliance).
  Calendar attaches `earn_via` per program from that file.
- SkyTeam note when route touches ICN/GMP/SEL: KE awards also bookable via
  Delta / Virgin Atlantic / Flying Blue — compare before transferring.
- No key → per-date `free_links` + KE/OZ rows, `availability: []` (never fake).

## Pack B — watcher (`award_watch`, `award_vs_cash`)

- `award_watches(route, date, program, max_miles, last_hit, last_checked,
  PRIMARY KEY(route,date,program))` table in `_price_history_db` (same DB).
- `_award_watch_impl(..., action="check"|"add"|"list"|"remove")`: check-on-query
  only — **no daemon** (price_watch precedent). Hit (min miles ≤ max) attaches
  live `_fetch_bonus_impl(program=...)` matches before advising a transfer.
- `_award_vs_cash_impl(route, date, program)`: cheapest cash via `_search_impl`
  + program-filtered award cards, each scored by `_cpp_impl` (unknown program
  → miles + cash + manual-compare note, never an error).

## Pack C — scraper (finish Delta replay, AA ToS-safe)

- `_award_delta_scan` graduates stub → single-shot replay: `_delta_parse_curl`
  (verbatim `re` port of jeremyyma `parse_curl`), `_delta_payload` (one-way
  single-date port of `build_payload`), `_delta_extract` (port of
  `extract_results`, returns cards). curl_cffi stays soft-import; cookies
  ~30 min (message says so). Calendar drives repetition later, not a loop here.
- AA: deep-links only (`free_links.aa_award`, tszumowski `generate_url` logic).
  Selenium rejected (chromedriver weight + brittle 2023 selectors + unmaintained).
  ToS note lives in README, not code.

## Files touched (4 max)

1. `mcp_server/server.py` — all impls + 3 tool wrappers (award_calendar,
   award_watch, award_vs_cash). No signature changes to existing tools.
2. `data/transfer_partners.json` — KE/OZ rows + `last_verified` bump.
3. `README.md` — award section: new tools, Delta opt-in, AA ToS note.
4. This spec. No requirements.txt change (curl_cffi optional, documented).

## Test plan

`py_compile` · no-key calendar returns free_links per date, no availability ·
keyed calendar (stubbed httpx) flags saver/dynamic + cheapest-per-day · nearby
map unit checks (SEL→ICN/GMP, TYO→NRT/HND, NYC→3) · watch add→hit→remove with
stubbed search · vs_cash verdict math vs `_cpp_impl` · delta stub degrades
(no env / no curl_cffi) · keyed award_search passthrough unchanged.
