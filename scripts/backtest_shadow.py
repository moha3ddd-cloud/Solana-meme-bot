#!/usr/bin/env python3
"""
Backtest على شموع حقيقية (GeckoTerminal 1-دقيقة) لكل عملة اتسجلت في shadow_log:
بنطبّق قواعد الخروج الفعلية ونقارن قواعد الدخول. بيشتغل على GitHub Actions
(الشبكة هناك مفتوحة). الناتج: data/backtest_report.json + جدول في الـlog.
"""
import json, os, statistics, sys, time, random
import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from core.data.geckoterminal_client import GeckoTerminalClient, GeckoTerminalError, GeckoTerminalRateLimitError
from core.data.geckoterminal_mappers import ohlcv_response_to_candles
from core.exit_sim import simulate_exit
from core.shadow_log import rule_matches

MAX_HOLD_MIN = 12 * 60
LATENCY_SEC = 60
COSTS_PCT = (0.0, 3.0, 6.0)   # تكلفة round-trip (slippage+رسوم) كنسبة من المركز


def load_cfg():
    ex = yaml.safe_load(open("config/exit_rules_conservative.yaml"))
    orch = yaml.safe_load(open("config/orchestrator_config.yaml"))
    cfg = {
        "stop_pct": orch["default_initial_stop_loss_pct"],
        "levels": [{"gain_pct": l["gain_pct"], "close_pct": l["close_position_pct"],
                    "breakeven": l.get("move_stop_to_breakeven", False)} for l in ex["take_profit_levels"]],
        "trail_activation_pct": ex["trailing_stop_activation_pct"],
        "trail_distance_pct": ex["trailing_stop_distance_pct"],
        "max_bars": MAX_HOLD_MIN,
    }
    return cfg, orch["shadow_rules"]


def call(fn, *a, **k):
    for attempt in range(4):
        try:
            return fn(*a, **k)
        except GeckoTerminalRateLimitError:
            time.sleep(65)
        except GeckoTerminalError as e:
            if "unexpected_status_404" in str(e):
                return None
            time.sleep(5)
    return None


def fetch_candles(client, address, ts):
    pools = call(client.get_token_pools, address)
    try:
        pool = pools["data"][0]["attributes"]["address"]
    except (TypeError, KeyError, IndexError):
        return None, "no_pool"
    resp = call(client.get_pool_ohlcv, pool, "minute", 1, 1000, int(ts + MAX_HOLD_MIN * 60))
    candles = ohlcv_response_to_candles(resp, pool) if resp else []
    return (candles, "ok") if candles else (None, "no_candles")


def stats(rets):
    if not rets:
        return {"n": 0}
    wins = [r for r in rets if r > 0]
    losses = [r for r in rets if r <= 0]
    pf = (sum(wins) / -sum(losses)) if losses and sum(losses) < 0 else None
    top3 = sorted(rets, reverse=True)[:3]
    rng = random.Random(1)
    boots = sorted(statistics.fmean(rng.choices(rets, k=len(rets))) for _ in range(2000))
    return {"n": len(rets), "win_rate_pct": round(100 * len(wins) / len(rets), 1),
            "mean_pct": round(statistics.fmean(rets), 2), "median_pct": round(statistics.median(rets), 2),
            "profit_factor": round(pf, 2) if pf else None,
            "worst_pct": round(min(rets), 1), "best_pct": round(max(rets), 1),
            "top3_share_of_gross_wins_pct": round(100 * sum(top3) / sum(wins), 1) if wins else None,
            "mean_ci95": [round(boots[50], 2), round(boots[1949], 2)]}


def main():
    cfg, rules = load_cfg()
    log = json.load(open("data/shadow_log.json"))
    records = sorted(log["completed"] + log["pending"], key=lambda r: r["ts"])
    first = {}
    for r in records:
        first.setdefault(r["address"], r)   # أول تقييم لكل عملة بس (مستقل)
    wanted = [r for r in first.values() if any(rule_matches(rl, r) for rl in rules.values())]
    print(f"tokens to test: {len(wanted)} (of {len(first)})", flush=True)

    client = GeckoTerminalClient(timeout_seconds=20)
    trades, skipped = [], {}
    for n, r in enumerate(wanted, 1):
        candles, why = fetch_candles(client, r["address"], r["ts"])
        if not candles:
            skipped[why] = skipped.get(why, 0) + 1
            continue
        start = r["ts"] + LATENCY_SEC
        idx = next((i for i, c in enumerate(candles) if c.interval_start_ts >= start), None)
        if idx is None or candles[idx].interval_start_ts - r["ts"] > 600:
            skipped["no_candle_at_entry"] = skipped.get("no_candle_at_entry", 0) + 1
            continue
        ep = candles[idx].open
        if not ep or abs(ep / r["entry_price"] - 1) > 0.5:
            skipped["price_mismatch"] = skipped.get("price_mismatch", 0) + 1
            continue
        out = simulate_exit(candles, idx, cfg)
        if out is None:
            continue
        gross, reason, bars = out
        trades.append({"address": r["address"], "symbol": r["symbol"], "ts": r["ts"], "gross_pct": gross,
                       "reason": reason, "bars": bars,
                       "rules": [k for k, rl in rules.items() if rule_matches(rl, r)]})
        if n % 25 == 0:
            print(f"  {n}/{len(wanted)} done, trades={len(trades)} skipped={skipped}", flush=True)

    report = {"generated_at": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()),
              "max_hold_min": MAX_HOLD_MIN, "latency_sec": LATENCY_SEC, "skipped": skipped,
              "groups": {}, "trades": trades}
    for name in rules:
        sel = [t for t in trades if name in t["rules"]]
        report["groups"][name] = {f"cost_{c}pct": stats([t["gross_pct"] - c for t in sel]) for c in COSTS_PCT}
        report["groups"][name]["exit_reasons"] = {k: sum(1 for t in sel if t["reason"] == k) for k in {t["reason"] for t in sel}}
    json.dump(report, open("data/backtest_report.json", "w"), indent=1)
    print(json.dumps({k: v for k, v in report.items() if k != "trades"}, indent=1))


if __name__ == "__main__":
    main()
