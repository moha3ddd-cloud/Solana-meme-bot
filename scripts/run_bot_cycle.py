#!/usr/bin/env python3
"""
نقطة تشغيل بديلة لـscripts/run_bot.py — دورة واحدة بس، تقفل بعدها.

مصممة لـGitHub Actions (أو أي scheduler تاني بياخد process جديد كل
مرة، عكس VPS اللي فيه process واحد شغال باستمرار). الحالة (الصفقات
المفتوحة، الكاش، الـRiskEngine) بتتحفظ في data/engine_state.json بعد
كل دورة، وبتتقرا تاني أول حاجة في الدورة الجاية — يعني مفيش فقدان
بيانات بين التشغيلتين.

⚠️ لسه Paper Trading بس. الـworkflow المسؤول عن استدعاء السكريبت ده
دوريًا وعمل commit للحالة الجديدة هو .github/workflows/run_bot.yml.
"""

import json
import logging
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.cycle_log import update_cycle_history
from core.dashboard_export import save_dashboard_snapshot
from core.data.dexscreener_client import DexScreenerClient
from core.data.rugcheck_client import RugCheckClient
from core.discovery import DiscoveryLayer, SeenTokensStore
from core.orchestrator import Orchestrator
from core.paper_trading import PaperTradingEngine, StrategyProfile
from core.risk_engine import RiskEngine
from core.scanner import ScannerEngine
from core.strategy import PositionManager, SignalEngine

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("run_bot_cycle")

STATE_PATH = "data/engine_state.json"
SNAPSHOT_PATH = "dashboard/snapshot.json"


def build_engine() -> PaperTradingEngine:
    return PaperTradingEngine(
        risk_engine=RiskEngine(config_path="config/risk_config.yaml"),
        scanner=ScannerEngine(config_path="config/scanner_config.yaml"),
        profiles={
            "conservative": StrategyProfile(
                signal_engine=SignalEngine(config_path="config/strategy_config.yaml"),
                position_manager=PositionManager(exit_rules_config_path="config/exit_rules_conservative.yaml"),
                scanner=ScannerEngine(config_path="config/scanner_config.yaml"),
            ),
            "scalp": StrategyProfile(
                signal_engine=SignalEngine(config_path="config/strategy_scalp_config.yaml"),
                position_manager=PositionManager(exit_rules_config_path="config/exit_rules_scalp.yaml"),
                scanner=ScannerEngine(config_path="config/scanner_scalp_config.yaml"),
            ),
        },
    )


def main():
    os.makedirs("data", exist_ok=True)
    os.makedirs("dashboard", exist_ok=True)

    engine = build_engine()

    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            saved_state = json.load(f)
        engine.import_state(saved_state)
        logger.info(f"Resumed state: cash=${engine.cash_usd:.2f}, open_positions={len(engine.open_positions)}")
    else:
        logger.info("No saved state found — starting fresh")

    dex_client = DexScreenerClient()
    rugcheck_client = RugCheckClient()
    discovery = DiscoveryLayer(
        client=dex_client,
        seen_store=SeenTokensStore(db_path="data/discovery_seen.db"),
        config_path="config/discovery_config.yaml",
    )
    orchestrator = Orchestrator(
        discovery=discovery, dex_client=dex_client, rugcheck_client=rugcheck_client,
        engine=engine, config_path="config/orchestrator_config.yaml",
    )

    result = orchestrator.run_cycle()
    logger.info(
        f"Cycle done: discovered={result.candidates_discovered} evaluated={result.candidates_evaluated} "
        f"opened={len(result.positions_opened)} closed={len(result.positions_closed)} "
        f"emergency={result.emergency_triggered}"
    )
    if result.emergency_triggered:
        logger.warning(f"EMERGENCY MODE TRIGGERED: {result.emergency_reason}")
    for err in result.errors:
        logger.warning(f"Cycle error: {err}")

    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(engine.export_state(), f, indent=2)

    # سجل الدورات: بنقرا الـsnapshot القديم ونضيف عليه (cycle_log + cycle_totals)
    previous_snapshot = None
    if os.path.exists(SNAPSHOT_PATH):
        try:
            with open(SNAPSHOT_PATH, "r", encoding="utf-8") as f:
                previous_snapshot = json.load(f)
        except (OSError, ValueError) as e:
            logger.warning(f"Could not read previous snapshot, starting a new cycle log: {e}")
    history = update_cycle_history(previous_snapshot, result.to_log_entry(), datetime.now().isoformat())
    logger.info(f"Cycle detail: discovery={result.discovery_stats} rejections={result.rejection_counts}")

    save_dashboard_snapshot(engine, path=SNAPSHOT_PATH, extra=history)
    logger.info("State and dashboard snapshot saved.")


if __name__ == "__main__":
    main()
