"""Pure-python indicators. كل دالة بترجع list بنفس طول المدخل (None = لسه مفيش بيانات كفاية)."""
from __future__ import annotations

from typing import List, Optional

Num = Optional[float]


def sma(v: List[float], n: int) -> List[Num]:
    out: List[Num] = [None] * len(v)
    s = 0.0
    for i, x in enumerate(v):
        s += x
        if i >= n:
            s -= v[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def ema(v: List[float], n: int) -> List[Num]:
    out: List[Num] = [None] * len(v)
    if len(v) < n:
        return out
    k = 2.0 / (n + 1)
    e = sum(v[:n]) / n
    out[n - 1] = e
    for i in range(n, len(v)):
        e = v[i] * k + e * (1 - k)
        out[i] = e
    return out


def rsi(c: List[float], n: int = 14) -> List[Num]:
    out: List[Num] = [None] * len(c)
    if len(c) <= n:
        return out
    gain = loss = 0.0
    for i in range(1, n + 1):
        d = c[i] - c[i - 1]
        gain += max(d, 0.0)
        loss += max(-d, 0.0)
    gain /= n
    loss /= n
    out[n] = 100.0 if loss == 0 else 100.0 - 100.0 / (1 + gain / loss)
    for i in range(n + 1, len(c)):
        d = c[i] - c[i - 1]
        gain = (gain * (n - 1) + max(d, 0.0)) / n
        loss = (loss * (n - 1) + max(-d, 0.0)) / n
        out[i] = 100.0 if loss == 0 else 100.0 - 100.0 / (1 + gain / loss)
    return out


def atr(h: List[float], l: List[float], c: List[float], n: int = 14) -> List[Num]:
    out: List[Num] = [None] * len(c)
    if len(c) <= n:
        return out
    tr = [h[0] - l[0]]
    for i in range(1, len(c)):
        tr.append(max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])))
    a = sum(tr[1:n + 1]) / n
    out[n] = a
    for i in range(n + 1, len(c)):
        a = (a * (n - 1) + tr[i]) / n
        out[i] = a
    return out


def macd_hist(c: List[float], fast: int = 12, slow: int = 26, sig: int = 9) -> List[Num]:
    ef, es = ema(c, fast), ema(c, slow)
    line = [(a - b) if a is not None and b is not None else None for a, b in zip(ef, es)]
    first = next((i for i, x in enumerate(line) if x is not None), None)
    out: List[Num] = [None] * len(c)
    if first is None:
        return out
    sig_part = ema([x for x in line[first:]], sig)
    for j, s in enumerate(sig_part):
        if s is not None:
            out[first + j] = line[first + j] - s
    return out


def rolling_max_prev(v: List[float], n: int) -> List[Num]:
    """أعلى قيمة في الـn شمعة اللي *قبل* الشمعة الحالية (من غير الحالية)."""
    out: List[Num] = [None] * len(v)
    for i in range(n, len(v)):
        out[i] = max(v[i - n:i])
    return out


def ok(*xs) -> bool:
    return all(x is not None for x in xs)
