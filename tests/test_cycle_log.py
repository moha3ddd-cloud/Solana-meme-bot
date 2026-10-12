"""
اختبارات سجل الدورات (core/cycle_log.py) + إحصائيات الـDiscovery والـOrchestrator.

تشغيل: python3 tests/test_cycle_log.py
"""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.cycle_log import update_cycle_history
from core.dashboard_export import build_dashboard_snapshot
from core.discovery import DiscoveryLayer, SeenTokensStore
from core.orchestrator import CycleResult

from tests.test_orchestrator import (
    _FakeDexClient, _FakeDiscovery, _FakeRugCheckClient, build_engine, build_orchestrator, healthy_pair,
)
from core.discovery import DiscoveredCandidate

DISCOVERY_CONFIG = os.path.join(os.path.dirname(__file__), "..", "config", "discovery_config.yaml")


class _DexForDiscovery:
    def __init__(self, pairs, profiles=None):
        self.pairs, self.profiles = pairs, profiles or []

    def get_latest_token_profiles(self):
        return self.profiles

    def get_latest_token_boosts(self):
        return []

    def get_pairs_by_token_addresses(self, addresses):
        return [p for p in self.pairs if p["baseToken"]["address"] in addresses]


def _discovery(pairs):
    tmp = tempfile.mkdtemp()
    profiles = [{"tokenAddress": p["baseToken"]["address"]} for p in pairs]
    return DiscoveryLayer(
        client=_DexForDiscovery(pairs, profiles),
        seen_store=SeenTokensStore(db_path=os.path.join(tmp, "seen.db")),
        config_path=DISCOVERY_CONFIG,
    )


def test_discovery_stats_count_rejections_by_reason():
    good = healthy_pair(address="Good1")
    poor = healthy_pair(address="Poor1")
    poor["liquidity"] = {"usd": 10}
    old = healthy_pair(address="Old1", age_minutes=10_000_000)
    d = _discovery([good, poor, old])
    out = d.discover()
    st = d.last_stats
    assert st["addresses_collected"] == 3
    assert st["pairs_returned"] == 3
    assert st["passed_pre_filter"] == len(out) == 1
    assert st["pre_filter_rejected"].get("low_liquidity") == 1
    assert st["pre_filter_rejected"].get("too_old") == 1


def test_discovery_stats_count_recently_seen_on_second_run():
    d = _discovery([healthy_pair(address="Good1")])
    d.discover()
    d.discover()
    assert d.last_stats["skipped_recently_seen"] == 1
    assert d.last_stats["passed_pre_filter"] == 0


def test_cycle_history_accumulates_and_caps_log():
    entry = {
        "discovery": {"addresses_collected": 5, "pairs_returned": 4, "passed_pre_filter": 2,
                      "skipped_recently_seen": 1, "pre_filter_rejected": {"low_liquidity": 2}},
        "candidates_evaluated": 2, "rejections": {"scanner_hard_filter_failed": 2},
        "opened": [], "errors": [],
    }
    snap = None
    for i in range(5):
        h = update_cycle_history(snap, entry, f"t{i}", max_entries=3)
        snap = h
    assert len(h["cycle_log"]) == 3
    assert h["cycle_log"][-1]["timestamp"] == "t4"
    t = h["cycle_totals"]
    assert t["cycles"] == 5 and t["addresses_collected"] == 25
    assert t["pre_filter_rejected"]["low_liquidity"] == 10
    assert t["rejections"]["scanner_hard_filter_failed"] == 10


def test_cycle_history_survives_missing_or_broken_previous_snapshot():
    h = update_cycle_history({"unrelated": 1}, {"discovery": {}, "rejections": {}}, "t0")
    assert h["cycle_totals"]["cycles"] == 1
    assert len(h["cycle_log"]) == 1


def test_orchestrator_records_rejection_reasons_and_details():
    cands = [DiscoveredCandidate(address=f"A{i}", symbol=f"M{i}", pair=healthy_pair(address=f"A{i}")) for i in range(3)]
    disc = _FakeDiscovery(cands)
    disc.last_stats = {"addresses_collected": 9}
    orch = build_orchestrator(disc, _FakeDexClient(), _FakeRugCheckClient())
    r = orch.run_cycle()
    assert r.discovery_stats == {"addresses_collected": 9}
    assert sum(r.rejection_counts.values()) == 3  # كل مرشح اتسجّل له نتيجة (opened أو سبب رفض)
    assert len(r.candidate_details) == 3
    assert all("result" in d for d in r.candidate_details)
    entry = r.to_log_entry()
    json.dumps(entry)  # لازم يتحوّل JSON من غير مشاكل


def test_orchestrator_counts_rugcheck_failures():
    c = DiscoveredCandidate(address="A1", symbol="M", pair=healthy_pair(address="A1"))
    orch = build_orchestrator(_FakeDiscovery([c]), _FakeDexClient(), _FakeRugCheckClient(raise_error=True))
    r = orch.run_cycle()
    assert r.rugcheck_failures == 1
    assert r.rejection_counts == {"rugcheck_failed": 1}


def test_hard_filter_reasons_are_recorded_and_accumulated():
    pair = healthy_pair(address="Weak1")
    c = DiscoveredCandidate(address="Weak1", symbol="WEAK", pair=pair)
    orch = build_orchestrator(_FakeDiscovery([c]), _FakeDexClient(), _FakeRugCheckClient())
    # نخلّي السيولة تحت حد الـScanner (15000) عشان نضمن فشل Hard Filter
    c.pair["liquidity"] = {"usd": 12000}
    r = orch.run_cycle()
    assert r.rejection_counts.get("scanner_hard_filter_failed") == 1
    assert any("liquidity" in k for k in r.hard_filter_reasons), r.hard_filter_reasons
    assert r.candidate_details[0]["hard_filter_reasons"]
    h = update_cycle_history(update_cycle_history(None, r.to_log_entry(), "t0"), r.to_log_entry(), "t1")
    assert any(v == 2 for v in h["cycle_totals"]["hard_filter_reasons"].values())


def test_snapshot_includes_extra_cycle_history():
    engine = build_engine()
    extra = update_cycle_history(None, CycleResult().to_log_entry(), "t0")
    snap = build_dashboard_snapshot(engine, extra=extra)
    assert snap["cycle_totals"]["cycles"] == 1
    json.dumps(snap)
    assert "portfolio" in snap  # الحقول القديمة لسه موجودة


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
