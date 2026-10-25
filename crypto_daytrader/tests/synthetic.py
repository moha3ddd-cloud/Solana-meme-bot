"""بيانات تجريبية (random walk بترند متغير) لاختبار الكود من غير إنترنت. النتايج عليها بلا معنى تداولي."""
import random
from typing import Dict

from bot import data as D

M15 = 900_000


def make_15m(n: int, start_ts: int, seed: int, p0: float = 100.0, drift_cycle: int = 700):
    rnd = random.Random(seed)
    rows, p = [], p0
    for k in range(n):
        drift = 0.0006 if (k // drift_cycle) % 2 == 0 else -0.0004
        o = p
        c = o * (1 + drift + rnd.gauss(0, 0.004))
        h = max(o, c) * (1 + abs(rnd.gauss(0, 0.002)))
        l = min(o, c) * (1 - abs(rnd.gauss(0, 0.002)))
        v = 1000 * (1 + abs(rnd.gauss(0, 1)))
        t = start_ts + k * M15
        rows.append([t, o, h, l, c, v, t + M15 - 1])
        p = c
    return rows


def resample(rows, factor: int):
    out = []
    for i in range(0, len(rows) - factor + 1, factor):
        ch = rows[i:i + factor]
        out.append([ch[0][0], ch[0][1], max(r[2] for r in ch), min(r[3] for r in ch), ch[-1][4],
                    sum(r[5] for r in ch), ch[-1][6]])
    return out


class FakeClient:
    """بيقدّم داتا لحد 'now' فقط (آخر شمعة جارية) زي بينانس بالظبط."""

    def __init__(self, syms, n=6000, start_ts=1_760_000_000_000 // M15 * M15):
        self.start = start_ts
        self.rows = {s: make_15m(n, start_ts, seed=i + 1, p0=100 * (i + 1)) for i, s in enumerate(syms)}
        self.now = start_ts + n * M15 - 5 * 60_000

    def tickers_24h(self):
        return [{"symbol": s, "quoteVolume": "500000000"} for s in self.rows]

    def klines(self, symbol, interval, limit=500, start=None, end=None):
        f = {"15m": 1, "1h": 4, "4h": 16}[interval]
        base = [r for r in self.rows[symbol] if r[0] <= self.now]
        rows = resample(base, f) if f > 1 else base
        if start is not None:
            rows = [r for r in rows if r[0] >= start]
        if end is not None:
            rows = [r for r in rows if r[0] <= end]
        return D.rows_to_series(rows[-limit:])

    def klines_range(self, symbol, interval, start, end):
        f = {"15m": 1, "1h": 4, "4h": 16}[interval]
        rows = resample(self.rows[symbol], f) if f > 1 else self.rows[symbol]
        return D.rows_to_series([r for r in rows if start <= r[0] <= end])
