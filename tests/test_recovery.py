"""
اختبارات التعافي من الأخطاء المتتالية — مبنية على حادثة 19 أكتوبر: RugCheck فشل
في كام عملة جديدة متتالية، العدّاد وصل 5، حالة الطوارئ قفلت الدورة قبل الاكتشاف،
ومفيش حاجة كانت بتقدر تصفّر العدّاد، فالبوت فضل واقف ~19 ساعة.

تشغيل: python3 tests/test_recovery.py
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.data.rugcheck_client import RugCheckError, RugCheckRateLimitError
from core.discovery import DiscoveredCandidate

from tests.test_onchain_layer import clean_report
from tests.test_orchestrator import (
    _FakeDexClient, _FakeDiscovery, _FakeRugCheckClient, _engine_with_open_position,
    build_engine, build_orchestrator, healthy_pair,
)


class _ScriptedRugCheck:
    def __init__(self, behaviors):
        self.behaviors = list(behaviors)
        self.calls = 0

    def get_token_report(self, mint):
        b = self.behaviors[min(self.calls, len(self.behaviors) - 1)]
        self.calls += 1
        if isinstance(b, Exception):
            raise b
        return b


def _cands(n):
    return [DiscoveredCandidate(address=f"A{i}", symbol=f"M{i}", pair=healthy_pair(address=f"A{i}")) for i in range(n)]


def _stuck_engine():
    engine = build_engine()
    for _ in range(5):
        engine.risk_engine.record_execution_error()
    assert engine.risk_engine.daily_state.halted
    assert engine.risk_engine.daily_state.halt_reason.value == "consecutive_execution_errors"
    return engine


def test_success_clears_error_halt():
    engine = _stuck_engine()
    engine.risk_engine.record_execution_success()
    assert engine.risk_engine.daily_state.consecutive_errors == 0
    assert engine.risk_engine.can_open_new_trade()[0]


def test_success_does_not_clear_other_halts():
    engine = build_engine()
    engine.risk_engine.record_trade_result(pnl_usd=-20.0)  # daily loss limit
    engine.risk_engine.record_execution_success()
    can, reason = engine.risk_engine.can_open_new_trade()
    assert not can and reason == "daily_loss_limit_hit"

    engine2 = build_engine()
    engine2.risk_engine.activate_kill_switch()
    engine2.risk_engine.record_execution_success()
    assert not engine2.risk_engine.can_open_new_trade()[0]


def test_stuck_bot_recovers_by_itself_with_no_open_positions():
    """السيناريو الحقيقي: عدّاد أخطاء 5 + مفيش صفقات مفتوحة -> الدورة لازم تكمّل وتتعافى."""
    engine = _stuck_engine()
    orch = build_orchestrator(_FakeDiscovery(_cands(2)), _FakeDexClient(), _FakeRugCheckClient(), engine=engine)
    r = orch.run_cycle()
    assert r.candidates_discovered == 2  # قبل الإصلاح: 0 (الدورة كانت بتقف قبل الاكتشاف)
    assert r.candidates_evaluated == 2
    assert engine.risk_engine.daily_state.consecutive_errors == 0
    assert engine.risk_engine.can_open_new_trade()[0] or engine.open_positions  # اترفع الإيقاف (أو اتفتحت صفقة بعده)


def test_recovery_works_even_without_shadow_log():
    engine = _stuck_engine()
    orch = build_orchestrator(_FakeDiscovery(_cands(1)), _FakeDexClient(), _FakeRugCheckClient(), engine=engine)
    assert orch.shadow_log is None
    r = orch.run_cycle()
    assert r.candidates_evaluated == 1  # حاول وماوقفش عند الـhalt
    assert engine.risk_engine.daily_state.consecutive_errors == 0


def test_emergency_with_open_position_still_closes_and_stops_the_cycle():
    """أخطاء حقيقية مستمرة (DexScreener واقع) + صفقة مفتوحة = الحماية الأصلية: قفل وإيقاف الدورة."""
    engine = _engine_with_open_position("OpenAddr")
    for _ in range(3):
        engine.risk_engine.record_execution_error()
    orch = build_orchestrator(_FakeDiscovery(_cands(2)), _FakeDexClient(raise_error=True),
                              _FakeRugCheckClient(), engine=engine)
    r = orch.run_cycle()
    assert r.emergency_triggered and "OpenAddr" in r.positions_closed
    assert r.candidates_discovered == 0  # الحماية الأصلية لسه شغالة لما فيه صفقة مفتوحة
    assert not engine.open_positions


def test_successful_price_fetch_for_open_position_clears_stale_errors_first():
    """لو الشبكة رجعت وسعر الصفقة المفتوحة اتجاب بنجاح، مفيش طوارئ ومفيش قفل غلط."""
    engine = _engine_with_open_position("OpenAddr")
    for _ in range(3):
        engine.risk_engine.record_execution_error()
    orch = build_orchestrator(_FakeDiscovery([]), _FakeDexClient(prices_by_address={"OpenAddr": 1.0}),
                              _FakeRugCheckClient(), engine=engine)
    r = orch.run_cycle()
    assert not r.emergency_triggered and "OpenAddr" in engine.open_positions


def test_not_indexed_tokens_are_rejected_without_counting_execution_errors():
    engine = build_engine()
    rc = _ScriptedRugCheck([RugCheckError("token_not_found_or_not_indexed_yet")])
    orch = build_orchestrator(_FakeDiscovery(_cands(6)), _FakeDexClient(), rc, engine=engine)
    r = orch.run_cycle()
    assert rc.calls == 6
    assert r.rejection_counts == {"rugcheck_not_indexed": 6}
    assert engine.risk_engine.daily_state.consecutive_errors == 0  # قبل الإصلاح: 6 -> طوارئ
    assert engine.risk_engine.can_open_new_trade()[0]


def test_real_failures_still_count_and_trigger_the_halt():
    engine = build_engine()
    rc = _ScriptedRugCheck([RugCheckError("network_error: boom")])
    orch = build_orchestrator(_FakeDiscovery(_cands(4)), _FakeDexClient(), rc, engine=engine)
    orch.run_cycle()
    assert engine.risk_engine.daily_state.consecutive_errors >= 3
    assert engine.risk_engine.daily_state.halted


def test_rate_limit_counts_once_and_stops_evaluating_the_cycle():
    engine = build_engine()
    rc = _ScriptedRugCheck([RugCheckRateLimitError("rugcheck_rate_limited")])
    orch = build_orchestrator(_FakeDiscovery(_cands(6)), _FakeDexClient(), rc, engine=engine)
    r = orch.run_cycle()
    assert rc.calls == 1  # ماكمّلناش نضرب الـAPI بعد الـ429
    assert engine.risk_engine.daily_state.consecutive_errors == 1
    assert r.rejection_counts == {"rugcheck_failed": 1}


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
