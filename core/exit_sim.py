"""
محاكاة قواعد الخروج على شموع حقيقية (long-only، مركز واحد).
بتطبّق نفس منطق exit_rules_conservative: stop مبدئي، أهداف جزئية + breakeven،
وtrailing بعد التفعيل. داخل الشمعة الواحدة بنفترض الأسوأ: الـstop قبل الأهداف.
"""
from __future__ import annotations


def simulate_exit(candles, entry_idx, cfg):
    """candles: قائمة OHLCCandle مرتبة. بيرجع (return_pct_gross, reason, bars_held)."""
    entry = candles[entry_idx].open
    if entry <= 0:
        return None
    stop = entry * (1 - cfg["stop_pct"] / 100.0)
    levels = [dict(l, hit=False) for l in cfg["levels"]]
    remaining, realized = 1.0, 0.0
    trail_on, peak = False, entry
    last = min(len(candles), entry_idx + cfg["max_bars"])
    reason = "time"
    for i in range(entry_idx, last):
        c = candles[i]
        if c.low <= stop:
            fill = min(stop, c.open)  # gap تحت الـstop = تنفيذ على الـopen
            realized += remaining * (fill / entry - 1)
            return realized * 100, ("stop" if stop < entry else "breakeven_or_trail"), i - entry_idx
        for lv in levels:
            if not lv["hit"] and c.high >= entry * (1 + lv["gain_pct"] / 100.0):
                lv["hit"] = True
                part = min(remaining, lv["close_pct"] / 100.0)
                realized += part * (lv["gain_pct"] / 100.0)
                remaining -= part
                if lv.get("breakeven"):
                    stop = max(stop, entry)
        peak = max(peak, c.high)
        if not trail_on and peak >= entry * (1 + cfg["trail_activation_pct"] / 100.0):
            trail_on = True
        if trail_on:
            stop = max(stop, peak * (1 - cfg["trail_distance_pct"] / 100.0))
        if remaining <= 1e-9:
            return realized * 100, "all_targets", i - entry_idx
    realized += remaining * (candles[last - 1].close / entry - 1)
    return realized * 100, reason, last - 1 - entry_idx
