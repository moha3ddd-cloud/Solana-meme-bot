#!/usr/bin/env python3
"""
فحص اتصال حي بكل مصادر البيانات — "هل الكود فعلًا بيكلّم الـAPIs
الحقيقية وبيفهم ردودها؟"

ليه الملف ده موجود: كل الـmappers والـclients اتكتبت واتختبرت على بيانات
عينة، لكن عمرها ما اتجرّبت على ردود حقيقية (الشبكة مقفولة في بيئة
التطوير). الفحص ده بيغطي 3 احتمالات فشل منفصلة، وبيقولك أنهي واحد:
  1. الشبكة: مفيش اتصال، أو المصدر حاجب الـIP (403/429)
  2. العنوان: الـendpoint نفسه غلط أو اتغيّر (404)
  3. الشكل: الرد جه، بس الحقول مش بالأسماء اللي الـmapper متوقعها

بيشتغل من زرار في GitHub (.github/workflows/check_connections.yml) أو
محليًا: python3 scripts/check_connections.py

بيكتب data/connection_report.json (+ ملخص مقروء في GitHub Actions).
exit code = 1 لو أي فحص أساسي فشل. GeckoTerminal اختياري ومابيفشّلش.
"""

import json
import os
import sys
import traceback

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Bonk: عملة قديمة ومستقرة نسبيًا — مناسبة لفحص الاتصال والشكل (مش لقرار تداول)
PROBE_ADDRESS = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"

# الحقول اللي build_onchain_fields بيعتمد عليها في رد RugCheck
RUGCHECK_EXPECTED_KEYS = ["mintAuthority", "freezeAuthority", "topHolders", "markets", "risks"]

REPORT_PATH = "data/connection_report.json"


class _Checks:
    def __init__(self):
        self.items = []

    def run(self, name, fn, optional=False, needs=None):
        """بينفّذ فحص واحد. needs = قايمة أسماء فحوصات لازم تكون نجحت قبله."""
        if needs:
            failed = [n for n in needs if not self._ok(n)]
            if failed:
                self.items.append({"name": name, "ok": False, "optional": optional,
                                   "skipped": True, "detail": f"اتخطّى — محتاج: {', '.join(failed)}"})
                return None
        try:
            result = fn()
            detail = result.get("detail", "") if isinstance(result, dict) else ""
            data = result.get("data") if isinstance(result, dict) else None
            self.items.append({"name": name, "ok": True, "optional": optional, "detail": detail})
            return data
        except Exception as e:  # noqa: BLE001 — الفحص لازم يلتقط أي نوع فشل ويسجله
            self.items.append({"name": name, "ok": False, "optional": optional,
                               "detail": f"{type(e).__name__}: {str(e)[:300]}"})
            return None

    def _ok(self, name):
        return any(i["name"] == name and i["ok"] for i in self.items)

    @property
    def core_failed(self):
        return [i for i in self.items if not i["ok"] and not i["optional"]]


def run_checks(dex_client, rugcheck_client, gecko_client=None, config_dir="config",
               probe_address=PROBE_ADDRESS) -> dict:
    from core.data.mappers import build_dex_fields, pair_to_timing_signals
    from core.data.onchain_mappers import build_onchain_fields, merge_into_token_metrics
    from core.scanner import ScannerEngine

    c = _Checks()
    state = {}

    # ---------- DexScreener ----------
    def dex_profiles():
        items = dex_client.get_latest_token_profiles()
        if not isinstance(items, list):
            raise ValueError(f"الرد مش list: {type(items).__name__}")
        sample_keys = sorted(items[0].keys()) if items else []
        return {"detail": f"{len(items)} عنصر Solana — مفاتيح العينة: {sample_keys}"}

    def dex_boosts():
        items = dex_client.get_latest_token_boosts()
        if not isinstance(items, list):
            raise ValueError(f"الرد مش list: {type(items).__name__}")
        return {"detail": f"{len(items)} عنصر Solana"}

    def dex_pairs():
        pairs = dex_client.get_pairs_by_token_addresses([probe_address])
        if not pairs:
            raise ValueError("الرد رجع فاضي — العنوان أو الـendpoint فيه مشكلة")
        state["pair"] = pairs[0]
        p = pairs[0]
        return {"detail": f"{len(pairs)} pair — priceUsd={p.get('priceUsd')} "
                          f"liquidity={(p.get('liquidity') or {}).get('usd')}"}

    def dex_mapper():
        pair = state["pair"]
        fields = build_dex_fields(pair)
        signals = pair_to_timing_signals(pair)
        state["dex_fields"] = fields
        return {"detail": f"liquidity_usd={fields.get('liquidity_usd')} "
                          f"age_min={fields.get('token_age_minutes')} "
                          f"price_change_5m={signals.price_change_5m_pct}"}

    c.run("dexscreener_profiles", dex_profiles)
    c.run("dexscreener_boosts", dex_boosts)
    c.run("dexscreener_token_pairs", dex_pairs)
    c.run("dexscreener_mapper", dex_mapper, needs=["dexscreener_token_pairs"])

    # ---------- RugCheck ----------
    def rc_report():
        report = rugcheck_client.get_token_report(probe_address)
        if not isinstance(report, dict):
            raise ValueError(f"الرد مش dict: {type(report).__name__}")
        state["report"] = report
        missing = [k for k in RUGCHECK_EXPECTED_KEYS if k not in report]
        top = report.get("topHolders") or []
        markets = report.get("markets") or []
        info = {
            "top_level_keys": sorted(report.keys()),
            "missing_expected_keys": missing,
            "topHolders_sample_keys": sorted(top[0].keys()) if top and isinstance(top[0], dict) else None,
            "markets_sample_keys": sorted(markets[0].keys()) if markets and isinstance(markets[0], dict) else None,
        }
        state["rugcheck_shape"] = info
        if missing:
            raise ValueError(f"الرد جه بس ناقص حقول متوقعة: {missing} — الـmapper محتاج تعديل. "
                             f"المفاتيح الموجودة: {info['top_level_keys']}")
        return {"detail": f"كل الحقول المتوقعة موجودة — topHolders={len(top)} markets={len(markets)}"}

    def rc_mapper():
        fields = build_onchain_fields(state["report"])
        state["onchain_fields"] = fields
        return {"detail": f"top_holder_pct={fields.get('top_holder_pct')} "
                          f"top10={fields.get('top10_holders_pct')} "
                          f"mint_active={fields.get('mint_authority_active')} "
                          f"lp_locked={fields.get('liquidity_locked_or_burned')}"}

    c.run("rugcheck_report", rc_report)
    c.run("rugcheck_mapper", rc_mapper, needs=["rugcheck_report"])

    # ---------- الخط الكامل: بيانات حقيقية -> Scanner ----------
    def full_pipeline():
        token = merge_into_token_metrics(state["dex_fields"], state["onchain_fields"])
        out = []
        for label, cfg in (("محافظ", "scanner_config.yaml"), ("سكالب", "scanner_scalp_config.yaml")):
            r = ScannerEngine(config_path=os.path.join(config_dir, cfg)).scan(token)
            out.append(f"{label}: score={r.score} passed_hard={r.passed_hard_filters} eligible={r.eligible_for_entry}")
        return {"detail": " | ".join(out) + "  (مجرد فحص إن الخط شغال — مش قرار دخول)"}

    c.run("full_pipeline_scanner", full_pipeline,
          needs=["dexscreener_mapper", "rugcheck_mapper"])

    # ---------- GeckoTerminal (اختياري) ----------
    if gecko_client is not None:
        from core.data.geckoterminal_mappers import ohlcv_response_to_candles, pools_response_to_candidates

        def gt_pools():
            cands = pools_response_to_candidates(gecko_client.get_trending_pools())
            if not cands:
                raise ValueError("trending_pools رجعت فاضية بعد التحويل")
            state["gt_pool"] = next((x["pool_address"] for x in cands if x.get("pool_address")), None)
            return {"detail": f"{len(cands)} pool — أول رمز: {cands[0].get('symbol')}"}

        def gt_ohlcv():
            pool = state.get("gt_pool")
            if not pool:
                raise ValueError("مفيش pool_address نجرّب عليه")
            candles = ohlcv_response_to_candles(gecko_client.get_pool_ohlcv(pool, "minute", 5, 50), pool)
            if not candles:
                raise ValueError("OHLCV رجعت فاضية بعد التحويل")
            return {"detail": f"{len(candles)} شمعة — آخر إغلاق {candles[-1].close}"}

        c.run("geckoterminal_trending", gt_pools, optional=True)
        c.run("geckoterminal_ohlcv", gt_ohlcv, optional=True, needs=["geckoterminal_trending"])

    return {
        "all_core_passed": not c.core_failed,
        "checks": c.items,
        "rugcheck_shape": state.get("rugcheck_shape"),
    }


def render_markdown(report: dict) -> str:
    lines = ["## نتيجة فحص الاتصال", ""]
    lines.append("**النتيجة: كله تمام ✅**" if report["all_core_passed"]
                 else "**النتيجة: في فحص أساسي فشل ❌**")
    lines += ["", "| الفحص | النتيجة | التفاصيل |", "|---|---|---|"]
    for i in report["checks"]:
        if i["ok"]:
            mark = "✅"
        elif i.get("skipped"):
            mark = "⏭️"
        else:
            mark = "⚠️" if i["optional"] else "❌"
        detail = str(i["detail"]).replace("|", "\\|").replace("\n", " ")
        lines.append(f"| {i['name']}{' (اختياري)' if i['optional'] else ''} | {mark} | {detail} |")
    if report.get("rugcheck_shape"):
        lines += ["", "### شكل رد RugCheck الفعلي", "```json",
                  json.dumps(report["rugcheck_shape"], ensure_ascii=False, indent=2)[:3000], "```"]
    return "\n".join(lines)


def main():
    from core.data.dexscreener_client import DexScreenerClient
    from core.data.geckoterminal_client import GeckoTerminalClient
    from core.data.rugcheck_client import RugCheckClient

    report = run_checks(DexScreenerClient(), RugCheckClient(), GeckoTerminalClient())

    os.makedirs(os.path.dirname(REPORT_PATH), exist_ok=True)
    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    md = render_markdown(report)
    print(md)
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as f:
            f.write(md + "\n")

    sys.exit(0 if report["all_core_passed"] else 1)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        traceback.print_exc()
        sys.exit(2)
