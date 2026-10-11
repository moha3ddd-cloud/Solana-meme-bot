"""
اختبارات Orchestrator — الـPaperTradingEngine والـScanner/SignalEngine/
RiskEngine كلهم حقيقيين (نفس config الإنتاج)، والمصادر الخارجية
(DexScreener/RugCheck/Discovery) fake بالكامل — مفيش شبكة حقيقية.

تشغيل: python3 tests/test_orchestrator.py
"""

import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.discovery import DiscoveredCandidate
from core.orchestrator import Orchestrator
from core.paper_trading import PaperTradingEngine, StrategyProfile
from core.risk_engine import RiskEngine
from core.scanner import ScannerEngine
from core.strategy import PositionManager, SignalEngine

from tests.test_onchain_layer import clean_report
from tests.test_paper_trading import (
    EXIT_RULES_CONSERVATIVE, EXIT_RULES_SCALP, RISK_CONFIG,
    SCANNER_CONFIG, STRATEGY_CONFIG, STRATEGY_SCALP_CONFIG,
)

ORCHESTRATOR_CONFIG = os.path.join(os.path.dirname(__file__), "..", "config", "orchestrator_config.yaml")


def healthy_pair(address="TokenAddr1", symbol="MEME", price="1.0", age_minutes=120):
    now_ms = int(time.time() * 1000)
    return {
        "chainId": "solana",
        "baseToken": {"address": address, "symbol": symbol},
        "priceUsd": price,
        "liquidity": {"usd": 50000},
        "marketCap": 300000,
        "volume": {"h24": 200000, "h1": 20000, "m5": 3000},
        "priceChange": {"m5": 3.0, "h1": 15.0},
        "txns": {"h1": {"buys": 300, "sells": 150}, "m5": {"buys": 20, "sells": 5}},
        "pairCreatedAt": now_ms - (age_minutes * 60 * 1000),
    }


class _FakeDiscovery:
    def __init__(self, candidates=None, raise_error=False):
        self._candidates = candidates or []
        self._raise_error = raise_error

    def discover(self):
        if self._raise_error:
            raise ConnectionError("simulated discovery failure")
        return self._candidates


class _FakeDexClient:
    def __init__(self, prices_by_address=None, raise_error=False):
        self._prices = prices_by_address or {}
        self._raise_error = raise_error
        self.call_count = 0

    def get_pairs_by_token_addresses(self, addresses):
        self.call_count += 1
        if self._raise_error:
            raise ConnectionError("simulated dexscreener failure")
        results = []
        for addr in addresses:
            if addr in self._prices:
                results.append({"priceUsd": str(self._prices[addr])})
        return results


class _FakeRugCheckClient:
    def __init__(self, report=None, raise_error=False):
        self._report = report if report is not None else clean_report()
        self._raise_error = raise_error

    def get_token_report(self, mint_address):
        if self._raise_error:
            raise ConnectionError("simulated rugcheck failure")
        return self._report


def build_engine() -> PaperTradingEngine:
    return PaperTradingEngine(
        risk_engine=RiskEngine(config_path=RISK_CONFIG),
        scanner=ScannerEngine(config_path=SCANNER_CONFIG),
        profiles={
            "conservative": StrategyProfile(
                signal_engine=SignalEngine(config_path=STRATEGY_CONFIG),
                position_manager=PositionManager(exit_rules_config_path=EXIT_RULES_CONSERVATIVE),
            ),
            "scalp": StrategyProfile(
                signal_engine=SignalEngine(config_path=STRATEGY_SCALP_CONFIG),
                position_manager=PositionManager(exit_rules_config_path=EXIT_RULES_SCALP),
            ),
        },
    )


def build_orchestrator(discovery, dex_client, rugcheck_client, engine=None) -> Orchestrator:
    return Orchestrator(
        discovery=discovery, dex_client=dex_client, rugcheck_client=rugcheck_client,
        engine=engine or build_engine(), config_path=ORCHESTRATOR_CONFIG,
    )


# ---------------- Basic cycle tests ----------------

def test_empty_cycle_when_no_candidates_and_no_open_positions():
    orch = build_orchestrator(_FakeDiscovery([]), _FakeDexClient(), _FakeRugCheckClient())
    result = orch.run_cycle()
    assert result.candidates_discovered == 0
    assert result.candidates_evaluated == 0
    assert not result.emergency_triggered


def test_candidate_discovered_and_evaluated():
    pair = healthy_pair()
    candidate = DiscoveredCandidate(address="TokenAddr1", symbol="MEME", pair=pair)
    orch = build_orchestrator(
        _FakeDiscovery([candidate]), _FakeDexClient(), _FakeRugCheckClient(report=clean_report()),
    )
    result = orch.run_cycle()
    assert result.candidates_discovered == 1
    assert result.candidates_evaluated == 1  # لازم يتبنيله TokenMetrics+TimingSignals بنجاح


def test_rugcheck_failure_records_execution_error_and_skips_candidate():
    pair = healthy_pair()
    candidate = DiscoveredCandidate(address="TokenAddr1", symbol="MEME", pair=pair)
    engine = build_engine()
    errors_before = engine.risk_engine.daily_state.consecutive_errors
    orch = build_orchestrator(
        _FakeDiscovery([candidate]), _FakeDexClient(), _FakeRugCheckClient(raise_error=True), engine=engine,
    )
    result = orch.run_cycle()
    assert result.candidates_evaluated == 0  # اترفضت قبل حتى الـScanner
    assert engine.risk_engine.daily_state.consecutive_errors == errors_before + 1


def test_discovery_failure_does_not_crash_cycle():
    orch = build_orchestrator(_FakeDiscovery(raise_error=True), _FakeDexClient(), _FakeRugCheckClient())
    result = orch.run_cycle()
    assert result.candidates_discovered == 0
    assert len(result.errors) == 1
    assert "discovery_failed" in result.errors[0]


# ---------------- Open position lifecycle ----------------

def test_open_position_updated_with_fresh_price():
    pair = healthy_pair(address="TokenAddr1")
    candidate = DiscoveredCandidate(address="TokenAddr1", symbol="MEME", pair=pair)
    engine = build_engine()
    orch = build_orchestrator(
        _FakeDiscovery([candidate]), _FakeDexClient(prices_by_address={"TokenAddr1": 1.3}),
        _FakeRugCheckClient(report=clean_report()), engine=engine,
    )
    # أول دورة: افتح الصفقة (لو عدّت الفلترة)
    orch.run_cycle()
    if "TokenAddr1" not in engine.open_positions:
        return  # العملة ممكن ترفض حسب الـScanner/Signal الحقيقيين — مش هدف الاختبار ده
    # تاني دورة: السعر اتغيّر، المفروض يتحدث
    dex_client_2 = _FakeDexClient(prices_by_address={"TokenAddr1": 1.3})
    orch2 = build_orchestrator(_FakeDiscovery([]), dex_client_2, _FakeRugCheckClient(), engine=engine)
    orch2.run_cycle()
    assert dex_client_2.call_count >= 1  # لازم يحاول يجيب سعر محدث للمفتوح


def test_price_fetch_failure_for_open_position_records_execution_error():
    pair = healthy_pair(address="TokenAddr1")
    engine = build_engine()
    # نفتح مركز يدويًا مباشرة عشان الاختبار يبقى حتمي، مش معتمد على نتيجة Scanner عشوائية
    from tests.test_scanner import healthy_token
    from tests.test_strategy import strong_signals
    token = healthy_token(address="TokenAddr1")
    engine.evaluate_candidate("conservative", token, strong_signals(), entry_price=1.0, stop_loss_price=0.9)
    assert "TokenAddr1" in engine.open_positions

    errors_before = engine.risk_engine.daily_state.consecutive_errors
    orch = build_orchestrator(
        _FakeDiscovery([]), _FakeDexClient(raise_error=True), _FakeRugCheckClient(), engine=engine,
    )
    orch.run_cycle()
    assert engine.risk_engine.daily_state.consecutive_errors == errors_before + 1


# ---------------- Emergency path ----------------

def test_emergency_triggered_skips_new_candidate_evaluation():
    from tests.test_scanner import healthy_token
    from tests.test_strategy import strong_signals

    engine = build_engine()
    token = healthy_token(address="OpenAddr")
    engine.evaluate_candidate("conservative", token, strong_signals(), entry_price=1.0, stop_loss_price=0.9)
    engine.risk_engine.activate_kill_switch()  # يفعّل الطوارئ يدويًا

    new_candidate = DiscoveredCandidate(address="NewAddr", symbol="NEW", pair=healthy_pair(address="NewAddr"))
    orch = build_orchestrator(
        _FakeDiscovery([new_candidate]), _FakeDexClient(prices_by_address={"OpenAddr": 1.1}),
        _FakeRugCheckClient(), engine=engine,
    )
    result = orch.run_cycle()
    assert result.emergency_triggered
    assert result.emergency_reason == "kill_switch_active"
    assert result.candidates_discovered == 0  # ماكملناش لمرحلة الاكتشاف أصلًا
    assert "OpenAddr" in result.positions_closed
    assert len(engine.open_positions) == 0


def test_cannot_trade_blocks_further_candidate_evaluation():
    """لو الـRiskEngine مانع صفقات جديدة (من غير طوارئ)، نوقف نقيّم مرشحين جداد."""
    pair1 = healthy_pair(address="Addr1")
    pair2 = healthy_pair(address="Addr2")
    candidates = [
        DiscoveredCandidate(address="Addr1", symbol="A", pair=pair1),
        DiscoveredCandidate(address="Addr2", symbol="B", pair=pair2),
    ]
    engine = build_engine()
    engine.risk_engine.record_trade_result(pnl_usd=-20.0)  # يضرب daily loss limit
    can_trade, _ = engine.risk_engine.can_open_new_trade()
    assert not can_trade

    orch = build_orchestrator(
        _FakeDiscovery(candidates), _FakeDexClient(), _FakeRugCheckClient(), engine=engine,
    )
    result = orch.run_cycle()
    assert result.candidates_evaluated == 0  # اتوقفنا قبل أول مرشح


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
