"""
اختبارات طبقة البيانات — الـMapper والـData Logger.
بتستخدم عينة JSON شكلها زي رد DexScreener الحقيقي، من غير أي اتصال شبكة فعلي.

تشغيل: python3 tests/test_data_layer.py
"""

import os
import sqlite3
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.data.data_logger import DataLogger
from core.data.dexscreener_client import DexScreenerClient
from core.data.mappers import build_dex_fields, pair_to_timing_signals


def sample_pair(**overrides) -> dict:
    now_ms = int(time.time() * 1000)
    base = {
        "chainId": "solana",
        "baseToken": {"address": "SoMeMeToken111", "symbol": "MEME", "name": "MemeCoin"},
        "quoteToken": {"address": "So11111111111111111111111111111111111111112", "symbol": "SOL"},
        "priceUsd": "0.0012",
        "priceChange": {"m5": 4.2, "h1": 18.5, "h6": 30.0, "h24": 55.0},
        "liquidity": {"usd": 45000, "base": 1000000, "quote": 200},
        "marketCap": 320000,
        "fdv": 340000,
        "volume": {"m5": 3200, "h1": 28000, "h6": 90000, "h24": 210000},
        "txns": {
            "m5": {"buys": 22, "sells": 9},
            "h1": {"buys": 180, "sells": 95},
        },
        "pairCreatedAt": now_ms - (600 * 60 * 1000),  # عملها 600 دقيقة (10 ساعات)
    }
    base.update(overrides)
    return base


# ---------------- Mapper tests ----------------

def test_pair_to_timing_signals_maps_basic_fields():
    signals = pair_to_timing_signals(sample_pair())
    assert signals.symbol == "MEME"
    assert signals.address == "SoMeMeToken111"
    assert signals.price_change_5m_pct == 4.2
    assert signals.buys_last_5m == 22
    assert signals.sells_last_5m == 9


def test_pair_to_timing_signals_approximates_15m_between_5m_and_1h():
    signals = pair_to_timing_signals(sample_pair())
    # التقريب لازم يقع بين قيمة الـm5 والـh1 (مش أكبر من الاتنين ولا أصغر منهم)
    assert min(4.2, 18.5) <= signals.price_change_15m_pct <= max(4.2, 18.5)


def test_pair_to_timing_signals_breakout_defaults_safe():
    signals = pair_to_timing_signals(sample_pair())
    # مفيش تاريخ أسعار = مفيش breakout حقيقي يتحسب، القيمة الافتراضية آمنة
    assert signals.broke_recent_high is False


def test_build_dex_fields_extracts_available_metrics():
    fields = build_dex_fields(sample_pair())
    assert fields["symbol"] == "MEME"
    assert fields["liquidity_usd"] == 45000
    assert fields["market_cap_usd"] == 320000
    assert fields["volume_24h_usd"] == 210000
    assert fields["buys_last_hour"] == 180
    assert fields["sells_last_hour"] == 95
    # عمر التوكن قريب من 600 دقيقة (سمحنا بهامش بسيط لوقت تنفيذ الاختبار)
    assert 595 <= fields["token_age_minutes"] <= 605


def test_build_dex_fields_does_not_include_onchain_only_fields():
    fields = build_dex_fields(sample_pair())
    # التأكيد إننا مانخترعش بيانات مش موجودة فعليًا في DexScreener
    assert "top_holder_pct" not in fields
    assert "is_sellable" not in fields
    assert "mint_authority_active" not in fields


def test_missing_pair_created_at_defaults_to_zero_age():
    pair = sample_pair()
    del pair["pairCreatedAt"]
    fields = build_dex_fields(pair)
    assert fields["token_age_minutes"] == 0.0


# ---------------- Data Logger tests ----------------

class _FakeClient:
    """بديل بسيط لـDexScreenerClient عشان نختبر من غير شبكة فعلية."""
    def __init__(self, pairs_to_return):
        self._pairs = pairs_to_return

    def get_pairs_by_token_addresses(self, token_addresses):
        return self._pairs


def test_data_logger_snapshot_and_export_ohlc():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "test.db")
        logger = DataLogger(db_path=db_path)
        logger.init_db()

        fake_client = _FakeClient([sample_pair(priceUsd="0.0010")])
        inserted = logger.snapshot_pairs(fake_client, ["SoMeMeToken111"])
        assert inserted == 1

        fake_client2 = _FakeClient([sample_pair(priceUsd="0.0015")])
        logger.snapshot_pairs(fake_client2, ["SoMeMeToken111"])

        count = logger.snapshot_count_for("SoMeMeToken111")
        assert count == 2

        candles = logger.export_ohlc("SoMeMeToken111", interval_minutes=60)
        assert len(candles) >= 1
        # لازم يكون شاف السعرين اللي دخلناهم
        all_prices_seen = [c.open for c in candles] + [c.close for c in candles]
        assert any(abs(p - 0.0010) < 1e-9 for p in all_prices_seen) or \
               any(abs(p - 0.0015) < 1e-9 for p in all_prices_seen)


def test_data_logger_empty_pairs_inserts_nothing():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "test.db")
        logger = DataLogger(db_path=db_path)
        logger.init_db()

        fake_client = _FakeClient([])
        inserted = logger.snapshot_pairs(fake_client, ["SoMeMeToken111"])
        assert inserted == 0
        assert logger.snapshot_count_for("SoMeMeToken111") == 0


def test_dexscreener_client_rejects_over_30_addresses():
    client = DexScreenerClient()
    too_many = [f"addr{i}" for i in range(31)]
    try:
        client.get_pairs_by_token_addresses(too_many)
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
