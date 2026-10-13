"""
اختبارات Shadow Log والمسارات "الظل" (shadow_only).

تشغيل: python3 tests/test_shadow_log.py
"""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.paper_trading import PaperTradingEngine, StrategyProfile
from core.risk_engine import RiskEngine
from core.scanner import ScannerEngine
from core.shadow_log import ShadowLog
from core.strategy import PositionManager, SignalEngine

from tests.test_orchestrator import (
    _FakeDexClient, _FakeDiscovery, _FakeRugCheckClient, build_engine, build_orchestrator, healthy_pair,
)
from tests.test_paper_trading import (
    EXIT_RULES_CONSERVATIVE, EXIT_RULES_SCALP, RISK_CONFIG, SCANNER_CONFIG, STRATEGY_CONFIG, STRATEGY_SCALP_CONFIG,
)
from tests.test_scanner import healthy_token
from tests.test_strategy import strong_signals
from core.discovery import DiscoveredCandidate


class _Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t

    def advance_min(self, m):
        self.t += m * 60


def _log(clock, horizons=(30, 60, 120)):
    return ShadowLog(path=os.path.join(tempfile.mkdtemp(), "shadow.json"), horizons_min=horizons, clock=clock)


def _profiles(scalp="approved", cons="hard_filter_failed"):
    return {"scalp": {"stage": scalp, "scanner_score": 77, "signal_score": 80, "shadow_only": True},
            "conservative": {"stage": cons, "scanner_score": None, "signal_score": None, "shadow_only": False}}


def test_followups_are_measured_only_after_each_horizon_with_real_elapsed_time():
    clk = _Clock(); log = _log(clk)
    log.add("A", "AAA", 1.0, _profiles())
    calls = []
    fetch = lambda addrs: calls.append(addrs) or {"A": 1.5}

    clk.advance_min(10)
    log.update_followups(fetch)
    assert log.pending[0]["followups"] == {} and not calls  # لسه بدري: مفيش حتى طلب شبكة

    clk.advance_min(25)  # 35 دقيقة
    log.update_followups(fetch)
    f = log.pending[0]["followups"]
    assert list(f) == ["30"] and f["30"]["elapsed_min"] == 35.0 and f["30"]["return_pct"] == 50.0

    clk.advance_min(90)  # 125 دقيقة
    log.update_followups(lambda a: {"A": 0.5})
    assert not log.pending and len(log.completed) == 1
    done = log.completed[0]
    assert set(done["followups"]) == {"30", "60", "120"}
    assert done["followups"]["120"]["return_pct"] == -50.0


def test_missing_price_is_unresolved_not_zero():
    clk = _Clock(); log = _log(clk)
    log.add("DEAD", "RUG", 1.0, _profiles())
    clk.advance_min(400)  # بعد الأفق الأخير + فترة السماح
    log.update_followups(lambda addrs: {})  # العملة اختفت من DexScreener
    assert len(log.completed) == 1
    assert log.completed[0]["unresolved_horizons"] == [30, 60, 120]
    s = log.summarize()
    assert s["groups"]["all"]["by_horizon"]["30"]["n"] == 0
    assert s["groups"]["all"]["by_horizon"]["30"]["unresolved"] == 1  # بيتعدّ، مش بيتحسب كصفر


def test_summary_compares_would_enter_vs_not_entered():
    clk = _Clock(); log = _log(clk, horizons=(30,))
    log.add("W1", "W1", 1.0, _profiles(scalp="approved"))
    log.add("W2", "W2", 1.0, _profiles(scalp="approved"))
    log.add("N1", "N1", 1.0, _profiles(scalp="signal_rejected"))
    clk.advance_min(40)
    log.update_followups(lambda a: {"W1": 0.5, "W2": 0.6, "N1": 1.2})
    g = log.summarize()["groups"]
    assert g["scalp_would_enter"]["records"] == 2 and g["scalp_not_entered"]["records"] == 1
    would = g["scalp_would_enter"]["by_horizon"]["30"]
    assert would["n"] == 2 and would["mean_return_pct"] == -45.0 and would["pct_positive"] == 0.0
    assert would["pct_dumped_30"] == 100.0
    assert g["scalp_not_entered"]["by_horizon"]["30"]["mean_return_pct"] == 20.0
    assert g["all"]["unique_tokens"] == 3


def test_save_and_load_roundtrip_and_corrupt_file_is_tolerated():
    clk = _Clock(); log = _log(clk)
    log.add("A", "AAA", 2.0, _profiles())
    log.save()
    again = ShadowLog(path=log.path, horizons_min=(30, 60, 120), clock=clk)
    assert len(again.pending) == 1 and again.pending[0]["address"] == "A"
    with open(log.path, "w") as f:
        f.write("{not json")
    broken = ShadowLog(path=log.path, clock=clk)
    assert broken.pending == [] and broken.completed == []  # ماقعش


def test_add_ignores_invalid_price():
    clk = _Clock(); log = _log(clk)
    log.add("A", "A", 0.0, _profiles())
    log.add("", "A", 1.0, _profiles())
    assert log.pending == []


def _engine_with_scalp_shadow():
    engine = build_engine()
    engine.profiles["scalp"].shadow_only = True
    return engine


def test_shadow_only_profile_never_opens_a_position():
    engine = _engine_with_scalp_shadow()
    # بيانات قوية جدًا: المسارين المفروض يوافقوا — لكن السكالب shadow فمايفتحش
    d = engine.evaluate_candidate_auto(healthy_token(address="X1"), strong_signals(), 1.0, 0.9)
    if d.opened:
        assert d.profile == "conservative"
        assert engine.open_positions["X1"].profile == "conservative"

    engine2 = build_engine()
    engine2.profiles["conservative"].shadow_only = True
    engine2.profiles["scalp"].shadow_only = True
    d2 = engine2.evaluate_candidate_auto(healthy_token(address="X2"), strong_signals(), 1.0, 0.9)
    assert not d2.opened and not engine2.open_positions  # كله shadow = مفيش تداول


def test_shadow_evaluate_reports_every_profile_without_side_effects():
    engine = _engine_with_scalp_shadow()
    cash_before = engine.cash_usd
    res = engine.shadow_evaluate(healthy_token(address="X3"), strong_signals())
    assert set(res) == {"conservative", "scalp"}
    assert res["scalp"]["shadow_only"] and not res["conservative"]["shadow_only"]
    assert all(r["stage"] in ("hard_filter_failed", "score_too_low", "signal_rejected", "approved") for r in res.values())
    assert not engine.open_positions and engine.cash_usd == cash_before


def test_orchestrator_records_shadow_even_when_trading_is_halted():
    clk = _Clock(); log = _log(clk)
    engine = _engine_with_scalp_shadow()
    engine.risk_engine.record_trade_result(pnl_usd=-20.0)  # daily loss limit
    assert not engine.risk_engine.can_open_new_trade()[0]
    cands = [DiscoveredCandidate(address=f"A{i}", symbol=f"M{i}", pair=healthy_pair(address=f"A{i}")) for i in range(2)]
    orch = build_orchestrator(_FakeDiscovery(cands), _FakeDexClient(), _FakeRugCheckClient(), engine=engine)
    orch.shadow_log = log
    r = orch.run_cycle()
    assert len(log.pending) == 2  # اتسجّلوا رغم إن التداول موقوف
    assert all(rec["context"]["trading_allowed"] is False for rec in log.pending)
    assert r.rejection_counts.get("trading_halted") == 2
    assert not engine.open_positions


def test_shadow_failure_never_breaks_the_trading_cycle():
    class _BrokenLog:
        def update_followups(self, f): raise RuntimeError("boom")
        def add(self, *a, **k): raise RuntimeError("boom")
    c = DiscoveredCandidate(address="A1", symbol="M", pair=healthy_pair(address="A1"))
    orch = build_orchestrator(_FakeDiscovery([c]), _FakeDexClient(), _FakeRugCheckClient())
    orch.shadow_log = _BrokenLog()
    r = orch.run_cycle()
    assert r.candidates_evaluated == 1  # الدورة كمّلت عادي


def test_shadow_followup_prices_use_live_pair_and_dont_count_as_errors():
    from tests.test_primary_pair import _MultiPairDex, ADDR
    orch = build_orchestrator(_FakeDiscovery([]), _MultiPairDex(live_price="1.25"), _FakeRugCheckClient())
    prices = orch._fetch_prices_quiet([ADDR])
    assert prices == {ADDR: 1.25}  # مش السعر المتجمّد بتاع الـbonding curve القديم

    class _Down:
        def get_pairs_by_token_addresses(self, a): raise ConnectionError("x")
    orch2 = build_orchestrator(_FakeDiscovery([]), _Down(), _FakeRugCheckClient())
    errors_before = orch2.engine.risk_engine.daily_state.consecutive_errors
    assert orch2._fetch_prices_quiet(["A"]) == {}
    assert orch2.engine.risk_engine.daily_state.consecutive_errors == errors_before


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
