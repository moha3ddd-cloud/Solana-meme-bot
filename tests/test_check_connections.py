"""
اختبارات سكريبت فحص الاتصال (scripts/check_connections.py) — بعملاء
وهميين. الهدف: لما الفحص يفشل على GitHub، نكون متأكدين إن السبب في
الشبكة أو شكل الرد الحقيقي، مش في السكريبت نفسه.

تشغيل: python3 tests/test_check_connections.py
"""

import importlib.util
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tests.test_data_layer import sample_pair
from tests.test_onchain_layer import clean_report

_SCRIPT = os.path.join(os.path.dirname(__file__), "..", "scripts", "check_connections.py")
_spec = importlib.util.spec_from_file_location("check_connections", _SCRIPT)
cc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cc)

CONFIG_DIR = os.path.join(os.path.dirname(__file__), "..", "config")


class _FakeDex:
    def __init__(self, fail=False, empty_pairs=False):
        self.fail, self.empty_pairs = fail, empty_pairs

    def get_latest_token_profiles(self):
        if self.fail:
            raise ConnectionError("simulated network down")
        return [{"chainId": "solana", "tokenAddress": "A1"}]

    def get_latest_token_boosts(self):
        if self.fail:
            raise ConnectionError("simulated network down")
        return [{"chainId": "solana", "tokenAddress": "A2"}]

    def get_pairs_by_token_addresses(self, addresses):
        if self.fail:
            raise ConnectionError("simulated network down")
        return [] if self.empty_pairs else [sample_pair()]


class _FakeRugCheck:
    def __init__(self, report=None, fail=False):
        self.report = report if report is not None else clean_report()
        self.fail = fail

    def get_token_report(self, mint):
        if self.fail:
            raise ConnectionError("simulated rugcheck blocked")
        return self.report


def _by_name(result):
    return {i["name"]: i for i in result["checks"]}


def test_all_good_passes_every_core_check():
    r = cc.run_checks(_FakeDex(), _FakeRugCheck(), config_dir=CONFIG_DIR)
    assert r["all_core_passed"], [i for i in r["checks"] if not i["ok"]]
    assert _by_name(r)["full_pipeline_scanner"]["ok"]


def test_network_down_fails_and_skips_dependents():
    r = cc.run_checks(_FakeDex(fail=True), _FakeRugCheck(fail=True), config_dir=CONFIG_DIR)
    checks = _by_name(r)
    assert not r["all_core_passed"]
    assert not checks["dexscreener_token_pairs"]["ok"]
    assert checks["dexscreener_mapper"].get("skipped")  # اتخطّى بدل ما يرمي استثناء
    assert checks["full_pipeline_scanner"].get("skipped")


def test_rugcheck_blocked_but_dexscreener_ok_is_reported_separately():
    r = cc.run_checks(_FakeDex(), _FakeRugCheck(fail=True), config_dir=CONFIG_DIR)
    checks = _by_name(r)
    assert checks["dexscreener_token_pairs"]["ok"]
    assert not checks["rugcheck_report"]["ok"]
    assert "simulated rugcheck blocked" in checks["rugcheck_report"]["detail"]


def test_wrong_response_shape_names_the_missing_keys():
    bad_report = {"mint": "X", "someNewField": 1}  # شكل مختلف عن المتوقع
    r = cc.run_checks(_FakeDex(), _FakeRugCheck(report=bad_report), config_dir=CONFIG_DIR)
    check = _by_name(r)["rugcheck_report"]
    assert not check["ok"]
    assert "topHolders" in check["detail"] and "mintAuthority" in check["detail"]
    assert "someNewField" in check["detail"]  # بيعرض المفاتيح الفعلية عشان نصلّح الـmapper
    assert r["rugcheck_shape"]["missing_expected_keys"]


def test_empty_pairs_response_is_failure_not_silent_success():
    r = cc.run_checks(_FakeDex(empty_pairs=True), _FakeRugCheck(), config_dir=CONFIG_DIR)
    assert not _by_name(r)["dexscreener_token_pairs"]["ok"]


def test_optional_gecko_failure_does_not_fail_overall():
    class _BadGecko:
        def get_trending_pools(self):
            raise ConnectionError("blocked")

    r = cc.run_checks(_FakeDex(), _FakeRugCheck(), gecko_client=_BadGecko(), config_dir=CONFIG_DIR)
    assert r["all_core_passed"]
    assert not _by_name(r)["geckoterminal_trending"]["ok"]
    assert _by_name(r)["geckoterminal_ohlcv"].get("skipped")


def test_markdown_report_renders_for_pass_and_fail():
    ok = cc.render_markdown(cc.run_checks(_FakeDex(), _FakeRugCheck(), config_dir=CONFIG_DIR))
    bad = cc.render_markdown(cc.run_checks(_FakeDex(fail=True), _FakeRugCheck(fail=True), config_dir=CONFIG_DIR))
    assert "✅" in ok and "كله تمام" in ok
    assert "❌" in bad


if __name__ == "__main__":
    import traceback

    tests = [obj for name, obj in list(globals().items()) if name.startswith("test_")]
    passed, failed = 0, 0
    for t in tests:
        try:
            t()
            print(f"PASS: {t.__name__}")
            passed += 1
        except Exception:
            print(f"FAIL: {t.__name__}")
            traceback.print_exc()
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
