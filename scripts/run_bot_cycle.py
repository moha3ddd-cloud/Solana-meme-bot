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

import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.cycle_log import update_cycle_history
from core.dashboard_export import save_dashboard_snapshot
from core.data.dexscreener_client import DexScreenerClient
from core.data.rugcheck_client import RugCheckClient
from core.discovery import DiscoveryLayer, SeenTokensStore
from core.orchestrator import Orchestrator
from core.paper_trading import PaperTradingEngine, StrategyProfile
from core.risk_engine import RiskEngine
from core.shadow_log import ShadowLog
from core.scanner import ScannerEngine
from core.strategy import PositionManager, SignalEngine

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("run_bot_cycle")

STATE_PATH = "data/engine_state.json"

# مراقبة لصيقة للصفقات المفتوحة بعد الدورة (شوف Orchestrator.watch_positions).
# 0 = مقفولة (التشغيل المحلي العادي). الـworkflow بيحط WATCH_MAX_MINUTES.
WATCH_MAX_MINUTES = float(os.environ.get("WATCH_MAX_MINUTES", "0") or 0)
WATCH_INTERVAL_SECONDS = float(os.environ.get("WATCH_INTERVAL_SECONDS", "60") or 60)
SNAPSHOT_PATH = "dashboard/snapshot.json"
SHADOW_LOG_PATH = "data/shadow_log.json"
ORCHESTRATOR_CONFIG_PATH = "config/orchestrator_config.yaml"


def load_orchestrator_config() -> dict:
    with open(ORCHESTRATOR_CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def build_engine(shadow_only_profiles=()) -> PaperTradingEngine:
    engine = PaperTradingEngine(
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
    for name in shadow_only_profiles:
        if name in engine.profiles:
            engine.profiles[name].shadow_only = True
    return engine


def main():
    os.makedirs("data", exist_ok=True)
    os.makedirs("dashboard", exist_ok=True)

    config = load_orchestrator_config()
    shadow_only = config.get("shadow_only_profiles") or []
    engine = build_engine(shadow_only_profiles=shadow_only)
    logger.info(f"Shadow-only profiles (evaluated, never traded): {shadow_only or 'none'}")
    shadow_log = ShadowLog(path=SHADOW_LOG_PATH,
                           horizons_min=config.get("shadow_horizons_minutes") or (30, 60, 120))

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
        engine=engine, config_path=ORCHESTRATOR_CONFIG_PATH, shadow_log=shadow_log,
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

    def save_state():
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(engine.export_state(), f, indent=2)

    save_state()

    # لو فيه صفقات مفتوحة (قديمة أو لسه مفتوحة الدورة دي) نراقبها لصيقًا
    if WATCH_MAX_MINUTES > 0 and engine.open_positions and not result.emergency_triggered:
        logger.info(f"Watching {len(engine.open_positions)} open position(s) for up to "
                    f"{WATCH_MAX_MINUTES:g} min (every {WATCH_INTERVAL_SECONDS:g}s)")
        summary = orchestrator.watch_positions(
            result, max_seconds=WATCH_MAX_MINUTES * 60, interval_seconds=WATCH_INTERVAL_SECONDS,
            on_change=save_state,
        )
        logger.info(f"Watch done: {summary}")
        if result.emergency_triggered:
            logger.warning(f"EMERGENCY MODE TRIGGERED during watch: {result.emergency_reason}")
        save_state()

    # سجل الدورات: بنقرا الـsnapshot القديم ونضيف عليه (cycle_log + cycle_totals)
    previous_snapshot = None
    if os.path.exists(SNAPSHOT_PATH):
        try:
            with open(SNAPSHOT_PATH, "r", encoding="utf-8") as f:
                previous_snapshot = json.load(f)
        except (OSError, ValueError) as e:
            logger.warning(f"Could not read previous snapshot, starting a new cycle log: {e}")
    shadow_log.save()
    history = update_cycle_history(previous_snapshot, result.to_log_entry(), datetime.now().isoformat())
    history["shadow_summary"] = shadow_log.summarize()
    logger.info(f"Cycle detail: discovery={result.discovery_stats} rejections={result.rejection_counts}")

    save_dashboard_snapshot(engine, path=SNAPSHOT_PATH, extra=history)
    logger.info("State and dashboard snapshot saved.")


if __name__ == "__main__":
    main()
