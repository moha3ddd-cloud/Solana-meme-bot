"""
اختبارات الـScanner — تأكيد إن العملات الخطرة بتتاستبعد فعلًا،
والعملات الصحية بتاخد Score عالي.

تشغيل: python3 tests/test_scanner.py
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.scanner import ScannerEngine, TokenMetrics

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config", "scanner_config.yaml")


def fresh_scanner() -> ScannerEngine:
    return ScannerEngine(config_path=CONFIG_PATH)


def healthy_token(**overrides) -> TokenMetrics:
    """عملة 'صحية' افتراضيًا — كل حقل ممكن نغيره في كل اختبار."""
    base = dict(
        symbol="GOOD",
        address="Gxxx111",
        liquidity_usd=80000,
        market_cap_usd=600000,
        volume_24h_usd=120000,
        volume_1h_usd=8000,
        token_age_minutes=600,
        top_holder_pct=6.0,
        top10_holders_pct=30.0,
        holder_count=850,
        holder_count_1h_ago=800,
        liquidity_locked_or_burned=True,
        mint_authority_active=False,
        freeze_authority_active=False,
        is_sellable=True,
        buy_tax_pct=1.0,
        sell_tax_pct=1.0,
        buys_last_hour=140,
        sells_last_hour=90,
        unique_buyers_last_hour=95,
        critical_flags=[],
        warning_flags=[],
    )
    base.update(overrides)
    return TokenMetrics(**base)


def test_healthy_token_passes_and_scores_well():
    scanner = fresh_scanner()
    result = scanner.scan(healthy_token())
    assert result.passed_hard_filters
    assert result.score is not None and result.score >= 70
    assert result.eligible_for_entry


def test_low_liquidity_rejected_before_scoring():
    scanner = fresh_scanner()
    result = scanner.scan(healthy_token(liquidity_usd=2000))
    assert not result.passed_hard_filters
    assert result.score is None
    assert any("liquidity_below_min" in r for r in result.rejection_reasons)


def test_mint_authority_active_rejected():
    scanner = fresh_scanner()
    result = scanner.scan(healthy_token(mint_authority_active=True))
    assert not result.passed_hard_filters
    assert "mint_authority_still_active" in result.rejection_reasons


def test_honeypot_not_sellable_rejected():
    scanner = fresh_scanner()
    result = scanner.scan(healthy_token(is_sellable=False))
    assert not result.passed_hard_filters
    assert "not_sellable_honeypot_risk" in result.rejection_reasons


def test_critical_flag_rejects_regardless_of_other_metrics():
    """حتى لو كل حاجة تانية ممتازة، critical flag = استبعاد فوري."""
    scanner = fresh_scanner()
    result = scanner.scan(healthy_token(critical_flags=["honeypot_detected"]))
    assert not result.passed_hard_filters
    assert "critical_flag:honeypot_detected" in result.critical_flags_triggered


def test_high_holder_concentration_rejected():
    scanner = fresh_scanner()
    result = scanner.scan(healthy_token(top_holder_pct=25.0))
    assert not result.passed_hard_filters


def test_token_too_new_rejected():
    scanner = fresh_scanner()
    result = scanner.scan(healthy_token(token_age_minutes=5))
    assert not result.passed_hard_filters


def test_borderline_token_scores_lower_but_still_passes_filters():
    scanner = fresh_scanner()
    result = scanner.scan(healthy_token(
        top_holder_pct=13.0,
        top10_holders_pct=40.0,
        volume_24h_usd=15000,  # نشاط ضعيف نسبيًا
    ))
    assert result.passed_hard_filters
    assert result.score is not None
    # المفروض السكور يقل عن العملة المثالية
    assert result.score < 90


def test_warning_flags_reduce_score():
    scanner = fresh_scanner()
    clean = scanner.scan(healthy_token())
    flagged = scanner.scan(healthy_token(warning_flags=["abnormal_wash_trading_pattern"]))
    assert flagged.score < clean.score


def test_wash_trading_volume_pattern_scores_lower_than_healthy_volume():
    scanner = fresh_scanner()
    normal = scanner.scan(healthy_token())
    # حجم تداول أعلى من السيولة بأضعاف كتير = مشبوه
    suspicious = scanner.scan(healthy_token(volume_24h_usd=2_000_000))
    assert suspicious.score_breakdown["volume_activity"] < normal.score_breakdown["volume_activity"]


def test_scanner_without_tracker_uses_neutral_smart_money_score():
    """من غير tracker، إشارة الـsmart money حيادية دايمًا (مش بتأثر سلبًا ولا إيجابًا)."""
    scanner = fresh_scanner()  # مفيش wallet_reputation_tracker
    result = scanner.scan(healthy_token(top_holder_addresses=["SomeWallet111"]))
    assert result.score is not None


def test_scanner_with_good_wallet_history_boosts_score():
    """
    بعد الإصلاح: من غير تاريخ حقيقي، wallet_reputation بترجع negative_signal_score
    زي القديم بالظبط (سقفها 100 لو مفيش warning flags). عشان نشوف تأثير
    smart money فعليًا، لازم نبدأ بعملة عندها penalty (warning flag)
    زي dev_wallet_selling — كده فيه مساحة يظهر فيها تحسّن السمعة.
    """
    from core.data.wallet_reputation import WalletReputationTracker
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        tracker = WalletReputationTracker(db_path=f"{tmp}/wallet_rep.db")
        for _ in range(5):
            tracker.record_trade_outcome(["ProvenGoodWallet"], win=True, pnl_usd=50.0)

        scanner_with_tracker = ScannerEngine(config_path=CONFIG_PATH, wallet_reputation_tracker=tracker)
        scanner_without = fresh_scanner()

        token = healthy_token(top_holder_addresses=["ProvenGoodWallet"], warning_flags=["dev_wallet_selling"])
        result_with = scanner_with_tracker.scan(token)
        result_without = scanner_without.scan(token)

        assert result_with.score_breakdown["wallet_reputation"] > result_without.score_breakdown["wallet_reputation"]


def test_scanner_without_wallet_history_matches_pre_smart_money_behavior():
    """
    أهم اختبار في الإصلاح: عملة من غير أي تاريخ محافظ (الحالة الشائعة
    في البداية) المفروض تاخد وزن wallet_reputation زي بالظبط لو مفيش
    smart money system أصلًا — مفيش "خصم لمجرد عدم وجود بيانات".
    """
    scanner = fresh_scanner()
    token = healthy_token(top_holder_addresses=["NeverSeenWallet"])  # مفيش tracker + عنوان مش معروف
    result = scanner.scan(token)
    assert result.score_breakdown["wallet_reputation"] == 100.0  # مفيش warning flags = 100 كاملة، مش 75


def test_scanner_with_bad_wallet_history_lowers_score():
    from core.data.wallet_reputation import WalletReputationTracker
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        tracker = WalletReputationTracker(db_path=f"{tmp}/wallet_rep.db")
        for _ in range(5):
            tracker.record_trade_outcome(["ProvenBadWallet"], win=False, pnl_usd=-50.0)

        scanner_with_tracker = ScannerEngine(config_path=CONFIG_PATH, wallet_reputation_tracker=tracker)
        scanner_without = fresh_scanner()

        token = healthy_token(top_holder_addresses=["ProvenBadWallet"])
        result_with = scanner_with_tracker.scan(token)
        result_without = scanner_without.scan(token)

        assert result_with.score_breakdown["wallet_reputation"] < result_without.score_breakdown["wallet_reputation"]


def test_social_presence_affects_score():
    scanner = fresh_scanner()
    with_socials = scanner.scan(healthy_token(social_presence_score=65.0))
    without_socials = scanner.scan(healthy_token(social_presence_score=30.0))
    assert with_socials.score > without_socials.score


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
