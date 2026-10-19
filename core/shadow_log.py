"""
Shadow Log — "هل اختياراتنا عندها ميزة؟" من غير ما نخسر ولا دولار
==================================================================
لكل عملة البوت بيقيّمها (حتى لو مادخلهاش)، بنسجّل: السعر وقت التقييم،
وقرار كل مسار (المحافظ/السكالب)، وبعدين بنرجع نقيس سعرها بعد 30 و60 و120
دقيقة. بعد كام يوم نقدر نقارن: العملات اللي السكالب كان "هيدخلها" بتتحرك
إزاي بالنسبة للباقي؟ لو مفيش فرق، يبقى مفيش ميزة في الاختيار — وده قرار
بيتاخد بالأرقام مش بالإحساس.

⚠️ حدود لازم تتقال:
- القياس بيحصل في بداية كل دورة (كل ~30-40 دقيقة)، فمفيش قياس على 5 أو 15
  دقيقة. ده مش بديل لـBacktest على شموع حقيقية (ده المكان الصح للأفق القصير).
- العملة اللي بتنهار وتختفي من DexScreener مالهاش سعر: بنعدّها
  "unresolved" ومابنحسبهاش كأنها صفر. لو كتير، الأرقام متفائلة (survivorship).
- نفس العملة ممكن تتسجل أكتر من مرة (كل مرة اتقيّمت فيها)، فالسجلات مش
  مستقلة تمامًا — شوف unique_tokens في الملخص.
"""

from __future__ import annotations

import json
import os
import statistics
import time
from typing import Callable, Optional

DEFAULT_HORIZONS_MIN = (30, 60, 120)
EXPIRE_AFTER_EXTRA_MIN = 180   # نسيب السجل شوية بعد آخر أفق قبل ما نعتبره منتهي
MAX_COMPLETED = 3000           # حد أقصى للسجلات المنتهية في الملف (الأقدم يتشال)
DUMP_THRESHOLD_PCT = -30.0


def _f(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def rule_matches(rule: dict, record: dict) -> bool:
    """
    قاعدة دخول مرشّحة للقياس (من config.shadow_rules). كل الشروط اختيارية وكلها لازم تتحقق:
      passed_hard_filters: true   -> أي مسار عدّى الـhard filters (الفلاتر واحدة بين المسارات)
      m5_min / m5_max             -> تغيّر السعر 5 دقايق وقت التقييم (min شامل، max غير شامل)
      h1_min / h1_max             -> تغيّر السعر ساعة
      min_scanner_score           -> أعلى scanner score بين المسارات
    لو قيمة السياق ناقصة والقاعدة بتشترطها => السجل مايدخلش القاعدة.
    """
    profiles = record.get("profiles") or {}
    ctx = record.get("context") or {}
    if rule.get("passed_hard_filters"):
        if not any(p.get("stage") not in (None, "hard_filter_failed") for p in profiles.values()):
            return False
    for key, ctx_key, lo_key, hi_key in (("m5", "price_change_m5", "m5_min", "m5_max"),
                                          ("h1", "price_change_h1", "h1_min", "h1_max")):
        if lo_key in rule or hi_key in rule:
            v = _f(ctx.get(ctx_key))
            if v is None:
                return False
            if lo_key in rule and v < rule[lo_key]:
                return False
            if hi_key in rule and v >= rule[hi_key]:
                return False
    if "min_scanner_score" in rule:
        scores = [p.get("scanner_score") for p in profiles.values() if p.get("scanner_score") is not None]
        if not scores or max(scores) < rule["min_scanner_score"]:
            return False
    return True


class ShadowLog:
    def __init__(self, path: str = "data/shadow_log.json",
                 horizons_min=DEFAULT_HORIZONS_MIN,
                 clock: Callable[[], float] = time.time,
                 rules: Optional[dict] = None):
        self.rules = rules or {}   # {اسم: قاعدة} — بتتحسب وقت الملخص، فبتنطبق على السجلات القديمة كمان
        self.path = path
        self.horizons = tuple(sorted(horizons_min))
        self.clock = clock
        self.pending: list[dict] = []
        self.completed: list[dict] = []
        self._load()

    # ---------------- persistence ----------------
    def _load(self) -> None:
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.pending = list(data.get("pending") or [])
            self.completed = list(data.get("completed") or [])
        except (OSError, ValueError):
            # ملف بايظ مايوقفش البوت — نبدأ سجل جديد (البيانات القديمة بتضيع، بس التداول يكمّل)
            self.pending, self.completed = [], []

    def save(self) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"horizons_min": list(self.horizons),
                       "pending": self.pending,
                       "completed": self.completed[-MAX_COMPLETED:]}, f, ensure_ascii=False)

    # ---------------- recording ----------------
    def add(self, address: str, symbol: str, entry_price: float, profiles: dict,
            context: Optional[dict] = None) -> None:
        """profiles: {name: {"stage":..., "scanner_score":..., "signal_score":..., "shadow_only":...}}"""
        if not address or not entry_price or entry_price <= 0:
            return
        self.pending.append({
            "ts": self.clock(),
            "address": address,
            "symbol": symbol,
            "entry_price": entry_price,
            "profiles": profiles,
            "context": context or {},
            "followups": {},   # "30": {"price":..,"elapsed_min":..,"return_pct":..}
        })

    # ---------------- follow-up measurement ----------------
    def update_followups(self, price_fetcher: Callable[[list], dict]) -> dict:
        """
        price_fetcher(addresses) -> {address: price}. بيتنادى بعناوين السجلات اللي
        وصلت أفق لسه ماتقاسش. بيرجع إحصائية صغيرة (للّوج).
        """
        now = self.clock()
        due_addresses = set()
        for rec in self.pending:
            elapsed = (now - rec["ts"]) / 60.0
            if any(str(h) not in rec["followups"] and elapsed >= h for h in self.horizons):
                due_addresses.add(rec["address"])

        prices: dict = {}
        if due_addresses:
            prices = price_fetcher(sorted(due_addresses)) or {}

        measured = 0
        still_pending = []
        for rec in self.pending:
            elapsed = (now - rec["ts"]) / 60.0
            price = prices.get(rec["address"])
            if price:
                for h in self.horizons:
                    key = str(h)
                    if key not in rec["followups"] and elapsed >= h:
                        rec["followups"][key] = {
                            "price": price,
                            "elapsed_min": round(elapsed, 1),
                            "return_pct": round((price / rec["entry_price"] - 1) * 100, 2),
                        }
                        measured += 1

            all_filled = all(str(h) in rec["followups"] for h in self.horizons)
            expired = elapsed >= max(self.horizons) + EXPIRE_AFTER_EXTRA_MIN
            if all_filled or expired:
                rec["unresolved_horizons"] = [h for h in self.horizons if str(h) not in rec["followups"]]
                self.completed.append(rec)
            else:
                still_pending.append(rec)
        self.pending = still_pending
        return {"measured": measured, "pending": len(self.pending), "completed": len(self.completed)}

    # ---------------- summary ----------------
    def summarize(self) -> dict:
        """ملخص مقارن لكل مجموعة، لكل أفق. المجموعات: all + لكل مسار would_enter / not_entered."""
        records = self.completed + self.pending
        profile_names = sorted({n for r in records for n in (r.get("profiles") or {})})
        groups: dict[str, list] = {"all": records}
        for name in profile_names:
            groups[f"{name}_would_enter"] = [r for r in records if (r.get("profiles") or {}).get(name, {}).get("stage") == "approved"]
            groups[f"{name}_not_entered"] = [r for r in records if (r.get("profiles") or {}).get(name, {}).get("stage") not in (None, "approved")]
        for rule_name, rule in self.rules.items():
            groups[f"rule:{rule_name}"] = [r for r in records if rule_matches(rule, r)]

        out = {"horizons_min": list(self.horizons), "records_total": len(records),
               "pending": len(self.pending), "completed": len(self.completed), "groups": {}}
        for gname, recs in groups.items():
            g = {"records": len(recs), "unique_tokens": len({r["address"] for r in recs}), "by_horizon": {}}
            for h in self.horizons:
                key = str(h)
                rets = [r["followups"][key]["return_pct"] for r in recs if key in r.get("followups", {})]
                unresolved = sum(1 for r in recs if h in (r.get("unresolved_horizons") or []))
                if rets:
                    g["by_horizon"][key] = {
                        "n": len(rets),
                        "mean_return_pct": round(statistics.fmean(rets), 2),
                        "median_return_pct": round(statistics.median(rets), 2),
                        "pct_positive": round(100 * sum(1 for x in rets if x > 0) / len(rets), 1),
                        "pct_dumped_30": round(100 * sum(1 for x in rets if x <= DUMP_THRESHOLD_PCT) / len(rets), 1),
                        "unresolved": unresolved,
                    }
                else:
                    g["by_horizon"][key] = {"n": 0, "unresolved": unresolved}
            out["groups"][gname] = g
        return out
