"""
اختبارات الـStrategy Engine — إشارات الدخول وإدارة الخروج
(take profit تدريجي + trailing stop).

تشغيل: python3 tests/test_strategy.py
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.strategy import (
    ExitActionType,
    PositionManager,
    PositionState,
    SignalEngine,
    TimingSignals,
)

STRATEGY_CONFIG = os.path.join(os.path.dirname(__file__), "..", "config", "strategy_config.yaml")
RISK_CONFIG = os.path.join(os.path.dirname(__file__), "..", "config", "risk_config.yaml")
EXIT_RULES_CONSERVATIVE = os.path.join(os.path.dirname(__file__), "..", "config", "exit_rules_conservative.yaml")


def fresh_signal_engine() -> SignalEngine:
    return SignalEngine(config_path=STRATEGY_CONFIG)


def fresh_position_manager() -> PositionManager:
    return PositionManager(exit_rules_config_path=EXIT_RULES_CONSERVATIVE)


def strong_signals(**overrides) -> TimingSignals:
    base = dict(
        symbol="GOOD",
        address="Gxxx111",
        price_change_5m_pct=8.0,
        price_change_15m_pct=12.0,
        volume_5m_usd=20000,
        volume_15m_usd=45000,
        volume_1h_usd=90000,
        buys_last_5m=60,
        sells_last_5m=25,
        broke_recent_high=True,
        breakout_volume_multiple=2.5,
    )
    base.update(overrides)
    return TimingSignals(**base)


# ---------------- Entry signal tests ----------------

def test_strong_signals_approved():
    engine = fresh_signal_engine()
    decision = engine.evaluate_entry(strong_signals())
    assert decision.approved
    assert decision.positive_signal_count >= 2


def test_flat_price_and_low_volume_rejected():
    engine = fresh_signal_engine()
    decision = engine.evaluate_entry(strong_signals(
        price_change_5m_pct=0.0,
        price_change_15m_pct=0.0,
        volume_5m_usd=1000,
        volume_15m_usd=3000,
        volume_1h_usd=90000,
        buys_last_5m=20,
        sells_last_5m=20,
        broke_recent_high=False,
    ))
    assert not decision.approved


def test_single_strong_factor_alone_not_enough():
    """حتى لو عامل واحد قوي جدًا، المفروض نحتاج أكتر من إشارة مستقلة."""
    engine = fresh_signal_engine()
    decision = engine.evaluate_entry(strong_signals(
        price_change_5m_pct=25.0,   # momentum قوي جدًا بمفرده
        price_change_15m_pct=25.0,
        volume_5m_usd=1000,
        volume_15m_usd=2000,        # حجم ضعيف
        volume_1h_usd=90000,
        buys_last_5m=15,
        sells_last_5m=15,           # ضغط شراء متعادل
        broke_recent_high=False,    # مفيش كسر
    ))
    assert decision.positive_signal_count < 2 or not decision.approved


def test_fakeout_breakout_low_volume_scores_lower():
    engine = fresh_signal_engine()
    real_breakout = engine.evaluate_entry(strong_signals(breakout_volume_multiple=3.0))
    fake_breakout = engine.evaluate_entry(strong_signals(breakout_volume_multiple=0.5))
    assert fake_breakout.signal_breakdown["breakout_confirmation"] < real_breakout.signal_breakdown["breakout_confirmation"]


# ---------------- Exit / position management tests ----------------

def test_stop_loss_triggers_full_close():
    pm = fresh_position_manager()
    pos = PositionState(
        symbol="X", address="addr", entry_price=1.0,
        initial_stop_price=0.9, original_size_usd=100.0,
    )
    actions = pm.evaluate(pos, current_price=0.89)
    assert len(actions) == 1
    assert actions[0].action_type == ExitActionType.FULL_CLOSE_STOP_HIT


def test_first_take_profit_target_partial_closes_and_moves_stop_to_breakeven():
    pm = fresh_position_manager()
    pos = PositionState(
        symbol="X", address="addr", entry_price=1.0,
        initial_stop_price=0.9, original_size_usd=100.0,
    )
    # +30% gain -> أول target في الـconfig الافتراضي
    actions = pm.evaluate(pos, current_price=1.30)
    action_types = [a.action_type for a in actions]
    assert ExitActionType.PARTIAL_CLOSE in action_types
    assert ExitActionType.MOVE_STOP in action_types
    assert pos.current_stop_price == 1.0  # اتحرك لـbreakeven
    assert pos.remaining_size_pct == 70.0  # 30% اتقفلت


def test_target_not_triggered_twice():
    pm = fresh_position_manager()
    pos = PositionState(
        symbol="X", address="addr", entry_price=1.0,
        initial_stop_price=0.9, original_size_usd=100.0,
    )
    pm.evaluate(pos, current_price=1.30)  # يضرب أول target
    actions_again = pm.evaluate(pos, current_price=1.31)  # لسه فوق نفس الـtarget
    partial_closes = [a for a in actions_again if a.action_type == ExitActionType.PARTIAL_CLOSE]
    assert len(partial_closes) == 0  # مايتكررش


def test_trailing_stop_activates_and_advances_with_price():
    pm = fresh_position_manager()
    pos = PositionState(
        symbol="X", address="addr", entry_price=1.0,
        initial_stop_price=0.9, original_size_usd=100.0,
    )
    # يوصل لكل الـtargets عشان trailing يتفعل (activation عند 100% في الـconfig الافتراضي)
    pm.evaluate(pos, current_price=1.30)
    pm.evaluate(pos, current_price=1.60)
    stop_before = pos.current_stop_price
    pm.evaluate(pos, current_price=2.00)  # +100% -> trailing يتفعل
    assert pos.trailing_active
    stop_after_activation = pos.current_stop_price
    assert stop_after_activation > stop_before

    # السعر يكمل يصعد -> الستوب يتحرك وراه
    pm.evaluate(pos, current_price=2.50)
    assert pos.current_stop_price > stop_after_activation


def test_trailing_stop_never_moves_down_on_pullback():
    pm = fresh_position_manager()
    pos = PositionState(
        symbol="X", address="addr", entry_price=1.0,
        initial_stop_price=0.9, original_size_usd=100.0,
    )
    pm.evaluate(pos, current_price=1.30)
    pm.evaluate(pos, current_price=2.00)
    pm.evaluate(pos, current_price=2.50)
    stop_at_peak = pos.current_stop_price

    # السعر يرتد لتحت، بس فوق الستوب لسه
    actions = pm.evaluate(pos, current_price=2.20)
    trailing_updates = [a for a in actions if a.action_type == ExitActionType.UPDATE_TRAILING_STOP]
    assert len(trailing_updates) == 0
    assert pos.current_stop_price == stop_at_peak  # الستوب متحركش لتحت


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
