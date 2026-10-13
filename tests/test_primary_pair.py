"""
اختبارات اختيار الـpair الحي (select_primary_pair) — مبنية على حادثة Bear:
عملة pump.fun ليها pair bonding curve قديم بسعر متجمّد + pair pumpswap حي.
الخروج كان بيتسعّر من pairs[0] (القديم) فالستوب اتضرب بخسارة وهمية.

تشغيل: python3 tests/test_primary_pair.py
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.data.mappers import select_primary_pair
from core.discovery import DiscoveryLayer, SeenTokensStore

from tests.test_cycle_log import DISCOVERY_CONFIG, _DexForDiscovery
from tests.test_orchestrator import (
    _FakeDiscovery, _FakeRugCheckClient, build_orchestrator, healthy_pair, _engine_with_open_position,
)

ADDR = "TokenAddr1"


def stale_pumpfun_pair(price="0.00004047"):
    """pair bonding curve بعد الـmigration: سعر متجمّد ومفيش liquidity.usd."""
    p = healthy_pair(address=ADDR, price=price)
    p["dexId"] = "pumpfun"
    p.pop("liquidity", None)
    p["volume"] = {"h24": 640, "h1": 0, "m5": 0}
    return p


def live_pumpswap_pair(price="1.0"):
    p = healthy_pair(address=ADDR, price=price)
    p["dexId"] = "pumpswap"
    p["liquidity"] = {"usd": 17921}
    return p


def test_prefers_live_pair_over_stale_even_if_stale_is_first():
    stale, live = stale_pumpfun_pair(), live_pumpswap_pair()
    assert select_primary_pair([stale, live], ADDR) is live
    assert select_primary_pair([live, stale], ADDR) is live


def test_filters_by_token_address_and_handles_empty():
    other = healthy_pair(address="Other")
    live = live_pumpswap_pair()
    assert select_primary_pair([other, live], ADDR) is live
    assert select_primary_pair([other], ADDR) is None
    assert select_primary_pair([], ADDR) is None


def test_volume_breaks_liquidity_ties():
    a = live_pumpswap_pair(); b = live_pumpswap_pair()
    a["volume"] = {"h24": 10}; b["volume"] = {"h24": 999}
    assert select_primary_pair([a, b], ADDR) is b


class _MultiPairDex:
    """بيرجع pair قديم متجمّد الأول وبعده الحي (نفس ترتيب حادثة Bear)."""
    def __init__(self, live_price):
        self.live_price = live_price

    def get_pairs_by_token_addresses(self, addresses):
        return [stale_pumpfun_pair("0.00004047"), live_pumpswap_pair(self.live_price)]


def test_exit_price_uses_live_pair_so_no_phantom_stop_out():
    engine = _engine_with_open_position(ADDR)  # دخول بسعر 1.0، ستوب 0.9
    orch = build_orchestrator(_FakeDiscovery([]), _MultiPairDex(live_price="1.02"), _FakeRugCheckClient(), engine=engine)
    from core.orchestrator import CycleResult
    orch._update_open_positions(CycleResult())
    # قبل الإصلاح: pairs[0] = السعر المتجمّد 0.00004047 -> ستوب اتضرب بخسارة 99.99%
    assert ADDR in engine.open_positions
    assert not engine.closed_trades


def test_real_drop_on_live_pair_still_triggers_stop():
    engine = _engine_with_open_position(ADDR)
    orch = build_orchestrator(_FakeDiscovery([]), _MultiPairDex(live_price="0.85"), _FakeRugCheckClient(), engine=engine)
    from core.orchestrator import CycleResult
    orch._update_open_positions(CycleResult())
    assert ADDR not in engine.open_positions  # الستوب الحقيقي لسه شغال
    assert engine.closed_trades[-1]["total_pnl_usd"] < 0


def test_discovery_evaluates_one_pair_per_token():
    stale, live = stale_pumpfun_pair(), live_pumpswap_pair()
    stale["liquidity"] = {"usd": 50000}  # حتى لو عدّى الفلتر الرخيص، الحي هو اللي يتختار بالسيولة
    live["liquidity"] = {"usd": 60000}
    tmp = tempfile.mkdtemp()
    d = DiscoveryLayer(
        client=_DexForDiscovery([stale, live], [{"tokenAddress": ADDR}]),
        seen_store=SeenTokensStore(db_path=os.path.join(tmp, "seen.db")),
        config_path=DISCOVERY_CONFIG,
    )
    out = d.discover()
    assert len(out) == 1
    assert out[0].pair["dexId"] == "pumpswap"


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
