"""إدارة المخاطر — أهم جزء في البوت. كل القواعد هنا مستقلة عن الاستراتيجية."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Tuple

HOUR = 3_600_000


def day_key(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def risk_multiplier(equity: float, peak: float, cfg: dict) -> float:
    """كل ما التراجع من القمة يزيد، المخاطرة تقل (مش بتزيد أبداً)."""
    dd = (peak - equity) / peak * 100 if peak > 0 else 0.0
    mult = 1.0
    for thr, m in sorted(cfg["risk"]["drawdown_steps"]):
        if dd >= thr:
            mult = m
    return mult


def gate(state: dict, equity: float, now: int, cfg: dict, symbol: str | None = None) -> Tuple[bool, str]:
    """هل مسموح بدخول صفقة جديدة دلوقتي؟"""
    r = cfg["risk"]
    if state.get("killed"):
        return False, "Kill switch مفعّل — البوت متوقف (راجع الداشبورد)"
    peak = state["peak_equity"]
    dd = (peak - equity) / peak * 100 if peak > 0 else 0.0
    if dd >= r["kill_switch_drawdown_pct"]:
        state["killed"] = True
        state["killed_reason"] = f"تراجع {dd:.1f}% من القمة"
        return False, f"Kill switch: تراجع {dd:.1f}% من القمة"
    d = state["day"]
    start = d["start_equity"]
    pnl_pct = (equity - start) / start * 100 if start > 0 else 0.0
    if pnl_pct <= -r["daily_loss_limit_pct"]:
        return False, f"حد الخسارة اليومي ({pnl_pct:.2f}%)"
    lock = r.get("daily_profit_lock_pct", 0)
    if lock and pnl_pct >= lock:
        return False, f"تحقق هدف اليوم ({pnl_pct:+.2f}%) — حماية المكسب"
    if d["trades"] >= r["max_trades_per_day"]:
        return False, "وصلنا أقصى عدد صفقات اليوم"
    if len(state["positions"]) >= r["max_open_positions"]:
        return False, "أقصى عدد صفقات مفتوحة"
    if now < state.get("pause_until", 0):
        return False, "وقفة بعد خسايِر متتالية / هلع السوق"
    if symbol and now < state["cooldowns"].get(symbol, 0):
        return False, "عملة في فترة تبريد بعد ستوب"
    return True, "ok"


def position_size(equity: float, cash: float, entry: float, stop: float, cfg: dict,
                  mult: float, extra_mult: float = 1.0) -> float:
    """الكمية = (المخاطرة بالدولار) ÷ (مسافة الستوب). مش مبلغ ثابت."""
    r = cfg["risk"]
    risk_usd = equity * r["risk_per_trade_pct"] / 100 * mult * extra_mult
    dist = entry - stop
    if dist <= 0:
        return 0.0
    qty = risk_usd / dist
    cap_notional = min(equity * r["max_position_pct"] / 100, cash * 0.995)
    qty = min(qty, cap_notional / entry)
    return max(qty, 0.0)


def on_trade_closed(state: dict, pnl: float, symbol: str, stopped: bool, now: int, cfg: dict) -> None:
    r = cfg["risk"]
    if pnl < 0:
        state["consec_losses"] = state.get("consec_losses", 0) + 1
        if state["consec_losses"] >= r["consecutive_loss_pause"]:
            state["pause_until"] = now + int(r["pause_hours"] * HOUR)
            state["consec_losses"] = 0
    else:
        state["consec_losses"] = 0
    if stopped and pnl < 0:
        state["cooldowns"][symbol] = now + int(r["symbol_cooldown_hours"] * HOUR)
