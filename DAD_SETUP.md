# Dad setup — 5 steps, ~2 min, zero keys needed

1. **Download:** grab `flight-rank-dad.zip` (from GitHub release) and unzip it anywhere.
2. **Run:** open a terminal in that folder and type `./install.sh --wizard` — press **Enter** to skip every key, then **Y** when it asks to run the smoke test. (Windows: use git-bash or WSL with python 3.10+ installed.)
3. **Success looks like:** `9 passed` at the end. If you see that, you're done.
4. **Use it:** in OpenCode ask `award_search ICN-NRT 2026-11-20` — with no key you get bookable deep-links instead of live seats: aa.com award search, PointsYeah, and the Roame guide; click those to book/check seats by hand.
5. **Ignore:** all keys are optional. Add a live key later only if you want it (`SEATS_AERO_API_KEY` for live award seats, `SERPAPI_KEY` for fare fallback) — copy `.env.example` to `.env` and fill just that line.
