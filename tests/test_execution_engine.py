"""
اختبارات Execution Engine — فحوصات ما قبل التنفيذ + sellability probe.
كله بـfake client (مفيش شبكة حقيقية).

تشغيل: python3 tests/test_execution_engine.py
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.execution.execution_engine import ExecutionEngine
from core.execution.jupiter_client import SOL_MINT, JupiterNoRouteError

RISK_CONFIG = os.path.join(os.path.dirname(__file__), "..", "config", "risk_config.yaml")


def good_quote(**overrides) -> dict:
    base = {
        "inputMint": SOL_MINT,
        "outputMint": "SoMeMeToken111",
        "outAmount": "5000000000",
        "priceImpactPct": "0.008",  # 0.8%
        "routePlan": [
            {"swapInfo": {"label": "Raydium"}},
        ],
    }
    base.update(overrides)
    return base


class _FakeJupiterClient:
    """
    بديل لـJupiterClient — بنتحكم في السلوك يدويًا من غير أي HTTP فعلي.
    quote_to_return: dict أو exception instance يترمى بدل ما يترجع.
    """
    def __init__(self, quote_to_return=None, swap_response=None):
        self.quote_to_return = quote_to_return
        self.swap_response = swap_response or {"swapTransaction": "base64_placeholder_tx=="}
        self.quote_calls = []

    def get_quote(self, input_mint, output_mint, amount, slippage_bps=100):
        self.quote_calls.append((input_mint, output_mint, amount))
        if isinstance(self.quote_to_return, Exception):
            raise self.quote_to_return
        return self.quote_to_return

    def get_swap_transaction(self, quote_response, user_public_key):
        return self.swap_response


def engine_with(quote_to_return=None, swap_response=None) -> ExecutionEngine:
    fake = _FakeJupiterClient(quote_to_return=quote_to_return, swap_response=swap_response)
    return ExecutionEngine(jupiter_client=fake, risk_config_path=RISK_CONFIG)


# ---------------- Pre-trade check tests ----------------

def test_normal_quote_approved():
    engine = engine_with()
    result = engine.evaluate_pre_trade(good_quote(), liquidity_usd=50000)
    assert result.approved
    assert result.price_impact_pct == 0.8


def test_high_price_impact_rejected():
    engine = engine_with()
    result = engine.evaluate_pre_trade(good_quote(priceImpactPct="0.05"), liquidity_usd=50000)  # 5%
    assert not result.approved
    assert any("price_impact_too_high" in r for r in result.rejection_reasons)


def test_low_liquidity_rejected():
    engine = engine_with()
    result = engine.evaluate_pre_trade(good_quote(), liquidity_usd=500)
    assert not result.approved
    assert any("liquidity_below_min" in r for r in result.rejection_reasons)


def test_zero_output_amount_rejected():
    engine = engine_with()
    result = engine.evaluate_pre_trade(good_quote(outAmount="0"), liquidity_usd=50000)
    assert not result.approved
    assert "invalid_or_zero_output_amount" in result.rejection_reasons


def test_route_summary_extracted():
    engine = engine_with()
    result = engine.evaluate_pre_trade(good_quote(), liquidity_usd=50000)
    assert result.route_summary == "Raydium"


# ---------------- Sellability tests ----------------

def test_sellability_confirmed_when_route_exists():
    engine = engine_with(quote_to_return=good_quote())
    result = engine.check_sellability("SoMeMeToken111", token_amount_raw=1_000_000)
    assert result.sellable


def test_sellability_fails_when_no_route():
    engine = engine_with(quote_to_return=JupiterNoRouteError("no_route"))
    result = engine.check_sellability("SoMeMeToken111", token_amount_raw=1_000_000)
    assert not result.sellable
    assert result.reason == "no_sell_route_found"


def test_sellability_fails_on_extreme_price_impact():
    """price impact عالي جدًا وقت البيع = مؤشر honeypot كلاسيكي."""
    engine = engine_with(quote_to_return=good_quote(priceImpactPct="0.40"))  # 40%!
    result = engine.check_sellability("SoMeMeToken111", token_amount_raw=1_000_000)
    assert not result.sellable
    assert result.reason == "extreme_sell_price_impact"


# ---------------- Full prepare_buy flow tests ----------------

def test_prepare_buy_blocked_when_not_sellable():
    engine = engine_with(quote_to_return=JupiterNoRouteError("no_route"))
    plan = engine.prepare_buy(
        token_mint="SoMeMeToken111",
        sol_amount_lamports=100_000_000,
        liquidity_usd=50000,
        user_public_key="FakePubKey111",
    )
    assert not plan.ready_for_signing
    assert plan.unsigned_swap_transaction is None
    assert "sellability_check_failed" in plan.rejection_summary


def test_prepare_buy_succeeds_end_to_end():
    engine = engine_with(quote_to_return=good_quote())
    plan = engine.prepare_buy(
        token_mint="SoMeMeToken111",
        sol_amount_lamports=100_000_000,
        liquidity_usd=50000,
        user_public_key="FakePubKey111",
    )
    assert plan.ready_for_signing
    assert plan.unsigned_swap_transaction == "base64_placeholder_tx=="
    assert plan.sellability.sellable
    assert plan.pre_trade_check.approved


def test_prepare_buy_blocked_by_pretrade_check_even_if_sellable():
    engine = engine_with(quote_to_return=good_quote(), swap_response={"swapTransaction": "xyz"})
    # نغيّر الـliquidity لتكون قليلة عشان نتأكد إن pre-trade check بيوقف حتى بعد نجاح sellability
    plan = engine.prepare_buy(
        token_mint="SoMeMeToken111",
        sol_amount_lamports=100_000_000,
        liquidity_usd=100,  # أقل من الحد الأدنى
        user_public_key="FakePubKey111",
    )
    assert not plan.ready_for_signing
    assert plan.sellability.sellable  # الـsellability عدت، بس الـpre-trade مارضاش
    assert plan.unsigned_swap_transaction is None


def test_prepare_buy_never_calls_swap_when_sellability_fails():
    """تأكيد إننا مانضيعش وقت/requests على تجهيز صفقة لعملة already blocked."""
    fake = _FakeJupiterClient(quote_to_return=JupiterNoRouteError("no_route"))
    engine = ExecutionEngine(jupiter_client=fake, risk_config_path=RISK_CONFIG)
    engine.prepare_buy(
        token_mint="SoMeMeToken111",
        sol_amount_lamports=100_000_000,
        liquidity_usd=50000,
        user_public_key="FakePubKey111",
    )
    # لازم يكون اتنادى مرة واحدة بس (فحص الـsellability)، مش مرتين
    assert len(fake.quote_calls) == 1


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
