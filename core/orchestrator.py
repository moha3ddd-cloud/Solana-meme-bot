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
import time
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
    # --- شفافية: ليه مفيش صفقات؟ (بتتسجل في الـsnapshot) ---
    discovery_stats: dict = field(default_factory=dict)
    rejection_counts: dict = field(default_factory=dict)   # سبب الرفض -> عدد
    rugcheck_failures: int = 0
    hard_filter_reasons: dict = field(default_factory=dict)  # سبب فشل Hard Filter -> عدد (من كل المسارات)
    candidate_details: list = field(default_factory=list)  # أول N مرشح: symbol/scores/reason
    watch: dict = field(default_factory=dict)  # ملخص المراقبة اللصيقة (polls/closed) لو اشتغلت

    def to_log_entry(self, max_details: int = 10) -> dict:
        """ملخص دورة واحدة جاهز للـJSON (من غير timestamp — الـcaller بيضيفه)."""
        return {
            "discovery": self.discovery_stats,
            "candidates_discovered": self.candidates_discovered,
            "candidates_evaluated": self.candidates_evaluated,
            "rugcheck_failures": self.rugcheck_failures,
            "rejections": self.rejection_counts,
            "hard_filter_reasons": self.hard_filter_reasons,
            "opened": list(self.positions_opened),
            "closed": list(self.positions_closed),
            "emergency": self.emergency_reason if self.emergency_triggered else None,
            "errors": list(self.errors),
            "details": self.candidate_details[:max_details],
            "watch": self.watch,
        }


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
            if price is None:
                return None
            # نجاح فعلي: نصفّر عدّاد الأخطاء المتتالية (كان بيزيد بس ومحدش بيصفّره)
            self.engine.risk_engine.record_execution_success()
            return float(price)
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

        self.engine.risk_engine.record_execution_success()
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
    # مراقبة لصيقة: لما فيه صفقة مفتوحة، نفحص السعر كل interval ثانية
    # لحد max_seconds بدل ما نستنى الدورة الجاية (نص ساعة). السبب: عملات
    # الميم بتتحرك أكتر من نسبة الستوب في دقايق (صفقتين Paper خسروا 12.9%
    # و29% على ستوب 10%). بتقف لما مفيش صفقات مفتوحة أو الوقت يخلص أو
    # الطوارئ تتفعّل. sleep_fn/clock_fn بيتبدلوا في الاختبارات.
    # ------------------------------------------------------------------
    def watch_positions(
        self,
        result: CycleResult,
        max_seconds: float,
        interval_seconds: float = 60.0,
        sleep_fn=time.sleep,
        clock_fn=time.monotonic,
        on_change=None,
    ) -> dict:
        start = clock_fn()
        polls = 0
        closed_during_watch: list = []
        stopped_because = "time_up"

        while True:
            if not self.engine.open_positions:
                stopped_because = "no_open_positions"
                break
            if clock_fn() - start >= max_seconds:
                break

            sleep_fn(interval_seconds)
            if clock_fn() - start > max_seconds + interval_seconds:
                break  # حماية لو الـsleep طوّل أكتر من المتوقع

            polls += 1
            before = set(self.engine.open_positions)
            prices = self._update_open_positions(result)
            emergency = self.engine.run_emergency_check(prices)
            closed_now = sorted(before - set(self.engine.open_positions))
            for addr in closed_now:
                closed_during_watch.append(addr)
                if addr not in result.positions_closed:
                    result.positions_closed.append(addr)

            if emergency["triggered"]:
                result.emergency_triggered = True
                result.emergency_reason = emergency.get("reason")
                stopped_because = "emergency"
                if on_change:
                    on_change()
                break
            if closed_now and on_change:
                on_change()  # نحفظ الحالة أول ما صفقة تتقفل (لو الـjob اتقطع بعدها)

        summary = {"polls": polls, "closed": closed_during_watch, "stopped_because": stopped_because,
                   "interval_seconds": interval_seconds}
        result.watch = summary
        return summary

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
        result.discovery_stats = dict(getattr(self.discovery, "last_stats", {}) or {})

        # 4) تقييم كل مرشح
        for candidate in candidates:
            can_trade, _ = self.engine.risk_engine.can_open_new_trade()
            if not can_trade:
                break  # ممنوعين نفتح صفقات جديدة أصلًا — مفيش داعي نكمل

            built = self._build_token_and_signals(candidate)
            if built is None:
                result.rugcheck_failures += 1
                result.rejection_counts["rugcheck_failed"] = result.rejection_counts.get("rugcheck_failed", 0) + 1
                continue
            token, signals = built
            result.candidates_evaluated += 1

            entry_stop = self._entry_price_and_stop(candidate.pair)
            if entry_stop is None:
                result.rejection_counts["no_valid_price"] = result.rejection_counts.get("no_valid_price", 0) + 1
                continue
            entry_price, stop_loss_price = entry_stop

            decision = self.engine.evaluate_candidate_auto(token, signals, entry_price, stop_loss_price)
            if decision.opened:
                result.positions_opened.append(candidate.address)
                reason = "opened"
            else:
                reason = decision.rejection_reason or "unknown"
            # نجمّع السبب بدون تفاصيل متغيرة (قبل أول ":") عشان العدّاد يبقى مقروء
            key = reason.split(":")[0] if reason != "opened" else "opened"
            result.rejection_counts[key] = result.rejection_counts.get(key, 0) + 1
            scan_reasons = decision.scanner_rejection_reasons or {}
            # كل سبب بيتعد مرة واحدة لكل مرشح (حتى لو المسارين رفضوه لنفس السبب)،
            # وبدون الأرقام المتغيرة اللي بين الأقواس عشان العدّاد يبقى مقروء
            unique_reasons = {x.split(" (")[0] for reasons in scan_reasons.values() for x in reasons}
            for r in unique_reasons:
                result.hard_filter_reasons[r] = result.hard_filter_reasons.get(r, 0) + 1
            result.candidate_details.append({
                "symbol": candidate.symbol,
                "address": candidate.address,
                "result": reason,
                "scanner_score": decision.scanner_score,
                "signal_score": decision.signal_score,
                "hard_filter_reasons": scan_reasons or None,
            })

        return result
