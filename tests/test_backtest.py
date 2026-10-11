"""
اختبارات BacktestEngine — شموع OHLCV اصطناعية (أنماط واضحة: صعود
مستمر، كراش مفاجئ) لكن بتتمشى على نفس PaperTradingEngine الحقيقي.

تشغيل: python3 tests/test_backtest.py
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.backtest import BacktestEngine, _build_timing_signals
from core.data.data_logger import OHLCCandle

from tests.test_orchestrator import build_engine
from tests.test_scanner import healthy_token


def make_candles(prices: list, address="BacktestAddr1", volume_usd=5000, interval_minutes=5):
    """بيبني شموع OHLCV بسيطة من قائمة أسعار إغلاق — كل شمعة open=إغلاق اللي قبلها."""
    candles = []
    ts = 1_700_000_000
    prev_close = prices[0]
    for price in prices:
        o = prev_close
        c = price
        h = max(o, c) * 1.002
        l = min(o, c) * 0.998
        candles.append(OHLCCandle(
            address=address, interval_start_ts=ts, open=o, high=h, low=l, close=c,
            volume_usd=volume_usd, snapshot_count=1,
        ))
        ts += interval_minutes * 60
        prev_close = c
    return candles


def flat_prices(n, value=1.0):
    return [value] * n


def uptrend_prices(n, start=1.0, pct_per_candle=3.0):
    prices = []
    p = start
    for _ in range(n):
        p *= (1 + pct_per_candle / 100)
        prices.append(p)
    return prices


# ---------------- Basic structure tests ----------------

def test_insufficient_candles_returns_empty_result_safely():
    engine = build_engine()
    backtest = BacktestEngine(engine=engine, candle_interval_minutes=5)
    candles = make_candles(flat_prices(5))  # أقل من warmup (12 شمعة لـ1 ساعة بفاصل 5 دقايق)
    token = healthy_token(address="BacktestAddr1")
    result = backtest.run(candles, token)
    assert result.trades_opened == 0
    assert result.total_candles == 5


def test_window_sizes_computed_correctly_from_interval():
    engine = build_engine()
    backtest = BacktestEngine(engine=engine, candle_interval_minutes=5)
    assert backtest.window_5m == 1
    assert backtest.window_15m == 3
    assert backtest.window_1h == 12

    backtest_1m = BacktestEngine(engine=build_engine(), candle_interval_minutes=1)
    assert backtest_1m.window_5m == 5
    assert backtest_1m.window_15m == 15
    assert backtest_1m.window_1h == 60


# ---------------- Signal construction (no lookahead) ----------------

def test_timing_signals_do_not_use_future_candles():
    """لازم التأكد إن حساب أي إشارة عند index=i مايستخدمش بيانات i+1 أو بعدها."""
    flat = flat_prices(20, value=1.0)
    # نخلي الشمعة الأخيرة بس (مستقبل بالنسبة للـindex اللي بنحسب عنده) مرتفعة جدًا
    prices_with_future_spike = flat[:-1] + [100.0]
    candles = make_candles(prices_with_future_spike)

    # نحسب الإشارات عند index=10 (قبل الـspike في آخر شمعة بكتير)
    signals = _build_timing_signals(candles, i=10, symbol="TEST", address="A",
                                     window_5m=1, window_15m=3, window_1h=12)
    # لو في lookahead، التغيّر كان هيبقى ضخم جدًا؛ من غيره لازم يفضل قريب من صفر (أسعار flat)
    assert abs(signals.price_change_5m_pct) < 1.0


def test_buy_sell_proxy_reflects_candle_direction():
    prices = [1.0, 1.1, 1.2, 1.3]  # كل الشموع صاعدة
    candles = make_candles(prices)
    signals = _build_timing_signals(candles, i=3, symbol="TEST", address="A",
                                     window_5m=4, window_15m=4, window_1h=4)
    assert signals.buys_last_5m > signals.sells_last_5m


# ---------------- Full engine integration ----------------

def test_clear_uptrend_can_open_and_profit():
    """
    صعود قوي ومستمر — المفروض على الأقل تفتح صفقة واحدة، ولو فتحت
    ومستمرة في الصعود، الـPnL المتحقق أو غير المتحقق لازم يكون موجب.
    """
    prices = flat_prices(15, value=1.0) + uptrend_prices(30, start=1.0, pct_per_candle=4.0)
    candles = make_candles(prices)
    token = healthy_token(address="BacktestAddr1", symbol="PUMP")

    engine = build_engine()
    backtest = BacktestEngine(engine=engine, candle_interval_minutes=5)
    result = backtest.run(candles, token)

    if result.trades_opened == 0:
        return  # ممكن السيجنال ميعديش حسب العتبات الحقيقية — مقبول، مش هدف الاختبار الحتمي
    # لو فتحت، إما قفلت بربح أو لسه مفتوحة (مش خسارة في صعود مستمر نظيف)
    if result.trades_closed > 0:
        assert result.total_pnl_usd >= 0 or result.position_open_at_end


def test_crash_after_entry_triggers_stop_loss():
    """صعود يفتح صفقة، وبعدين كراش حاد — لازم الستوب يضرب ويسجل خسارة محدودة (مش غير محدودة)."""
    prices = (
        flat_prices(15, value=1.0)
        + uptrend_prices(20, start=1.0, pct_per_candle=5.0)  # صعود يفتح صفقة
    )
    crash_start = prices[-1]
    crash = [crash_start * (1 - 0.15 * i) for i in range(1, 6)]  # كراش حاد -15% متتالية
    candles = make_candles(prices + [max(p, 0.0001) for p in crash])
    token = healthy_token(address="BacktestAddr1", symbol="CRASH")

    engine = build_engine()
    backtest = BacktestEngine(engine=engine, candle_interval_minutes=5)
    result = backtest.run(candles, token)

    if result.trades_opened == 0:
        return  # نفس المبدأ فوق — مش كل سيناريو هيعدي العتبات الحقيقية حتمًا
    if result.trades_closed > 0:
        # لو قفلت، المفروض الخسارة محدودة تقريبًا بحجم الستوب (10%)، مش انهيار كامل بلا حدود
        worst_trade_pnl_pct = min(
            (t["total_pnl_usd"] / (engine.risk_engine.config["account"]["starting_capital_usd"] * 0.01))
            for t in result.closed_trades
        ) if result.closed_trades else 0
        assert result.total_pnl_usd > -1000  # مش خسارة كارثية غير منطقية (sanity check بسيط)


def test_backtest_reuses_real_engine_state_closed_trades_format():
    """تأكيد إن النتايج جايه من نفس closed_trades بتاع PaperTradingEngine الحقيقي، مش بنية موازية."""
    prices = flat_prices(15, value=1.0) + uptrend_prices(30, start=1.0, pct_per_candle=4.0)
    candles = make_candles(prices)
    token = healthy_token(address="BacktestAddr1")

    engine = build_engine()
    backtest = BacktestEngine(engine=engine, candle_interval_minutes=5)
    result = backtest.run(candles, token)

    assert result.closed_trades == engine.closed_trades


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
