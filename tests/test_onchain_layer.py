"""
اختبارات On-chain layer — RugCheck mapper + الدمج النهائي مع DexScreener.

تشغيل: python3 tests/test_onchain_layer.py
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.data.mappers import build_dex_fields
from core.data.onchain_mappers import build_onchain_fields, merge_into_token_metrics
from core.data.rugcheck_client import RugCheckClient
from tests.test_data_layer import sample_pair


def clean_report(**overrides) -> dict:
    base = {
        "mint": "SoMeMeToken111",
        "mintAuthority": None,     # None = revoked = آمن
        "freezeAuthority": None,
        "totalHolders": 850,
        "topHolders": [
            {"address": "poolAddr1", "pct": 40.0},   # ده الـLP نفسه، لازم يتستبعد
            {"address": "wallet1", "pct": 6.0},
            {"address": "wallet2", "pct": 4.5},
            {"address": "wallet3", "pct": 3.0},
        ],
        "markets": [
            {"lp": {"holders_addresses": ["poolAddr1"], "lpLockedPct": 100.0}}
        ],
        "risks": [
            {"name": "Mutable metadata", "level": "warn"},
        ],
    }
    base.update(overrides)
    return base


def risky_report(**overrides) -> dict:
    base = clean_report(
        mintAuthority="StillActiveAuthorityAddress",
        freezeAuthority="StillActiveFreezeAddress",
        risks=[
            {"name": "Honeypot - Cannot sell tokens", "level": "danger"},
            {"name": "High transfer tax detected", "level": "danger"},
        ],
        markets=[{"lp": {"holders_addresses": [], "lpLockedPct": 0.0}}],
    )
    base.update(overrides)
    return base


# ---------------- RugCheck mapper tests ----------------

def test_clean_token_maps_to_safe_onchain_fields():
    fields = build_onchain_fields(clean_report())
    assert fields["mint_authority_active"] is False
    assert fields["freeze_authority_active"] is False
    assert fields["liquidity_locked_or_burned"] is True
    assert fields["critical_flags"] == []
    assert fields["is_sellable"] is True


def test_pool_address_excluded_from_holder_concentration():
    fields = build_onchain_fields(clean_report())
    # أكبر holder حقيقي هو wallet1 بـ6%، مش الـpool بـ40%
    assert fields["top_holder_pct"] == 6.0


def test_top_holder_addresses_excludes_pool_and_sorted_descending():
    fields = build_onchain_fields(clean_report())
    assert "poolAddr1" not in fields["top_holder_addresses"]
    assert fields["top_holder_addresses"][0] == "wallet1"  # الأعلى نسبة (6%) الأول
    assert fields["top_holder_addresses"] == ["wallet1", "wallet2", "wallet3"]


def test_active_mint_and_freeze_authority_detected():
    fields = build_onchain_fields(risky_report())
    assert fields["mint_authority_active"] is True
    assert fields["freeze_authority_active"] is True


def test_honeypot_risk_sets_critical_flag_and_blocks_sellable():
    fields = build_onchain_fields(risky_report())
    assert "honeypot_detected" in fields["critical_flags"]
    assert fields["is_sellable"] is False


def test_transfer_tax_risk_flagged_critical():
    fields = build_onchain_fields(risky_report())
    assert "transfer_restrictions_detected" in fields["critical_flags"]


def test_unlocked_lp_not_marked_locked():
    fields = build_onchain_fields(risky_report())
    assert fields["liquidity_locked_or_burned"] is False


def test_no_top_holders_data_returns_zero_concentration():
    report = clean_report(topHolders=[])
    fields = build_onchain_fields(report)
    assert fields["top_holder_pct"] == 0.0
    assert fields["top10_holders_pct"] == 0.0


# ---------------- Merge tests ----------------

def test_merge_produces_complete_token_metrics():
    dex_fields = build_dex_fields(sample_pair())
    onchain_fields = build_onchain_fields(clean_report())
    metrics = merge_into_token_metrics(dex_fields, onchain_fields, buys_last_hour=180)

    assert metrics.symbol == "MEME"
    assert metrics.liquidity_usd == 45000
    assert metrics.mint_authority_active is False
    assert metrics.top_holder_pct == 6.0
    assert metrics.unique_buyers_last_hour == 180


def test_merged_metrics_feed_directly_into_scanner():
    """اختبار تكاملي: تأكيد إن المخرجات فعلًا تشتغل مع ScannerEngine."""
    from core.scanner import ScannerEngine

    config_path = os.path.join(os.path.dirname(__file__), "..", "config", "scanner_config.yaml")
    scanner = ScannerEngine(config_path=config_path)

    dex_fields = build_dex_fields(sample_pair(
        liquidity={"usd": 80000, "base": 1000000, "quote": 200},
        volume={"m5": 3200, "h1": 28000, "h6": 90000, "h24": 120000},
    ))
    onchain_fields = build_onchain_fields(clean_report())
    metrics = merge_into_token_metrics(dex_fields, onchain_fields, buys_last_hour=140)

    result = scanner.scan(metrics)
    assert result.passed_hard_filters


def test_risky_token_merged_metrics_rejected_by_scanner():
    from core.scanner import ScannerEngine

    config_path = os.path.join(os.path.dirname(__file__), "..", "config", "scanner_config.yaml")
    scanner = ScannerEngine(config_path=config_path)

    dex_fields = build_dex_fields(sample_pair())
    onchain_fields = build_onchain_fields(risky_report())
    metrics = merge_into_token_metrics(dex_fields, onchain_fields, buys_last_hour=180)

    result = scanner.scan(metrics)
    assert not result.passed_hard_filters


def test_rugcheck_client_authenticated_gets_higher_rate_limit():
    client_no_key = RugCheckClient()
    client_with_key = RugCheckClient(api_key="fake-key-for-limit-test")
    assert client_with_key._limiter.max_per_minute > client_no_key._limiter.max_per_minute


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
