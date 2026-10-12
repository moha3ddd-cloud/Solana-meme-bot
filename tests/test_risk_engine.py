"""
اختبارات سريعة للـRisk Engine — تأكيد إن القواعد اللي إنت حطيتها
فعلاً بتتنفذ زي ما هي، قبل ما نبني أي حاجة فوقها.

تشغيل: python -m pytest tests/test_risk_engine.py -v
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.risk_engine import RiskEngine, TradingHaltReason

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config", "risk_config.yaml")


def fresh_engine() -> RiskEngine:
    return RiskEngine(config_path=CONFIG_PATH)


def test_normal_position_sizing_respects_1pct_risk():
    engine = fresh_engine()  # $1000 account, 1% risk config
    result = engine.calculate_position_size(entry_price=1.0, stop_loss_price=0.9)
    assert result.approved
    # 1% of $1000 = $10 risk. الحجم بيتحسب على (مسافة الستوب 10% + الفجوة
    # الافتراضية stop_gap_buffer_pct من الـconfig) مش على الستوب لوحده.
    buffer = engine.config["position_sizing"]["stop_gap_buffer_pct"] / 100
    assert result.risk_amount_usd == 10.0
    assert result.position_size_usd == round(10.0 / (0.10 + buffer), 2)


def test_gap_buffer_sizes_position_so_a_gapped_stop_costs_the_risk_budget():
    engine = fresh_engine()
    buffer_pct = engine.config["position_sizing"]["stop_gap_buffer_pct"]
    assert buffer_pct > 0  # الافتراضي الجديد: فيه فجوة أمان
    result = engine.calculate_position_size(entry_price=1.0, stop_loss_price=0.9)
    # الستوب 10% + فجوة buffer_pct => الخسارة الفعلية بالنسبة دي = 1% من الحساب
    loss_if_gapped = result.position_size_usd * (0.10 + buffer_pct / 100)
    assert abs(loss_if_gapped - result.risk_amount_usd) < 0.05


def test_zero_gap_buffer_keeps_old_sizing_behavior():
    engine = fresh_engine()
    engine.config["position_sizing"]["stop_gap_buffer_pct"] = 0.0
    result = engine.calculate_position_size(entry_price=1.0, stop_loss_price=0.9)
    assert result.position_size_usd == 100.0  # 10$ / 10%


def test_execution_success_resets_consecutive_errors():
    engine = fresh_engine()
    engine.record_execution_error()
    engine.record_execution_error()
    engine.record_execution_success()
    assert engine.daily_state.consecutive_errors == 0


def test_daily_loss_limit_halts_trading():
    engine = fresh_engine()
    # خسارة 2% من 1000 = -$20 -> يوصل للـdaily loss limit
    engine.record_trade_result(pnl_usd=-20.0)
    can_trade, reason = engine.can_open_new_trade()
    assert not can_trade
    assert reason == TradingHaltReason.DAILY_LOSS_LIMIT.value


def test_drawdown_reduces_risk_not_increases_it():
    engine = fresh_engine()
    # نخسر تدريجيًا لحد ما الـdrawdown يوصل 10%
    engine.update_equity(900.0)  # 10% drawdown من peak 1000
    result = engine.calculate_position_size(entry_price=1.0, stop_loss_price=0.9)
    # المفروض المضاعِف يبقى 0.5 (حسب الـconfig tier بتاع 10%)
    assert result.drawdown_multiplier_applied == 0.5
    # المخاطرة الفعلية أقل من الأساسية (1%)
    assert result.effective_risk_pct < 1.0


def test_emergency_drawdown_blocks_all_trades():
    engine = fresh_engine()
    engine.update_equity(780.0)  # 22% drawdown -> فوق الـemergency threshold (20%)
    result = engine.calculate_position_size(entry_price=1.0, stop_loss_price=0.9)
    assert not result.approved
    assert result.reason_if_rejected == "emergency_drawdown_zero_risk"


def test_kill_switch_blocks_new_trades_immediately():
    engine = fresh_engine()
    engine.activate_kill_switch()
    can_trade, reason = engine.can_open_new_trade()
    assert not can_trade
    assert reason == TradingHaltReason.KILL_SWITCH.value


def test_consecutive_errors_halt_trading():
    engine = fresh_engine()
    for _ in range(3):  # max_consecutive_errors = 3 in config
        engine.record_execution_error()
    can_trade, reason = engine.can_open_new_trade()
    assert not can_trade
    assert reason == TradingHaltReason.CONSECUTIVE_ERRORS.value


def test_position_size_never_increases_after_losses_without_recovery():
    """قاعدة مهمة: البوت ممنوع يضاعف المخاطرة عشان يعوض خسارة."""
    engine = fresh_engine()
    result_before = engine.calculate_position_size(entry_price=1.0, stop_loss_price=0.9)

    engine.update_equity(950.0)  # 5% drawdown
    result_after = engine.calculate_position_size(entry_price=1.0, stop_loss_price=0.9)

    assert result_after.effective_risk_pct <= result_before.effective_risk_pct


# ---------------- Emergency trigger tests ----------------

def test_emergency_none_when_healthy():
    engine = fresh_engine()
    assert engine.emergency_trigger_reason() is None


def test_emergency_triggered_by_kill_switch():
    engine = fresh_engine()
    engine.activate_kill_switch()
    assert engine.emergency_trigger_reason() == "kill_switch_active"


def test_emergency_triggered_by_drawdown():
    engine = fresh_engine()
    engine.update_equity(780.0)  # 22% drawdown -> فوق حد الطوارئ (20% في الـconfig)
    assert engine.emergency_trigger_reason() == "emergency_drawdown_exceeded"


def test_emergency_triggered_by_consecutive_errors():
    engine = fresh_engine()
    for _ in range(3):
        engine.record_execution_error()
    assert engine.emergency_trigger_reason() == "consecutive_execution_errors"


def test_emergency_daily_loss_limit_alone_does_not_trigger_emergency():
    """
    daily loss limit (-2%) بيوقف صفقات جديدة بس — مش نفس درجة الطوارئ
    اللي بتقفل المراكز المفتوحة. الاتنين مختلفين عمدًا.
    """
    engine = fresh_engine()
    engine.record_trade_result(pnl_usd=-20.0)  # يضرب daily loss limit
    can_trade, _ = engine.can_open_new_trade()
    assert not can_trade  # صفقات جديدة اتمنعت
    assert engine.emergency_trigger_reason() is None  # لكن مفيش طوارئ تقفل المفتوح


if __name__ == "__main__":
    # تشغيل بسيط من غير pytest، للتأكيد السريع
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
