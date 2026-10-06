"""No-key battery: 9/9 offline. No API keys, no network — every live leg is
stubbed or key-gated, so this runs on a clean box in seconds.

Run: pytest tests/test_no_key.py  (or: python tests/test_no_key.py)
"""
import py_compile
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import mcp_server.server as S  # noqa: E402

KEYS = ("SEATS_AERO_API_KEY", "SERPAPI_KEY", "DELTA_CURL_FILE", "DUFFEL_API_KEY_LIVE", "SEARXNG_URL")


@pytest.fixture(autouse=True)
def no_keys_and_throwaway_db(monkeypatch, tmp_path):
    for k in KEYS:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(S, "CACHE_DIR", tmp_path)  # throwaway SQLite + file cache
    yield


def test_1_py_compile():
    assert py_compile.compile(str(REPO / "mcp_server" / "server.py"), doraise=True)


def test_2_award_search_no_key():
    r = S._award_search_impl("ICN-NRT", "2026-11-20")
    assert r["source"] == "none" and r["availability"] == []
    assert r["free_links"]["aa_award"].startswith("https://www.aa.com/booking/search")
    assert any(n["field"] == "SEATS_AERO_API_KEY" for n in r["need_from_you"])
    assert "partner_search" in r["free_links"]  # KE/OZ partner caches need no key
    assert "star_note" in r  # ICN route surfaces OZ Star Alliance hints


def test_3_award_calendar_no_key():
    r = S._award_calendar_impl("ICN-NRT", "2026-11-20", window=1)
    assert sorted(r["by_day"]) == ["2026-11-19", "2026-11-20", "2026-11-21"]
    assert r["by_day"]["2026-11-20"]["free_links"]["southwest_points"].startswith("https://")
    assert len(r["density"]) == 3


def test_4_delta_scan_no_key():
    r = S._award_delta_scan("ICN-NRT", "2026-11-20")
    assert r["availability"] == [] and "DELTA_CURL_FILE absent" in r["message"]
    assert any(n["field"] == "DELTA_CURL_FILE" for n in r["need_from_you"])


def test_5_fetch_bonus_static(monkeypatch):
    monkeypatch.setattr(S, "_searxng_read", lambda url: (_ for _ in ()).throw(RuntimeError("offline")))
    monkeypatch.setattr(S, "_jina_read", lambda url: (_ for _ in ()).throw(RuntimeError("offline")))
    r = S._fetch_bonus_impl()
    assert r["answered_by"] == "static-file" and len(r["bonuses"]) >= 1


def test_6_vs_cash_no_key(monkeypatch):
    fake = {"source": "test", "options": [{"price": 800, "airline": "OZ",
                                           "stops": 0, "duration": "2h", "depart": "?",
                                           "arrive": "?", "link": "https://x"}]}
    monkeypatch.setattr(S, "_search_impl", lambda *a, **k: fake)
    r = S._award_vs_cash_impl("ICN-NRT", "2026-11-20", "aeroplan")
    assert r["cash_price_usd"] == 800 and r["cash_source"] == "test"
    assert r["rows"] == []  # no key → no award cards, cash stands alone


def test_7_watch_throwaway_db():
    assert S._award_watch_impl("ICN-NRT", "2026-11-20", 30000, "aeroplan", "add")["watching"] is True
    chk = S._award_watch_impl("ICN-NRT", "2026-11-20", 30000, "aeroplan", "check")
    assert chk["hit"] is False  # no key → no availability, never raises
    assert len(S._award_watch_impl("ICN-NRT", "2026-11-20", program="aeroplan", action="list")["watches"]) == 1
    assert S._award_watch_impl("ICN-NRT", "2026-11-20", program="aeroplan", action="remove")["watching"] is False


def test_8_cpp_static_math():
    r = S._cpp_impl(60000, "Chase UR", 900)
    assert r["verdict"] == "points" and r["points_value_usd"] == 1230.0


def test_9_price_signal_no_key():
    r = S._price_signal_impl("JFK-LAX", "2026-11-20")
    assert r["signal"] == "none"
    assert any(n["field"] == "SERPAPI_KEY" for n in r["need_from_you"])


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
