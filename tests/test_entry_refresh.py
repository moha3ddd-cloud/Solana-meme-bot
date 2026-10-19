"""
اختبارات تحديث السعر قبل الدخول + قواعد الـshadow (rule_matches).

تشغيل: python3 tests/test_entry_refresh.py
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.discovery import DiscoveredCandidate
from core.shadow_log import ShadowLog, rule_matches

from tests.test_orchestrator import (
    _FakeDiscovery, _FakeRugCheckClient, build_engine, build_orchestrator, healthy_pair,
)


class _PriceDex:
    """يرجّع سعر طازة ثابت لأي عنوان (أو يرمي خطأ / يرجّع فاضي)."""
    def __init__(self, price=None, raise_error=False):
        self.price, self.raise_error, self.calls = price, raise_error, 0

    def get_pairs_by_token_addresses(self, addresses):
        self.calls += 1
        if self.raise_error:
            raise ConnectionError("down")
        return [] if self.price is None else [{"priceUsd": str(self.price)}]


def _run(fresh_price, snapshot="1.0", **dex_kwargs):
    c = DiscoveredCandidate(address="A1", symbol="M", pair=healthy_pair(address="A1", price=snapshot))
    engine = build_engine()
    dex = _PriceDex(price=fresh_price, **dex_kwargs)
    orch = build_orchestrator(_FakeDiscovery([c]), dex, _FakeRugCheckClient(), engine=engine)
    r = orch.run_cycle()
    return r, engine, dex


def test_entry_uses_fresh_price_when_close_to_snapshot():
    r, engine, dex = _run(fresh_price=1.02)
    assert "A1" in engine.open_positions, r.rejection_counts
    pos = engine.open_positions["A1"].position
    assert pos.entry_price == 1.02                      # بالسعر الطازة مش سعر الاكتشاف
    assert abs(pos.initial_stop_price - 1.02 * 0.9) < 1e-9  # والستوب متحسب منه
    assert r.candidate_details[-1]["entry_drift_pct"] == 2.0


def test_entry_rejected_when_price_moved_too_far_up_or_down():
    for fresh in (1.20, 0.80):  # +20% (مطاردة) / -20% (بينهار)
        r, engine, _ = _run(fresh_price=fresh)
        assert not engine.open_positions
        assert r.rejection_counts == {"entry_price_drifted": 1}
        assert r.candidate_details[-1]["entry_drift_pct"] == round((fresh - 1) * 100, 2)


def test_entry_rejected_when_fresh_price_unavailable():
    r, engine, _ = _run(fresh_price=None)
    assert not engine.open_positions
    assert r.rejection_counts == {"entry_price_unavailable": 1}


def test_price_refresh_error_counts_as_execution_error():
    r, engine, _ = _run(fresh_price=1.0, raise_error=True)
    assert not engine.open_positions
    assert engine.risk_engine.daily_state.consecutive_errors == 1  # خطأ شبكة حقيقي بيتعدّ


def test_refresh_can_be_disabled_by_config():
    c = DiscoveredCandidate(address="A1", symbol="M", pair=healthy_pair(address="A1", price="1.0"))
    engine = build_engine()
    dex = _PriceDex(price=5.0)  # كان هيترفض (drift 400%)
    orch = build_orchestrator(_FakeDiscovery([c]), dex, _FakeRugCheckClient(), engine=engine)
    orch.config["max_entry_price_drift_pct"] = 0
    orch.run_cycle()
    assert "A1" in engine.open_positions and dex.calls == 0  # مفيش طلب تحديث أصلًا
    assert engine.open_positions["A1"].position.entry_price == 1.0


def test_no_refresh_request_for_candidates_no_live_profile_approved():
    """التحديث مكلّف (طلب شبكة) فبيحصل بس للمرشح اللي فعلًا هيدخل."""
    bad = healthy_pair(address="B1", price="1.0")
    bad["liquidity"] = {"usd": 50000}
    c = DiscoveredCandidate(address="B1", symbol="B", pair=bad)
    engine = build_engine()
    for p in engine.profiles.values():
        p.shadow_only = True  # مفيش مسار شغّال => مفيش دخول => مفيش تحديث
    dex = _PriceDex(price=1.0)
    orch = build_orchestrator(_FakeDiscovery([c]), dex, _FakeRugCheckClient(), engine=engine)
    orch.run_cycle()
    assert dex.calls == 0


# ---------------- shadow rules ----------------

def _rec(m5, h1=None, stage="approved", score=70.0):
    return {"address": "A", "profiles": {"conservative": {"stage": stage, "scanner_score": score}},
            "context": {"price_change_m5": m5, "price_change_h1": h1}, "followups": {}}


def test_rule_bounds_min_inclusive_max_exclusive():
    rule = {"passed_hard_filters": True, "m5_min": -5, "m5_max": 5}
    assert rule_matches(rule, _rec(-5)) and rule_matches(rule, _rec(0)) and rule_matches(rule, _rec(4.99))
    assert not rule_matches(rule, _rec(5)) and not rule_matches(rule, _rec(-5.01))


def test_rule_requires_hard_filters_and_handles_missing_context():
    rule = {"passed_hard_filters": True, "m5_min": -5, "m5_max": 5}
    assert not rule_matches(rule, _rec(0, stage="hard_filter_failed"))
    assert not rule_matches(rule, _rec(None))        # قيمة ناقصة => مش داخل القاعدة
    assert not rule_matches(rule, _rec("abc"))
    assert rule_matches(rule, _rec("1.5"))           # DexScreener ممكن يرجّع string


def test_rule_h1_and_score_conditions():
    rule = {"m5_min": -5, "m5_max": 5, "h1_min": 10, "min_scanner_score": 65}
    assert rule_matches(rule, _rec(0, h1=12, score=70))
    assert not rule_matches(rule, _rec(0, h1=5, score=70))
    assert not rule_matches(rule, _rec(0, h1=12, score=60))


def test_summary_includes_rule_groups_over_existing_records():
    log = ShadowLog(path=os.path.join(tempfile.mkdtemp(), "s.json"), horizons_min=(30,),
                    rules={"calm": {"passed_hard_filters": True, "m5_min": -5, "m5_max": 5},
                           "chasing": {"passed_hard_filters": True, "m5_min": 15}})
    calm, chase = _rec(1), _rec(30)
    calm["address"], chase["address"] = "C", "P"
    calm["followups"] = {"30": {"return_pct": -5.0}}
    chase["followups"] = {"30": {"return_pct": -60.0}}
    log.completed = [calm, chase]
    g = log.summarize()["groups"]
    assert g["rule:calm"]["records"] == 1 and g["rule:chasing"]["records"] == 1
    assert g["rule:calm"]["by_horizon"]["30"]["median_return_pct"] == -5.0
    assert g["rule:chasing"]["by_horizon"]["30"]["median_return_pct"] == -60.0


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
