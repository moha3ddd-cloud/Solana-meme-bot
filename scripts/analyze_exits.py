#!/usr/bin/env python3
"""مقارنة نسخ مختلفة من قواعد الخروج على الشموع المحفوظة (بدون شبكة)."""
import gzip, json, os, statistics, sys, random
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import yaml
from core.data.data_logger import OHLCCandle
from core.exit_sim import simulate_exit
from core.shadow_log import rule_matches

COST = 3.0
cache = json.load(gzip.open("data/backtest_candles.json.gz", "rt"))
rules = yaml.safe_load(open("config/orchestrator_config.yaml"))["shadow_rules"]
log = json.load(open("data/shadow_log.json"))
first = {}
for r in sorted(log["completed"] + log["pending"], key=lambda r: r["ts"]):
    first.setdefault(r["address"], r)

def L(*gc): return [{"gain_pct": g, "close_pct": c, "breakeven": True} for g, c in gc]
VARIANTS = {
    "current (stop10, 30/60/100, trail100/15)": dict(stop_pct=10, levels=L((30,30),(60,30),(100,20)), trail_activation_pct=100, trail_distance_pct=15),
    "stop 15": dict(stop_pct=15, levels=L((30,30),(60,30),(100,20)), trail_activation_pct=100, trail_distance_pct=15),
    "stop 7": dict(stop_pct=7, levels=L((30,30),(60,30),(100,20)), trail_activation_pct=100, trail_distance_pct=15),
    "early targets 15/30/60": dict(stop_pct=10, levels=L((15,30),(30,30),(60,20)), trail_activation_pct=60, trail_distance_pct=15),
    "no BE, targets 30/60/100": dict(stop_pct=10, levels=[dict(l, breakeven=False) for l in L((30,30),(60,30),(100,20))], trail_activation_pct=100, trail_distance_pct=15),
    "trail from +30, 20%": dict(stop_pct=10, levels=L((30,30),(60,30),(100,20)), trail_activation_pct=30, trail_distance_pct=20),
    "runner: 50% at +50, trail 25": dict(stop_pct=10, levels=L((50,50)), trail_activation_pct=50, trail_distance_pct=25),
}

def run(group_rule, cfg):
    out = []
    for addr, v in cache.items():
        if v.get("status") != "ok" or addr not in first: continue
        r = first[addr]
        if group_rule and not rule_matches(rules[group_rule], r): continue
        cs = [OHLCCandle(addr, *row[:1], *row[1:5], 0, 1) for row in v["c"]]
        idx = next((i for i, c in enumerate(cs) if c.interval_start_ts >= r["ts"] + 60), None)
        if idx is None or cs[idx].interval_start_ts - r["ts"] > 600: continue
        if abs(cs[idx].open / r["entry_price"] - 1) > 0.5: continue
        res = simulate_exit(cs, idx, dict(cfg, max_bars=720))
        if res: out.append(res[0] - COST)
    return out

def fmt(x):
    if not x: return "n=0"
    rng = random.Random(1); b = sorted(statistics.fmean(rng.choices(x, k=len(x))) for _ in range(2000))
    w = sum(i for i in x if i > 0); l = -sum(i for i in x if i <= 0)
    return f"n={len(x):3d} mean={statistics.fmean(x):6.2f}% win={100*sum(i>0 for i in x)/len(x):3.0f}% PF={(w/l if l else 0):4.2f} CI95=[{b[50]:6.1f},{b[1949]:6.1f}]"

for g in ("calm_entry", "passed_filters_any"):
    print(f"\n=== {g} (cost {COST}%) ===")
    for name, cfg in VARIANTS.items():
        print(f"  {name:42s} {fmt(run(g, cfg))}")
