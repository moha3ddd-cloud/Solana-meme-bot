"""
Cycle Log — سجل مختصر لكل دورة + إجماليات تراكمية
=====================================================
الهدف: نعرف "ليه مفيش صفقات؟" من غير ما نفتح اللوجات: كام عنوان اتجمّع،
كام اتفحص، وكام اترفض ولأي سبب. بيتخزّن جوه dashboard/snapshot.json نفسه
(cycle_log = آخر N دورة، cycle_totals = إجماليات من أول تشغيل)، فمش
محتاجين ملف جديد ولا تعديل في الـworkflow.
"""

from __future__ import annotations

MAX_LOG_ENTRIES = 200  # ~4 أيام لو الدورة كل 30 دقيقة


def _add_counts(target: dict, source: dict) -> None:
    for k, v in (source or {}).items():
        target[k] = target.get(k, 0) + v


def update_cycle_history(previous_snapshot: dict | None, entry: dict, timestamp_iso: str,
                         max_entries: int = MAX_LOG_ENTRIES) -> dict:
    """
    بياخد الـsnapshot القديم (أو None) + ملخص الدورة الجديدة، ويرجّع
    dict فيه cycle_log و cycle_totals الجداد، جاهز يتمرر كـ extra.
    """
    prev = previous_snapshot or {}
    log = list(prev.get("cycle_log") or [])
    totals = dict(prev.get("cycle_totals") or {})

    log.append({"timestamp": timestamp_iso, **entry})
    log = log[-max_entries:]

    discovery = entry.get("discovery") or {}
    totals["cycles"] = totals.get("cycles", 0) + 1
    totals["addresses_collected"] = totals.get("addresses_collected", 0) + discovery.get("addresses_collected", 0)
    totals["skipped_recently_seen"] = totals.get("skipped_recently_seen", 0) + discovery.get("skipped_recently_seen", 0)
    totals["pairs_returned"] = totals.get("pairs_returned", 0) + discovery.get("pairs_returned", 0)
    totals["passed_pre_filter"] = totals.get("passed_pre_filter", 0) + discovery.get("passed_pre_filter", 0)
    totals["evaluated"] = totals.get("evaluated", 0) + entry.get("candidates_evaluated", 0)
    totals["opened"] = totals.get("opened", 0) + len(entry.get("opened") or [])
    totals["errors"] = totals.get("errors", 0) + len(entry.get("errors") or [])

    pre = dict(totals.get("pre_filter_rejected") or {})
    _add_counts(pre, discovery.get("pre_filter_rejected"))
    totals["pre_filter_rejected"] = pre

    rej = dict(totals.get("rejections") or {})
    _add_counts(rej, entry.get("rejections"))
    totals["rejections"] = rej

    return {"cycle_log": log, "cycle_totals": totals}
