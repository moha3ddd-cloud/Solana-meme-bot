#!/usr/bin/env python3
"""
بيجيب شموع الدقيقة (12 ساعة بعد الدخول) لكل عملة في shadow_log وبيحفظها في
data/backtest_candles.json.gz. بيكمّل من اللي اتحفظ قبل كده (resumable)،
فنقدر نشغّله كذا مرة لحد ما نغطي كل العملات. التحليل بعدين محلي بدون شبكة.
"""
import gzip, json, os, sys, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import yaml
from core.data import geckoterminal_client as gt
from core.data.geckoterminal_mappers import ohlcv_response_to_candles
from core.shadow_log import rule_matches
from scripts.backtest_shadow import call, MAX_HOLD_MIN

CACHE = "data/backtest_candles.json.gz"
BUDGET = 36 * 60
gt.RATE_LIMIT_PER_MIN = 8


def main():
    rules = yaml.safe_load(open("config/orchestrator_config.yaml"))["shadow_rules"]
    log = json.load(open("data/shadow_log.json"))
    first = {}
    for r in sorted(log["completed"] + log["pending"], key=lambda r: r["ts"]):
        first.setdefault(r["address"], r)
    prio = {"calm_entry", "chasing_pump", "calm_in_uptrend"}
    wanted = [r for r in first.values() if any(rule_matches(rl, r) for rl in rules.values())]
    wanted.sort(key=lambda r: (not any(k in prio and rule_matches(rules[k], r) for k in rules), r["ts"]))
    cache = {}
    if os.path.exists(CACHE):
        cache = json.load(gzip.open(CACHE, "rt"))
    todo = [r for r in wanted if r["address"] not in cache]
    print(f"cached={len(cache)} todo={len(todo)}", flush=True)
    client = gt.GeckoTerminalClient(timeout_seconds=20)
    client._limiter.max_per_minute = 8
    t0 = time.time()
    for n, r in enumerate(todo, 1):
        if time.time() - t0 > BUDGET:
            break
        pools = call(client.get_token_pools, r["address"])
        try:
            pool = pools["data"][0]["attributes"]["address"]
        except (TypeError, KeyError, IndexError):
            cache[r["address"]] = {"status": "no_pool"}
            continue
        resp = call(client.get_pool_ohlcv, pool, "minute", 1, 1000, int(r["ts"] + MAX_HOLD_MIN * 60))
        cs = ohlcv_response_to_candles(resp, pool) if resp else []
        cs = [c for c in cs if c.interval_start_ts >= r["ts"] - 120]
        cache[r["address"]] = {"status": "ok" if cs else "no_candles", "ts": r["ts"], "symbol": r["symbol"],
                               "entry_price": r["entry_price"],
                               "c": [[c.interval_start_ts, c.open, c.high, c.low, c.close] for c in cs]}
        if n % 20 == 0:
            print(f"  {n}/{len(todo)} elapsed={int(time.time()-t0)}s", flush=True)
            with gzip.open(CACHE, "wt") as f:
                json.dump(cache, f)
    with gzip.open(CACHE, "wt") as f:
        json.dump(cache, f)
    print(f"saved cache: {len(cache)} tokens, remaining {len([r for r in wanted if r['address'] not in cache])}")


if __name__ == "__main__":
    main()
