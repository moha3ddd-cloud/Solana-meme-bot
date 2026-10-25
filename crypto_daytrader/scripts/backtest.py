#!/usr/bin/env python3
"""Backtest على بيانات بينانس الحقيقية + مقارنة حدود الـscore (عشان نكشف الـoverfitting)."""
import copy
import json
import os
import sys
import time

import yaml

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)

from bot import backtest as B  # noqa: E402
from bot import data  # noqa: E402


def main() -> int:
    with open(os.path.join(ROOT, "config.yaml"), encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    days = int(sys.argv[1]) if len(sys.argv) > 1 else cfg["backtest"]["days"]
    end = int(time.time() * 1000) // B.STEP * B.STEP
    start = end - days * 86_400_000
    client = data.BinanceData()
    feats = B.prepare(client, cfg["backtest"]["symbols"], start, end)
    if "BTCUSDT" not in feats:
        print("BTC data unavailable"); return 1
    out = {"period_days": days, "symbols": sorted(feats), "runs": {}}
    # نصفين زمنيين: لو النتيجة بتتغير جامد بين النص الأول والتاني → مش مستقرة
    mid = start + (end - start) // 2
    for thr in cfg["backtest"]["threshold_sweep"]:
        c = copy.deepcopy(cfg); c["entry"]["score_threshold"] = thr
        res = B.run(feats, c, start, end)
        s = B.summarize(res, c, feats["BTCUSDT"], start, end)
        h1 = B.summarize(B.run(feats, c, start, mid), c, feats["BTCUSDT"], start, mid)
        h2 = B.summarize(B.run(feats, c, mid, end), c, feats["BTCUSDT"], mid, end)
        s["first_half_return_pct"], s["second_half_return_pct"] = h1["return_pct"], h2["return_pct"]
        out["runs"][f"threshold_{thr}"] = s
        print(f"thr={thr}: ret={s['return_pct']}% trades={s['trades']} win={s['win_rate']}% PF={s['profit_factor']} "
              f"DD={s['max_drawdown_pct']}% halves={h1['return_pct']}/{h2['return_pct']} BTC={s['btc_buy_hold_pct']}%")
    os.makedirs(os.path.join(ROOT, "reports"), exist_ok=True)
    with open(os.path.join(ROOT, "reports", "backtest.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
