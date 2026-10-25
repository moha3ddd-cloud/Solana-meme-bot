"""Paper broker: يحاكي تنفيذ بينانس سبوت بالرسوم والانزلاق.

قواعد المحاكاة المحافظة:
- لو الستوب والهدف اتلمسوا في نفس الشمعة → نفترض إن الستوب اتضرب الأول.
- لو الشمعة فتحت تحت الستوب (gap) → التنفيذ بسعر الفتح مش بسعر الستوب.
- كل أمر Market بيتخصم منه انزلاق + رسوم.
"""
from __future__ import annotations

from typing import List, Optional

HOUR = 3_600_000


def new_position(sym: str, entry_raw: float, qty: float, plan: dict, now: int, last_ot: int,
                 score: int, reasons: list, cfg: dict) -> dict:
    cs = cfg["costs"]
    fill = entry_raw * (1 + cs["slippage"])
    fee = fill * qty * cs["fee_rate"]
    stop_dist = plan["stop_dist"]
    return {
        "symbol": sym, "entry": fill, "qty": qty, "qty_open": qty,
        "stop": fill - stop_dist, "initial_stop": fill - stop_dist, "risk_dist": stop_dist,
        "tp1": fill + cfg["exit"]["tp1_r"] * stop_dist, "tp1_done": False,
        "atr1": plan["atr1"], "high": fill, "open_ts": now, "last_ot": last_ot,
        "score": score, "reasons": reasons, "fees": fee, "realized": -fee, "cost_basis": fill * qty,
        "mark": fill, "proceeds": 0.0,
    }


def _sell(pos: dict, qty: float, price: float, cfg: dict, market: bool) -> float:
    """يبيع qty بسعر price. يرجع الصافي المحصل (بعد الرسوم والانزلاق)."""
    cs = cfg["costs"]
    fill = price * (1 - cs["slippage"]) if market else price
    gross = fill * qty
    fee = gross * cs["fee_rate"]
    pos["fees"] += fee
    pos["qty_open"] -= qty
    pos["proceeds"] += gross - fee
    pos["realized"] += (fill - pos["entry"]) * qty - fee
    return gross - fee


def process_candle(pos: dict, o: float, h: float, l: float, c: float, ot: int, cfg: dict,
                   atr_now: Optional[float] = None) -> List[dict]:
    """يطبق شمعة 15m على صفقة مفتوحة. يرجع events: [{'type','price','qty','cash'}]."""
    ex = cfg["exit"]
    events: List[dict] = []
    if pos["qty_open"] <= 0:
        return events
    # 1) الستوب الأول (محافظة)
    if l <= pos["stop"]:
        px = min(pos["stop"], o)
        qty = pos["qty_open"]
        cash = _sell(pos, qty, px, cfg, market=True)
        events.append({"type": "stop" if not pos["tp1_done"] and pos["stop"] < pos["entry"] else "trail_stop",
                       "price": px, "qty": qty, "cash": cash})
        pos["mark"] = px
        return events
    # 2) الهدف الأول
    if not pos["tp1_done"] and h >= pos["tp1"]:
        qty = min(pos["qty_open"], pos["qty"] * ex["tp1_fraction"])
        cash = _sell(pos, qty, pos["tp1"], cfg, market=False)
        pos["tp1_done"] = True
        be = pos["entry"] * (1 + 2 * cfg["costs"]["fee_rate"])  # تعادل بعد الرسوم
        pos["stop"] = max(pos["stop"], be)
        events.append({"type": "tp1", "price": pos["tp1"], "qty": qty, "cash": cash})
    # 3) تحديث أعلى سعر + trailing (بعد الهدف الأول)
    pos["high"] = max(pos["high"], h)
    if pos["tp1_done"]:
        a = atr_now or pos["atr1"]
        pos["stop"] = max(pos["stop"], pos["high"] - ex["trail_atr_mult"] * a)
    pos["mark"] = c
    return events


def time_exit(pos: dict, now: int, price: float, cfg: dict) -> Optional[dict]:
    """خروج بالوقت: 24 ساعة كحد أقصى، أو 8 ساعات من غير تقدم."""
    ex = cfg["exit"]
    age_h = (now - pos["open_ts"]) / HOUR
    r_now = (price - pos["entry"]) / pos["risk_dist"]
    reason = None
    if age_h >= ex["max_hold_hours"]:
        reason = "time_stop_24h"
    elif age_h >= ex["stale_hours"] and r_now < ex["stale_min_r"] and not pos["tp1_done"]:
        reason = "stale_no_progress"
    if not reason:
        return None
    qty = pos["qty_open"]
    cash = _sell(pos, qty, price, cfg, market=True)
    pos["mark"] = price
    return {"type": reason, "price": price, "qty": qty, "cash": cash}


def close_record(pos: dict, exit_type: str, close_ts: int) -> dict:
    """سجل الصفقة النهائي (بعد ما كل الكمية اتباعت)."""
    pnl = pos["realized"]
    return {
        "symbol": pos["symbol"], "open_ts": pos["open_ts"], "close_ts": close_ts,
        "entry": round(pos["entry"], 8), "qty": pos["qty"],
        "exit_avg": round(pos["proceeds"] / pos["qty"], 8) if pos["qty"] else 0,
        "pnl": round(pnl, 4), "r": round(pnl / (pos["risk_dist"] * pos["qty"]), 3),
        "fees": round(pos["fees"], 4), "exit_type": exit_type, "tp1": pos["tp1_done"],
        "score": pos["score"], "reasons": pos["reasons"],
    }
