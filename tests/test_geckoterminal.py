"""
اختبارات GeckoTerminal — mappers (OHLCV + pools) بعينة JSON:API واقعية،
من غير أي اتصال شبكة فعلي.

تشغيل: python3 tests/test_geckoterminal.py
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.data.geckoterminal_client import GeckoTerminalClient
from core.data.geckoterminal_mappers import ohlcv_response_to_candles, pools_response_to_candidates


# ---------------- OHLCV mapper tests ----------------

def sample_ohlcv_response():
    return {
        "data": {
            "id": "pool_ohlcv",
            "type": "ohlcv_request_response",
            "attributes": {
                "ohlcv_list": [
                    [1700000600, 0.0012, 0.0013, 0.0011, 0.00125, 5000],
                    [1700000300, 0.0010, 0.0012, 0.0010, 0.0012, 3000],
                    [1700000000, 0.0009, 0.0011, 0.0009, 0.0010, 2000],
                ]
            },
        }
    }


def test_ohlcv_maps_to_candles():
    candles = ohlcv_response_to_candles(sample_ohlcv_response(), pool_address="Pool111")
    assert len(candles) == 3
    assert all(c.address == "Pool111" for c in candles)


def test_ohlcv_sorted_oldest_to_newest():
    candles = ohlcv_response_to_candles(sample_ohlcv_response(), pool_address="Pool111")
    timestamps = [c.interval_start_ts for c in candles]
    assert timestamps == sorted(timestamps)
    assert timestamps[0] == 1700000000  # الأقدم أول


def test_ohlcv_fields_mapped_correctly():
    candles = ohlcv_response_to_candles(sample_ohlcv_response(), pool_address="Pool111")
    first = candles[0]  # أقدم شمعة
    assert first.open == 0.0009
    assert first.high == 0.0011
    assert first.low == 0.0009
    assert first.close == 0.0010
    assert first.volume_usd == 2000


def test_ohlcv_empty_response_returns_empty_list():
    assert ohlcv_response_to_candles({}, pool_address="Pool111") == []
    assert ohlcv_response_to_candles({"data": {}}, pool_address="Pool111") == []


def test_ohlcv_malformed_entry_skipped():
    response = {"data": {"attributes": {"ohlcv_list": [[1700000000, 0.001]]}}}  # ناقصة
    candles = ohlcv_response_to_candles(response, pool_address="Pool111")
    assert candles == []


# ---------------- Pools (discovery) mapper tests ----------------

def sample_pools_response():
    return {
        "data": [
            {
                "id": "solana_PoolAddr1",
                "type": "pool",
                "attributes": {
                    "address": "PoolAddr1",
                    "reserve_in_usd": "45000.0",
                    "volume_usd": {"m5": "1200.0", "h1": "9000.0"},
                    "pool_created_at": "2026-09-26T10:00:00Z",
                },
                "relationships": {
                    "base_token": {"data": {"id": "solana_TokenAddr1", "type": "token"}},
                },
            }
        ],
        "included": [
            {
                "id": "solana_TokenAddr1",
                "type": "token",
                "attributes": {"address": "TokenAddr1", "symbol": "MEME", "name": "MemeCoin"},
            }
        ],
    }


def test_pools_response_extracts_token_address_and_symbol():
    candidates = pools_response_to_candidates(sample_pools_response())
    assert len(candidates) == 1
    assert candidates[0]["address"] == "TokenAddr1"
    assert candidates[0]["symbol"] == "MEME"


def test_pools_response_extracts_liquidity_and_volume():
    candidates = pools_response_to_candidates(sample_pools_response())
    assert candidates[0]["liquidity_usd"] == 45000.0
    assert candidates[0]["volume_5m_usd"] == 1200.0


def test_pools_response_parses_created_at_to_ms():
    candidates = pools_response_to_candidates(sample_pools_response())
    assert candidates[0]["pool_created_at_ms"] is not None
    assert candidates[0]["pool_created_at_ms"] > 0


def test_pools_response_missing_included_falls_back_to_id_parsing():
    response = sample_pools_response()
    response["included"] = []  # مفيش token metadata خالص
    candidates = pools_response_to_candidates(response)
    assert candidates[0]["address"] == "TokenAddr1"  # لسه قادر يستخرجه من الـid
    assert candidates[0]["symbol"] == "UNKNOWN"


def test_empty_pools_response_returns_empty_list():
    assert pools_response_to_candidates({}) == []
    assert pools_response_to_candidates({"data": []}) == []


# ---------------- Client sanity tests ----------------

def test_client_rejects_ohlcv_limit_over_1000():
    client = GeckoTerminalClient()
    try:
        client.get_pool_ohlcv("PoolAddr1", limit=5000)
        assert False, "expected ValueError"
    except ValueError:
        pass


if __name__ == "__main__":
    import traceback

    tests = [obj for name, obj in list(globals().items()) if name.startswith("test_")]
    passed, failed = 0, 0
    for t in tests:
        try:
            t()
            print(f"PASS: {t.__name__}")
            passed += 1
        except Exception:
            print(f"FAIL: {t.__name__}")
            traceback.print_exc()
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
