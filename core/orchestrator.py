"""
Main Loop / Orchestration
============================
الحلقة اللي بتربط كل طبقة بنيناها لحد دلوقتي في دورة واحدة مستمرة:

  تحديث المفتوح -> Emergency Check -> Discovery -> بناء بيانات حقيقية
  لكل مرشح -> تقييم دخول (Scanner+SignalEngine عبر PaperTradingEngine)

ده أول مكان في المشروع بيشغّل كل الطبقات مع بعض تلقائيًا، بدل ما نربط
كل حاجة يدويًا زي ما كنا بنعمل في التجارب اليدوية.

⚠️ لسه Paper Trading بس — مفيش توقيع حقيقي هنا ولا تنفيذ فعلي على
الشبكة. الهدف إن السكريبت يفضل شغال على VPS، يجمع نتايج حقيقية من
قرارات حقيقية على عملات حقيقية، من غير فلوس حقيقية لسه.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

import yaml

from core.data.dexscreener_client import DexScreenerClient
from core.data.mappers import build_dex_fields, pair_to_timing_signals
from core.data.onchain_mappers import build_onchain_fields, merge_into_token_metrics
from core.data.rugcheck_client import RugCheckClient
from core.discovery import DiscoveredCandidate, DiscoveryLayer
from core.paper_trading import PaperTradingEngine

logger = logging.getLogger("orchestrator")


@dataclass
class CycleResult:
    candidates_discovered: int = 0
    candidates_evaluated: int = 0
    positions_opened: list = field(default_factory=list)
    positions_closed: list = field(default_factory=list)
    emergency_triggered: bool = False
    emergency_reason: Optional[str] = None
    errors: list = field(default_factory=list)


class Orchestrator:
    def __init__(
        self,
        discovery: DiscoveryLayer,
        dex_client: DexScreenerClient,
        rugcheck_client: RugCheckClient,
        engine: PaperTradingEngine,
        config_path: str = "config/orchestrator_config.yaml",
    ):
        self.discovery = discovery
        self.dex_client = dex_client
        self.rugcheck_client = rugcheck_client
        self.engine = engine
        with open(config_path, "r", encoding="utf-8") as f:
            self.config = yaml.safe_load(f)

    # ------------------------------------------------------------------
    # تحديث المراكز المفتوحة بأحدث سعر — بيرجع dict العناوين->أسعار،
    # نفس الـdict ده بيتستخدم بعدين في run_emergency_check عشان منعملش
    # نفس طلبات الشبكة مرتين
    # ------------------------------------------------------------------
    def _fetch_current_price(self, address: str) -> Optional[float]:
        try:
            pairs = self.dex_client.get_pairs_by_token_addresses([address])
            if not pairs:
                return None
            price = pairs[0].get("priceUsd")
            return float(price) if price is not None else None
        except Exception as e:
            self.engine.risk_engine.record_execution_error()
            logger.warning(f"price_fetch_failed address={address} error={e}")
            return None

    def _update_open_positions(self, result: CycleResult) -> dict:
        current_prices = {}
        for address in list(self.engine.open_positions.keys()):
            price = self._fetch_current_price(address)
            if price is None:
                continue
            current_prices[address] = price
            still_open_before = address in self.engine.open_positions
            self.engine.update_position(address, current_price=price)
            if still_open_before and address not in self.engine.open_positions:
                result.positions_closed.append(address)
        return current_prices

    # ------------------------------------------------------------------
    # بناء TokenMetrics + TimingSignals حقيقية لمرشح واحد (DexScreener
    # زي ما جاله من الـDiscoveryLayer + RugCheck جديد)
    # ------------------------------------------------------------------
    def _build_token_and_signals(self, candidate: DiscoveredCandidate):
        pair = candidate.pair
        dex_fields = build_dex_fields(pair)

        try:
            report = self.rugcheck_client.get_token_report(candidate.address)
        except Exception as e:
            self.engine.risk_engine.record_execution_error()
            logger.warning(f"rugcheck_failed address={candidate.address} error={e}")
            return None

        onchain_fields = build_onchain_fields(report)
        token = merge_into_token_metrics(dex_fields, onchain_fields)
        signals = pair_to_timing_signals(pair)
        return token, signals

    def _entry_price_and_stop(self, pair: dict):
        price = pair.get("priceUsd")
        if price is None:
            return None
        entry_price = float(price)
        if entry_price <= 0:
            return None
        stop_pct = self.config["default_initial_stop_loss_pct"] / 100.0
        stop_loss_price = entry_price * (1 - stop_pct)
        return entry_price, stop_loss_price

    # ------------------------------------------------------------------
    # دورة واحدة كاملة — دي اللي بتتنادى من scripts/run_bot.py جوه loop
    # فيه sleep بين كل دورة والتانية
    # ------------------------------------------------------------------
    def run_cycle(self) -> CycleResult:
        result = CycleResult()

        # 1) المفتوح أولًا — أهم من اكتشاف جديد
        current_prices = self._update_open_positions(result)

        # 2) Emergency Check بأحدث الأسعار المتاحة
        emergency = self.engine.run_emergency_check(current_prices)
        result.emergency_triggered = emergency["triggered"]
        result.emergency_reason = emergency.get("reason")
        for addr in emergency.get("closed_addresses", []):
            if addr not in result.positions_closed:
                result.positions_closed.append(addr)

        if result.emergency_triggered:
            # لو الطوارئ اتفعّلت، نوقف هنا — مفيش داعي نفتح صفقات جديدة
            # في نفس الدورة اللي قفلنا فيها كل حاجة لسبب خطير
            return result

        # 3) اكتشاف عملات جديدة
        try:
            candidates = self.discovery.discover()
        except Exception as e:
            self.engine.risk_engine.record_execution_error()
            result.errors.append(f"discovery_failed:{e}")
            candidates = []
        result.candidates_discovered = len(candidates)

        # 4) تقييم كل مرشح
        for candidate in candidates:
            can_trade, _ = self.engine.risk_engine.can_open_new_trade()
            if not can_trade:
                break  # ممنوعين نفتح صفقات جديدة أصلًا — مفيش داعي نكمل

            built = self._build_token_and_signals(candidate)
            if built is None:
                continue
            token, signals = built
            result.candidates_evaluated += 1

            entry_stop = self._entry_price_and_stop(candidate.pair)
            if entry_stop is None:
                continue
            entry_price, stop_loss_price = entry_stop

            decision = self.engine.evaluate_candidate_auto(token, signals, entry_price, stop_loss_price)
            if decision.opened:
                result.positions_opened.append(candidate.address)

        return result
