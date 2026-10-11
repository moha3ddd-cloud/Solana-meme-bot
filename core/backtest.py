"""
Backtest Engine
==================
بيعيد تشغيل بيانات تاريخية حقيقية (شموع OHLCV من GeckoTerminal أو
DataLogger) عبر **نفس كود Paper Trading بالظبط** — مش محاكاة منفصلة
بمنطق مكرر. السؤال اللي بيجاوب عليه: "لو كان البوت شغال وقت الفترة
التاريخية دي، كان هيعمل إيه؟"

⚠️⚠️ حدود مهمة لازم تفهمها قبل ما تثق في أي نتيجة من هنا — دي مش
تفاصيل تقنية ثانوية، دي حاجات بتأثر على معنى النتيجة نفسها:

1. **الـScanner (الأمان) بيستخدم لقطة واحدة ثابتة من حالة العملة
   الحالية** (سيولة، توزيع حاملين، mint authority...) على طول
   الفترة التاريخية كلها. السبب: معندناش RugCheck تاريخي — الـAPI
   المجاني مابيوفروش "حالة العملة وقت كذا" في الماضي. لو العملة كانت
   فعلًا أخطر وقتها (مثلًا mint authority كان لسه فعّال)، الـBacktest
   مش هيعرف ده. يعني النتيجة دي بتقيّم **منطق التوقيت والخروج**
   أساسًا، مش دليل كامل على أداء الـScanner التاريخي.

2. **عدد صفقات الشراء/البيع الفعلي (buys/sells) مش متاح من شموع
   OHLCV خالص** — بنستخدم proxy: عدد الشموع الصاعدة مقابل الهابطة في
   نفس النافذة الزمنية، كتقريب لضغط الشراء/البيع. ده أقل دقة من
   بيانات buys/sells الحقيقية المستخدمة في Live/Paper Trading.

3. **السعر المستخدم لفحص الستوب/الأهداف داخل كل شمعة:** بنفحص الستوب
   بأسوأ سعر في الشمعة (low) الأول، وبعدين الأهداف بأحسن سعر (high) —
   ده أدق من الاكتفاء بسعر الإغلاق بس، لكنه لسه تقريب (مش كل حركة
   السعر جوه الشمعة معروفة بالتفصيل).

4. **price_change_15m و breakout_confirmation فعليًا أدق من Live
   mode** — عندنا تاريخ حقيقي متراكم هنا، فمفيش تقريب الـ70/30
   المستخدم في core/data/mappers.py للبيانات اللحظية.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from core.data.data_logger import OHLCCandle
from core.paper_trading import PaperTradingEngine
from core.scanner import TokenMetrics
from core.strategy import TimingSignals


@dataclass
class BacktestResult:
    symbol: str
    total_candles: int
    warmup_candles: int
    trades_opened: int = 0
    trades_closed: int = 0
    total_pnl_usd: float = 0.0
    win_count: int = 0
    loss_count: int = 0
    final_cash_usd: float = 0.0
    position_open_at_end: bool = False  # صفقة لسه مفتوحة آخر الفترة — مش محسوبة ضمن win/loss (unrealized)
    closed_trades: list = field(default_factory=list)

    @property
    def win_rate_pct(self) -> float:
        total = self.win_count + self.loss_count
        return (self.win_count / total * 100) if total else 0.0


def _build_timing_signals(
    candles: list, i: int, symbol: str, address: str,
    window_5m: int, window_15m: int, window_1h: int,
) -> TimingSignals:
    current = candles[i]

    def pct_change(window: int) -> float:
        past_idx = max(0, i - window)
        past_close = candles[past_idx].close
        if past_close == 0:
            return 0.0
        return ((current.close - past_close) / past_close) * 100

    def sum_volume(window: int) -> float:
        start = max(0, i - window + 1)
        return sum(c.volume_usd for c in candles[start:i + 1])

    # proxy لضغط الشراء/البيع — عدد الشموع الصاعدة/الهابطة، مش عدد صفقات حقيقي
    start_5m = max(0, i - window_5m + 1)
    window_candles = candles[start_5m:i + 1]
    buys_proxy = sum(1 for c in window_candles if c.close >= c.open)
    sells_proxy = sum(1 for c in window_candles if c.close < c.open)

    lookback = min(i, 20)
    if lookback > 0:
        recent_high = max(c.high for c in candles[i - lookback:i])
        broke_high = current.close > recent_high
        avg_volume = sum(c.volume_usd for c in candles[i - lookback:i]) / lookback
        volume_multiple = (current.volume_usd / avg_volume) if avg_volume > 0 else 1.0
    else:
        broke_high = False
        volume_multiple = 1.0

    return TimingSignals(
        symbol=symbol, address=address,
        price_change_5m_pct=pct_change(window_5m),
        price_change_15m_pct=pct_change(window_15m),
        volume_5m_usd=sum_volume(window_5m),
        volume_15m_usd=sum_volume(window_15m),
        volume_1h_usd=sum_volume(window_1h),
        buys_last_5m=buys_proxy,
        sells_last_5m=sells_proxy,
        broke_recent_high=broke_high,
        breakout_volume_multiple=volume_multiple,
    )


class BacktestEngine:
    def __init__(self, engine: PaperTradingEngine, candle_interval_minutes: float, initial_stop_loss_pct: float = 10.0):
        """
        engine: PaperTradingEngine حقيقي (بإعدادات الإنتاج) — بيتشغّل
        فعليًا، نفس كود Paper Trading بالظبط — مش نسخة مبسّطة.
        candle_interval_minutes: المسافة الزمنية بين كل شمعة والتانية.
        """
        self.engine = engine
        self.candle_interval_minutes = candle_interval_minutes
        self.initial_stop_loss_pct = initial_stop_loss_pct
        self.window_5m = max(1, round(5 / candle_interval_minutes))
        self.window_15m = max(1, round(15 / candle_interval_minutes))
        self.window_1h = max(1, round(60 / candle_interval_minutes))

    def run(self, candles: list, token_metrics_snapshot: TokenMetrics) -> BacktestResult:
        symbol = token_metrics_snapshot.symbol
        address = token_metrics_snapshot.address
        warmup = self.window_1h

        result = BacktestResult(symbol=symbol, total_candles=len(candles), warmup_candles=warmup)

        if len(candles) <= warmup:
            result.final_cash_usd = self.engine.cash_usd
            return result  # مش كفاية بيانات حتى لفترة الـwarm-up

        for i in range(warmup, len(candles)):
            current = candles[i]

            if address in self.engine.open_positions:
                was_open = True
                # الستوب الأول بأسوأ سعر (low)، بعدين الأهداف بأحسن سعر (high)
                self.engine.update_position(address, current_price=current.low)
                if address in self.engine.open_positions:
                    self.engine.update_position(address, current_price=current.high)
                if was_open and address not in self.engine.open_positions:
                    result.trades_closed += 1
                continue

            can_trade, _ = self.engine.risk_engine.can_open_new_trade()
            if not can_trade:
                continue

            signals = _build_timing_signals(
                candles, i, symbol, address, self.window_5m, self.window_15m, self.window_1h,
            )
            entry_price = current.close
            stop_loss_price = entry_price * (1 - self.initial_stop_loss_pct / 100)

            decision = self.engine.evaluate_candidate_auto(
                token_metrics_snapshot, signals, entry_price, stop_loss_price,
            )
            if decision.opened:
                result.trades_opened += 1

        result.position_open_at_end = address in self.engine.open_positions
        result.final_cash_usd = self.engine.cash_usd
        result.closed_trades = list(self.engine.closed_trades)
        result.win_count = sum(1 for t in result.closed_trades if t["total_pnl_usd"] > 0)
        result.loss_count = sum(1 for t in result.closed_trades if t["total_pnl_usd"] <= 0)
        result.total_pnl_usd = sum(t["total_pnl_usd"] for t in result.closed_trades)

        return result
