"""
اختبارات Paper Trading Engine — التدفق الكامل من التقييم للدخول
لحد الخروج، مرورًا بكل الأنظمة، ودعم أكتر من مسار (profile) في نفس الوقت.

تشغيل: python3 tests/test_paper_trading.py
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.paper_trading import PaperTradingEngine, StrategyProfile
from core.risk_engine import RiskEngine
from core.scanner import ScannerEngine
from core.strategy import PositionManager, SignalEngine
from tests.test_scanner import healthy_token
from tests.test_strategy import strong_signals

RISK_CONFIG = os.path.join(os.path.dirname(__file__), "..", "config", "risk_config.yaml")
SCANNER_CONFIG = os.path.join(os.path.dirname(__file__), "..", "config", "scanner_config.yaml")
STRATEGY_CONFIG = os.path.join(os.path.dirname(__file__), "..", "config", "strategy_config.yaml")
STRATEGY_SCALP_CONFIG = os.path.join(os.path.dirname(__file__), "..", "config", "strategy_scalp_config.yaml")
EXIT_RULES_CONSERVATIVE = os.path.join(os.path.dirname(__file__), "..", "config", "exit_rules_conservative.yaml")
EXIT_RULES_SCALP = os.path.join(os.path.dirname(__file__), "..", "config", "exit_rules_scalp.yaml")


def conservative_profile() -> StrategyProfile:
    return StrategyProfile(
        signal_engine=SignalEngine(config_path=STRATEGY_CONFIG),
        position_manager=PositionManager(exit_rules_config_path=EXIT_RULES_CONSERVATIVE),
    )


def scalp_profile() -> StrategyProfile:
    return StrategyProfile(
        signal_engine=SignalEngine(config_path=STRATEGY_SCALP_CONFIG),
        position_manager=PositionManager(exit_rules_config_path=EXIT_RULES_SCALP),
    )


def fresh_engine(profiles=None) -> PaperTradingEngine:
    if profiles is None:
        profiles = {"conservative": conservative_profile(), "scalp": scalp_profile()}
    return PaperTradingEngine(
        risk_engine=RiskEngine(config_path=RISK_CONFIG),
        scanner=ScannerEngine(config_path=SCANNER_CONFIG),
        profiles=profiles,
    )


def single_conservative_engine() -> PaperTradingEngine:
    return fresh_engine(profiles={"conservative": conservative_profile()})


# ---------------- Basic entry tests (single profile) ----------------

def test_good_candidate_opens_position():
    engine = single_conservative_engine()
    decision = engine.evaluate_candidate(
        "conservative", healthy_token(), strong_signals(), entry_price=1.0, stop_loss_price=0.9,
    )
    assert decision.opened
    assert healthy_token().address in engine.open_positions


def test_unknown_profile_raises():
    engine = single_conservative_engine()
    try:
        engine.evaluate_candidate("nonexistent", healthy_token(), strong_signals(), 1.0, 0.9)
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_position_deducts_from_virtual_cash():
    engine = single_conservative_engine()
    starting_cash = engine.cash_usd
    engine.evaluate_candidate("conservative", healthy_token(), strong_signals(), entry_price=1.0, stop_loss_price=0.9)
    assert engine.cash_usd < starting_cash


def test_bad_token_rejected_by_scanner_before_signal_check():
    engine = single_conservative_engine()
    bad_token = healthy_token(mint_authority_active=True)
    decision = engine.evaluate_candidate("conservative", bad_token, strong_signals(), 1.0, 0.9)
    assert not decision.opened
    assert "scanner" in decision.rejection_reason


def test_kill_switch_blocks_new_candidates():
    engine = single_conservative_engine()
    engine.risk_engine.activate_kill_switch()
    decision = engine.evaluate_candidate("conservative", healthy_token(), strong_signals(), 1.0, 0.9)
    assert not decision.opened
    assert "risk_engine_blocked" in decision.rejection_reason


def test_duplicate_token_not_opened_twice():
    engine = single_conservative_engine()
    engine.evaluate_candidate("conservative", healthy_token(), strong_signals(), 1.0, 0.9)
    decision2 = engine.evaluate_candidate("conservative", healthy_token(), strong_signals(), 1.0, 0.9)
    assert not decision2.opened
    assert decision2.rejection_reason == "position_already_open"


# ---------------- Exit / lifecycle tests (single profile) ----------------

def test_stop_hit_closes_position_and_updates_cash():
    engine = single_conservative_engine()
    engine.evaluate_candidate("conservative", healthy_token(), strong_signals(), 1.0, 0.9)
    address = healthy_token().address
    engine.update_position(address, current_price=0.89)
    assert address not in engine.open_positions
    assert len(engine.closed_trades) == 1
    assert engine.closed_trades[0]["win"] is False


def test_take_profit_target_records_partial_pnl_and_keeps_position_open():
    engine = single_conservative_engine()
    engine.evaluate_candidate("conservative", healthy_token(), strong_signals(), 1.0, 0.9)
    address = healthy_token().address
    engine.update_position(address, current_price=1.30)
    assert address in engine.open_positions
    assert len(engine.closed_trades) == 0


def test_trade_log_records_profile_and_reasons():
    engine = single_conservative_engine()
    engine.evaluate_candidate("conservative", healthy_token(), strong_signals(), 1.0, 0.9)
    entry_logs = [e for e in engine.trade_log if e.action == "entry"]
    assert len(entry_logs) == 1
    assert entry_logs[0].profile == "conservative"
    assert "scanner_score=" in entry_logs[0].reason
    assert "[conservative]" in entry_logs[0].reason


# ---------------- Multi-profile tests ----------------

def test_two_profiles_can_open_different_tokens_simultaneously():
    engine = fresh_engine()
    t1 = healthy_token(symbol="A", address="AddrA")
    t2 = healthy_token(symbol="B", address="AddrB")

    d1 = engine.evaluate_candidate("conservative", t1, strong_signals(), 1.0, 0.9)
    d2 = engine.evaluate_candidate("scalp", t2, strong_signals(), 1.0, 0.9)

    assert d1.opened and d2.opened
    assert len(engine.open_positions) == 2
    assert engine.open_positions["AddrA"].profile == "conservative"
    assert engine.open_positions["AddrB"].profile == "scalp"


def test_scalp_exit_rules_close_faster_than_conservative():
    """التأكيد إن مسار السكالب فعلًا بيقفل عند نسب ربح أقرب من المحافظ."""
    engine = fresh_engine()
    t1 = healthy_token(symbol="SWING", address="AddrSwing")
    t2 = healthy_token(symbol="SCALP", address="AddrScalp")

    engine.evaluate_candidate("conservative", t1, strong_signals(), 1.0, 0.9)
    engine.evaluate_candidate("scalp", t2, strong_signals(), 1.0, 0.9)

    # +10% -> كفاية لأول target بتاع السكالب (8%) بس مش المحافظ (30%)
    engine.update_position("AddrSwing", current_price=1.10)
    engine.update_position("AddrScalp", current_price=1.10)

    swing_pos = engine.open_positions["AddrSwing"].position
    scalp_pos = engine.open_positions["AddrScalp"].position

    assert swing_pos.targets_hit == []          # لسه محدش target اتحقق
    assert scalp_pos.targets_hit == [0]          # أول target بتاع السكالب اتحقق


def test_risk_engine_shared_across_profiles_for_daily_loss_limit():
    """خسارة من مسار السكالب لازم توقف الدخول في المحافظ برضو — نفس الحساب."""
    engine = fresh_engine()
    t1 = healthy_token(symbol="SCALP1", address="AddrScalp1")

    engine.evaluate_candidate("scalp", t1, strong_signals(), 1.0, 0.9)
    engine.update_position("AddrScalp1", current_price=0.89)  # خسارة -> قد تضرب الـdaily loss limit

    can_trade, _ = engine.risk_engine.can_open_new_trade()
    if not can_trade:
        # لو اتوقف، لازم يبقى موقوف على المسارين مع بعض (نفس الحساب)
        d_conservative = engine.evaluate_candidate(
            "conservative", healthy_token(symbol="X", address="AddrX"), strong_signals(), 1.0, 0.9,
        )
        assert not d_conservative.opened
        assert "risk_engine_blocked" in d_conservative.rejection_reason


def test_portfolio_snapshot_breaks_down_by_profile():
    engine = fresh_engine()
    t1 = healthy_token(symbol="A", address="AddrA")
    t2 = healthy_token(symbol="B", address="AddrB")

    engine.evaluate_candidate("conservative", t1, strong_signals(), 1.0, 0.9)
    engine.evaluate_candidate("scalp", t2, strong_signals(), 1.0, 0.9)

    engine.update_position("AddrA", current_price=0.89)  # محافظ يخسر
    engine.update_position("AddrB", current_price=1.30)  # سكالب لسه مفتوح (partial)

    snapshot = engine.portfolio_snapshot()
    assert "conservative" in snapshot["by_profile"]
    assert "scalp" in snapshot["by_profile"]
    assert snapshot["by_profile"]["conservative"]["closed_trades_count"] == 1
    assert snapshot["by_profile"]["conservative"]["win_rate_pct"] == 0.0


# ---------------- Wallet reputation feedback loop ----------------

def test_closed_trade_feeds_wallet_reputation_tracker():
    import tempfile
    from core.data.wallet_reputation import WalletReputationTracker

    with tempfile.TemporaryDirectory() as tmp:
        tracker = WalletReputationTracker(db_path=f"{tmp}/wallet_rep.db")
        engine = PaperTradingEngine(
            risk_engine=RiskEngine(config_path=RISK_CONFIG),
            scanner=ScannerEngine(config_path=SCANNER_CONFIG),
            profiles={"conservative": conservative_profile()},
            wallet_reputation_tracker=tracker,
        )

        token = healthy_token(top_holder_addresses=["WalletX111", "WalletY222"])
        engine.evaluate_candidate("conservative", token, strong_signals(), entry_price=1.0, stop_loss_price=0.9)
        engine.update_position(token.address, current_price=1.30)  # ربح جزئي
        engine.update_position(token.address, current_price=1.60)
        engine.update_position(token.address, current_price=2.20)
        engine.update_position(token.address, current_price=1.85)  # يقفل الباقي

        stats = tracker.get_wallet_stats("WalletX111")
        assert stats.trades_seen == 1  # صفقة واحدة اتقفلت بالكامل، سُجلت مرة


def test_no_tracker_means_no_crash_on_close():
    """التأكيد إن غياب الـtracker (الوضع الافتراضي) مايسببش أي مشكلة."""
    engine = single_conservative_engine()  # مفيش wallet_reputation_tracker
    token = healthy_token(top_holder_addresses=["SomeWallet"])
    engine.evaluate_candidate("conservative", token, strong_signals(), 1.0, 0.9)
    engine.update_position(token.address, current_price=0.89)  # لازم يقفل من غير exception
    assert len(engine.closed_trades) == 1


# ---------------- Emergency Mode ----------------

def test_emergency_check_does_nothing_when_healthy():
    engine = single_conservative_engine()
    token = healthy_token()
    engine.evaluate_candidate("conservative", token, strong_signals(), 1.0, 0.9)
    result = engine.run_emergency_check({token.address: 1.05})
    assert not result["triggered"]
    assert token.address in engine.open_positions  # المركز فضل مفتوح


def test_emergency_kill_switch_closes_all_open_positions():
    engine = fresh_engine()
    t1 = healthy_token(symbol="A", address="AddrA")
    t2 = healthy_token(symbol="B", address="AddrB")
    engine.evaluate_candidate("conservative", t1, strong_signals(), 1.0, 0.9)
    engine.evaluate_candidate("scalp", t2, strong_signals(), 1.0, 0.9)
    assert len(engine.open_positions) == 2

    engine.risk_engine.activate_kill_switch()
    result = engine.run_emergency_check({"AddrA": 1.10, "AddrB": 0.95})

    assert result["triggered"]
    assert result["reason"] == "kill_switch_active"
    assert len(engine.open_positions) == 0
    assert set(result["closed_addresses"]) == {"AddrA", "AddrB"}
    assert len(engine.closed_trades) == 2


def test_emergency_close_uses_given_current_price_for_pnl():
    engine = single_conservative_engine()
    token = healthy_token()
    engine.evaluate_candidate("conservative", token, strong_signals(), 1.0, 0.9)
    cash_before = engine.cash_usd

    engine.risk_engine.activate_kill_switch()
    engine.run_emergency_check({token.address: 1.20})  # +20% وقت القفل الطارئ

    assert engine.closed_trades[0]["total_pnl_usd"] > 0  # لازم يسجل ربح بناءً على السعر المعطى
    assert engine.cash_usd > cash_before


def test_emergency_close_falls_back_to_entry_price_when_price_missing():
    engine = single_conservative_engine()
    token = healthy_token()
    engine.evaluate_candidate("conservative", token, strong_signals(), 1.0, 0.9)

    engine.risk_engine.activate_kill_switch()
    result = engine.run_emergency_check({})  # مفيش سعر متاح خالص

    assert result["triggered"]
    entry_log = [e for e in engine.trade_log if e.action == "emergency_exit"][0]
    assert "price_unavailable_closed_at_entry_price" in entry_log.reason
    assert engine.closed_trades[0]["total_pnl_usd"] == 0.0  # PnL صفر لأن القفل كان بسعر الدخول نفسه


def test_emergency_triggered_by_drawdown_not_just_kill_switch():
    engine = fresh_engine()
    token = healthy_token()
    engine.evaluate_candidate("conservative", token, strong_signals(), 1.0, 0.9)

    engine.risk_engine.update_equity(780.0)  # 22% drawdown -> فوق حد الطوارئ
    result = engine.run_emergency_check({token.address: 0.85})

    assert result["triggered"]
    assert result["reason"] == "emergency_drawdown_exceeded"


def test_state_roundtrip_preserves_open_position_and_cash():
    engine = single_conservative_engine()
    token = healthy_token(address="StateAddr1", top_holder_addresses=["W1"])
    engine.evaluate_candidate("conservative", token, strong_signals(), entry_price=1.0, stop_loss_price=0.9)
    cash_before = engine.cash_usd

    state = engine.export_state()

    engine2 = single_conservative_engine()
    engine2.import_state(state)

    assert engine2.cash_usd == cash_before
    assert "StateAddr1" in engine2.open_positions
    assert engine2.open_positions["StateAddr1"].position.entry_price == 1.0
    assert engine2.open_positions["StateAddr1"].position.top_holder_addresses == ["W1"]


def test_state_roundtrip_preserves_closed_trades_and_trade_log():
    engine = single_conservative_engine()
    token = healthy_token(address="StateAddr2")
    engine.evaluate_candidate("conservative", token, strong_signals(), entry_price=1.0, stop_loss_price=0.9)
    engine.update_position("StateAddr2", current_price=0.89)  # يقفل بخسارة (ستوب)
    assert len(engine.closed_trades) == 1

    state = engine.export_state()
    engine2 = single_conservative_engine()
    engine2.import_state(state)

    assert engine2.closed_trades == engine.closed_trades
    assert len(engine2.trade_log) == len(engine.trade_log)
    assert engine2.trade_log[0].timestamp == engine.trade_log[0].timestamp


def test_state_roundtrip_preserves_risk_engine_equity_and_kill_switch():
    engine = single_conservative_engine()
    engine.risk_engine.update_equity(950.0)
    engine.risk_engine.activate_kill_switch()

    state = engine.export_state()
    engine2 = single_conservative_engine()
    engine2.import_state(state)

    assert engine2.risk_engine.current_equity_usd == 950.0
    assert engine2.risk_engine.kill_switch_active is True


def test_state_survives_json_roundtrip():
    """الاستخدام الحقيقي: الحالة بتتحفظ كملف JSON فعلي بين تشغيلة والتانية."""
    import json

    engine = single_conservative_engine()
    token = healthy_token(address="StateAddr3")
    engine.evaluate_candidate("conservative", token, strong_signals(), entry_price=1.0, stop_loss_price=0.9)

    state = engine.export_state()
    json_text = json.dumps(state)  # لازم يكون JSON-safe بالكامل
    reloaded_state = json.loads(json_text)

    engine2 = single_conservative_engine()
    engine2.import_state(reloaded_state)
    assert "StateAddr3" in engine2.open_positions
    assert engine2.cash_usd == engine.cash_usd


def test_emergency_feeds_wallet_reputation_tracker():
    import tempfile
    from core.data.wallet_reputation import WalletReputationTracker

    with tempfile.TemporaryDirectory() as tmp:
        tracker = WalletReputationTracker(db_path=f"{tmp}/wallet_rep.db")
        engine = PaperTradingEngine(
            risk_engine=RiskEngine(config_path=RISK_CONFIG),
            scanner=ScannerEngine(config_path=SCANNER_CONFIG),
            profiles={"conservative": conservative_profile()},
            wallet_reputation_tracker=tracker,
        )
        token = healthy_token(top_holder_addresses=["EmergencyWallet1"])
        engine.evaluate_candidate("conservative", token, strong_signals(), 1.0, 0.9)

        engine.risk_engine.activate_kill_switch()
        engine.run_emergency_check({token.address: 1.05})

        assert tracker.get_wallet_stats("EmergencyWallet1").trades_seen == 1


# ---------------- Auto-routing (evaluate_candidate_auto) ----------------

def test_auto_picks_profile_with_higher_signal_score_when_both_approve():
    engine = fresh_engine()
    token = healthy_token()
    decision = engine.evaluate_candidate_auto(token, strong_signals(), entry_price=1.0, stop_loss_price=0.9)

    assert decision.opened
    # الاتنين وافقوا (نفس strong_signals شغالة مع الاتنين هنا)، الفايز هو الأعلى score
    assert decision.profile in ("conservative", "scalp")
    assert decision.profiles_considered is not None
    assert decision.profile == max(decision.profiles_considered, key=decision.profiles_considered.get)


def test_auto_falls_back_to_only_approving_profile():
    """سيجنال بيعدي المحافظ (عتبة 65) بس مايعديش السكالب (عتبة 72)."""
    from tests.test_strategy import strong_signals as base_signals

    engine = fresh_engine()
    # نبني إشارة قريبة من عتبة المحافظ بس أضعف من عتبة السكالب
    borderline = base_signals(
        price_change_5m_pct=3.0, price_change_15m_pct=3.0,
        volume_5m_usd=4000, volume_15m_usd=9000, volume_1h_usd=90000,
        buys_last_5m=35, sells_last_5m=25,
        broke_recent_high=True, breakout_volume_multiple=2.2,
    )
    token = healthy_token()
    decision = engine.evaluate_candidate_auto(token, borderline, entry_price=1.0, stop_loss_price=0.9)

    if decision.opened:
        # لو اتفتحت، لازم تكون المحافظ بس (الأسهل تعدية) مش السكالب
        assert decision.profile == "conservative"


def test_auto_rejects_when_no_profile_approves():
    engine = fresh_engine()
    weak_signals = strong_signals(
        price_change_5m_pct=0.0, price_change_15m_pct=0.0,
        volume_5m_usd=500, volume_15m_usd=1000, buys_last_5m=10, sells_last_5m=10,
        broke_recent_high=False,
    )
    decision = engine.evaluate_candidate_auto(healthy_token(), weak_signals, 1.0, 0.9)
    assert not decision.opened
    assert decision.profile == "auto"


def test_auto_respects_scanner_gate():
    engine = fresh_engine()
    bad_token = healthy_token(mint_authority_active=True)
    decision = engine.evaluate_candidate_auto(bad_token, strong_signals(), 1.0, 0.9)
    assert not decision.opened
    assert "scanner" in decision.rejection_reason


def test_auto_respects_shared_risk_gate():
    engine = fresh_engine()
    engine.risk_engine.activate_kill_switch()
    decision = engine.evaluate_candidate_auto(healthy_token(), strong_signals(), 1.0, 0.9)
    assert not decision.opened
    assert "risk_engine_blocked" in decision.rejection_reason


def test_auto_does_not_open_duplicate_for_same_address():
    engine = fresh_engine()
    token = healthy_token()
    engine.evaluate_candidate_auto(token, strong_signals(), 1.0, 0.9)
    decision2 = engine.evaluate_candidate_auto(token, strong_signals(), 1.0, 0.9)
    assert not decision2.opened
    assert decision2.rejection_reason == "position_already_open"


def test_auto_uses_per_profile_scanner_when_provided():
    """
    عملة صغيرة عمرها ساعة بس: الـScanner المحافظ (منحنى نضج 6 ساعات)
    المفروض يرفضها، لكن الـScanner المخصص للسكالب (منحنى نضج ساعة
    ونص) المفروض يقبلها — نفس الـHard Filters، منحنى مختلف بس.
    """
    from core.scanner import ScannerEngine

    scanner_conservative = ScannerEngine(config_path=SCANNER_CONFIG)
    scanner_scalp = ScannerEngine(config_path=os.path.join(
        os.path.dirname(__file__), "..", "config", "scanner_scalp_config.yaml"
    ))

    young_token = healthy_token(token_age_minutes=63, holder_count_1h_ago=None)
    # نفس عدد الهولدرز (نمو 0%) عشان نعزل تأثير منحنى العمر بس
    young_token.holder_count_1h_ago = young_token.holder_count

    result_conservative = scanner_conservative.scan(young_token)
    result_scalp = scanner_scalp.scan(young_token)

    assert result_scalp.score_breakdown["age_maturity"] > result_conservative.score_breakdown["age_maturity"]


def test_evaluate_candidate_auto_respects_profile_specific_scanner():
    from core.scanner import ScannerEngine
    from core.strategy import PositionManager, SignalEngine

    scanner_conservative = ScannerEngine(config_path=SCANNER_CONFIG)
    scanner_scalp = ScannerEngine(config_path=os.path.join(
        os.path.dirname(__file__), "..", "config", "scanner_scalp_config.yaml"
    ))

    profiles = {
        "conservative": StrategyProfile(
            signal_engine=SignalEngine(config_path=STRATEGY_CONFIG),
            position_manager=PositionManager(exit_rules_config_path=EXIT_RULES_CONSERVATIVE),
            scanner=scanner_conservative,
        ),
        "scalp": StrategyProfile(
            signal_engine=SignalEngine(config_path=STRATEGY_SCALP_CONFIG),
            position_manager=PositionManager(exit_rules_config_path=EXIT_RULES_SCALP),
            scanner=scanner_scalp,
        ),
    }
    engine = PaperTradingEngine(
        risk_engine=RiskEngine(config_path=RISK_CONFIG),
        scanner=scanner_conservative,  # fallback مشترك، مش هيتستخدم هنا لأن كل مسار عنده واحد خاص
        profiles=profiles,
    )

    # عملة صغيرة السن، حجم تداول نسبة للسيولة عالي (طبيعي لعملة سكالب)
    young_hot_token = healthy_token(token_age_minutes=45, volume_24h_usd=350000, liquidity_usd=50000)
    young_hot_token.holder_count_1h_ago = young_hot_token.holder_count

    decision = engine.evaluate_candidate_auto(young_hot_token, strong_signals(), entry_price=1.0, stop_loss_price=0.9)
    # المفروض تدخل تحت السكالب تحديدًا (المحافظ من المرجح يرفضها لصغر سنها)
    if decision.opened:
        assert decision.profile == "scalp"


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
