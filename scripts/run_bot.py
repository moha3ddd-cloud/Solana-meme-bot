#!/usr/bin/env python3
"""
نقطة التشغيل الفعلية — السكريبت اللي هيشتغل على الـVPS (cron أو
systemd service أو حتى `python3 scripts/run_bot.py` جوه `tmux`).

⚠️ ده لسه Paper Trading بس. مفيش توقيع حقيقي، مفيش فلوس حقيقية.
الهدف: يفضل شغال، يجمع قرارات حقيقية على عملات حقيقية، وتراجع
النتايج من الـDashboard بعد فترة.

الاستخدام:
    python3 scripts/run_bot.py

وقف السكريبت: Ctrl+C (بيحفظ آخر حالة قبل ما يقفل).
"""

import logging
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.dashboard_export import save_dashboard_snapshot
from core.data.dexscreener_client import DexScreenerClient
from core.data.rugcheck_client import RugCheckClient
from core.discovery import DiscoveryLayer, SeenTokensStore
from core.orchestrator import Orchestrator
from core.paper_trading import PaperTradingEngine, StrategyProfile
from core.risk_engine import RiskEngine
from core.scanner import ScannerEngine
from core.strategy import PositionManager, SignalEngine

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("data/bot.log"),
    ],
)
logger = logging.getLogger("run_bot")

CYCLE_INTERVAL_SECONDS = 60  # كل دقيقة — عدّل حسب rate limits المصادر المفعّلة


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

    dex_client = DexScreenerClient()
    rugcheck_client = RugCheckClient()  # بدون API key = rate limit أقل (10/min بدل 60/min)
    discovery = DiscoveryLayer(
        client=dex_client,
        seen_store=SeenTokensStore(db_path="data/discovery_seen.db"),
        config_path="config/discovery_config.yaml",
    )
    engine = build_engine()
    orchestrator = Orchestrator(
        discovery=discovery, dex_client=dex_client, rugcheck_client=rugcheck_client,
        engine=engine, config_path="config/orchestrator_config.yaml",
    )

    logger.info("Bot started — Paper Trading mode. Ctrl+C to stop.")
    cycle_number = 0
    try:
        while True:
            cycle_number += 1
            try:
                result = orchestrator.run_cycle()
                logger.info(
                    f"Cycle {cycle_number}: discovered={result.candidates_discovered} "
                    f"evaluated={result.candidates_evaluated} "
                    f"opened={len(result.positions_opened)} closed={len(result.positions_closed)} "
                    f"emergency={result.emergency_triggered}"
                )
                if result.emergency_triggered:
                    logger.warning(f"EMERGENCY MODE TRIGGERED: {result.emergency_reason}")
                if result.errors:
                    for err in result.errors:
                        logger.warning(f"Cycle error: {err}")

                save_dashboard_snapshot(engine, path="dashboard/snapshot.json")
            except Exception:
                logger.exception(f"Unhandled error in cycle {cycle_number} — continuing to next cycle")

            time.sleep(CYCLE_INTERVAL_SECONDS)
    except KeyboardInterrupt:
        logger.info("Stopped by user (Ctrl+C). Final snapshot saved.")
        save_dashboard_snapshot(engine, path="dashboard/snapshot.json")


if __name__ == "__main__":
    main()
