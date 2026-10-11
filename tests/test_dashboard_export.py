"""
اختبارات Dashboard Export — تأكيد إن الـJSON الناتج فيه كل حاجة
الـdashboard محتاجاها وبالشكل الصح.

تشغيل: python3 tests/test_dashboard_export.py
"""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.dashboard_export import build_dashboard_snapshot, save_dashboard_snapshot
from core.paper_trading import PaperTradingEngine, StrategyProfile
from core.risk_engine import RiskEngine
from core.scanner import ScannerEngine
from core.strategy import PositionManager, SignalEngine
from tests.test_scanner import healthy_token
from tests.test_strategy import strong_signals

RISK_CONFIG = os.path.join(os.path.dirname(__file__), "..", "config", "risk_config.yaml")
EXIT_RULES_CONSERVATIVE = os.path.join(os.path.dirname(__file__), "..", "config", "exit_rules_conservative.yaml")
SCANNER_CONFIG = os.path.join(os.path.dirname(__file__), "..", "config", "scanner_config.yaml")
STRATEGY_CONFIG = os.path.join(os.path.dirname(__file__), "..", "config", "strategy_config.yaml")


def engine_with_one_closed_trade() -> PaperTradingEngine:
    profile = StrategyProfile(
        signal_engine=SignalEngine(config_path=STRATEGY_CONFIG),
        position_manager=PositionManager(exit_rules_config_path=EXIT_RULES_CONSERVATIVE),
    )
    engine = PaperTradingEngine(
        risk_engine=RiskEngine(config_path=RISK_CONFIG),
        scanner=ScannerEngine(config_path=SCANNER_CONFIG),
        profiles={"conservative": profile},
    )
    engine.evaluate_candidate("conservative", healthy_token(), strong_signals(), entry_price=1.0, stop_loss_price=0.9)
    address = healthy_token().address
    engine.update_position(address, current_price=1.30)  # partial take profit، لسه مفتوح
    return engine


def test_snapshot_has_required_top_level_keys():
    engine = engine_with_one_closed_trade()
    snapshot = build_dashboard_snapshot(engine)
    for key in ["portfolio", "risk_status", "stats", "open_positions", "trade_log", "config", "drawdown_tiers"]:
        assert key in snapshot


def test_snapshot_reflects_open_position():
    engine = engine_with_one_closed_trade()
    snapshot = build_dashboard_snapshot(engine)
    assert snapshot["stats"]["open_positions_count"] == 1
    assert len(snapshot["open_positions"]) == 1
    assert snapshot["open_positions"][0]["symbol"] == "GOOD"
    assert snapshot["open_positions"][0]["targets_hit"] == [0]


def test_snapshot_trade_log_includes_entry_and_partial_exit():
    engine = engine_with_one_closed_trade()
    snapshot = build_dashboard_snapshot(engine)
    actions = [e["action"] for e in snapshot["trade_log"]]
    assert "entry" in actions
    assert "partial_exit" in actions


def test_snapshot_is_json_serializable():
    engine = engine_with_one_closed_trade()
    snapshot = build_dashboard_snapshot(engine)
    serialized = json.dumps(snapshot)  # لازم مايرميش exception
    assert len(serialized) > 0


def test_save_dashboard_snapshot_writes_valid_file():
    engine = engine_with_one_closed_trade()
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "snapshot.json")
        save_dashboard_snapshot(engine, path=path)
        with open(path, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        assert loaded["portfolio"]["starting_capital_usd"] == 1000.0


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
