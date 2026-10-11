"""
اختبارات Discovery Layer — بـfake client (مفيش شبكة حقيقية)، بيتأكد
من الفلترة الرخيصة، الـcooldown، ودمج المصادر المختلفة.

تشغيل: python3 tests/test_discovery.py
"""

import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.discovery import DiscoveryLayer, SeenTokensStore

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config", "discovery_config.yaml")


def sample_pair(address="TokenAddr1", symbol="MEME", liquidity=50000, volume_5m=2000, age_minutes=60):
    now_ms = int(time.time() * 1000)
    return {
        "chainId": "solana",
        "baseToken": {"address": address, "symbol": symbol},
        "liquidity": {"usd": liquidity},
        "volume": {"m5": volume_5m},
        "pairCreatedAt": now_ms - (age_minutes * 60 * 1000),
    }


class _FakeClient:
    """بديل لـDexScreenerClient — بنتحكم في كل مصدر بيانات يدويًا."""
    def __init__(self, profiles=None, boosts=None, pairs_by_address=None, raise_on_profiles=False):
        self._profiles = profiles or []
        self._boosts = boosts or []
        self._pairs_by_address = pairs_by_address or {}
        self._raise_on_profiles = raise_on_profiles
        self.batch_calls = []

    def get_latest_token_profiles(self):
        if self._raise_on_profiles:
            raise ConnectionError("simulated network failure")
        return self._profiles

    def get_latest_token_boosts(self):
        return self._boosts

    def get_pairs_by_token_addresses(self, addresses):
        self.batch_calls.append(list(addresses))
        return [self._pairs_by_address[a] for a in addresses if a in self._pairs_by_address]


def fresh_seen_store(tmp_dir: str) -> SeenTokensStore:
    return SeenTokensStore(db_path=os.path.join(tmp_dir, "seen.db"))


def discovery_with(client, tmp_dir) -> DiscoveryLayer:
    return DiscoveryLayer(client=client, seen_store=fresh_seen_store(tmp_dir), config_path=CONFIG_PATH)


# ---------------- SeenTokensStore tests ----------------

def test_seen_store_not_seen_initially():
    with tempfile.TemporaryDirectory() as tmp:
        store = fresh_seen_store(tmp)
        assert not store.is_recently_seen("AddrX", cooldown_minutes=30)


def test_seen_store_marks_and_detects_within_cooldown():
    with tempfile.TemporaryDirectory() as tmp:
        store = fresh_seen_store(tmp)
        store.mark_seen("AddrX")
        assert store.is_recently_seen("AddrX", cooldown_minutes=30)


# ---------------- Discovery pre-filter tests ----------------

def test_healthy_candidate_passes_prefilter():
    pair = sample_pair(liquidity=50000, volume_5m=2000, age_minutes=60)
    with tempfile.TemporaryDirectory() as tmp:
        client = _FakeClient(
            profiles=[{"chainId": "solana", "tokenAddress": "TokenAddr1"}],
            pairs_by_address={"TokenAddr1": pair},
        )
        discovery = discovery_with(client, tmp)
        results = discovery.discover()
        assert len(results) == 1
        assert results[0].address == "TokenAddr1"
        assert results[0].symbol == "MEME"


def test_low_liquidity_candidate_rejected_but_marked_seen():
    pair = sample_pair(liquidity=2000)  # أقل من min_liquidity_usd في الـconfig
    with tempfile.TemporaryDirectory() as tmp:
        client = _FakeClient(
            profiles=[{"chainId": "solana", "tokenAddress": "TokenAddr1"}],
            pairs_by_address={"TokenAddr1": pair},
        )
        seen_store = fresh_seen_store(tmp)
        discovery = DiscoveryLayer(client=client, seen_store=seen_store, config_path=CONFIG_PATH)
        results = discovery.discover()
        assert len(results) == 0
        # لازم يتسجل "شفناها" بغض النظر عن الرفض — عشان مانعيدش الطلب عليها فورًا
        assert seen_store.is_recently_seen("TokenAddr1", cooldown_minutes=30)


def test_low_volume_candidate_rejected():
    pair = sample_pair(liquidity=50000, volume_5m=10)  # نشاط شبه منعدم
    with tempfile.TemporaryDirectory() as tmp:
        client = _FakeClient(
            profiles=[{"chainId": "solana", "tokenAddress": "TokenAddr1"}],
            pairs_by_address={"TokenAddr1": pair},
        )
        discovery = discovery_with(client, tmp)
        results = discovery.discover()
        assert len(results) == 0


def test_too_old_candidate_rejected():
    pair = sample_pair(age_minutes=2000)  # أكتر من max_token_age_minutes (720 في الـconfig)
    with tempfile.TemporaryDirectory() as tmp:
        client = _FakeClient(
            profiles=[{"chainId": "solana", "tokenAddress": "TokenAddr1"}],
            pairs_by_address={"TokenAddr1": pair},
        )
        discovery = discovery_with(client, tmp)
        results = discovery.discover()
        assert len(results) == 0


# ---------------- Source combination & dedup tests ----------------

def test_combines_profiles_and_boosts_and_dedupes():
    pair = sample_pair(address="SharedAddr")
    with tempfile.TemporaryDirectory() as tmp:
        client = _FakeClient(
            profiles=[{"chainId": "solana", "tokenAddress": "SharedAddr"}],
            boosts=[{"chainId": "solana", "tokenAddress": "SharedAddr"}],  # نفس العنوان في المصدرين
            pairs_by_address={"SharedAddr": pair},
        )
        discovery = discovery_with(client, tmp)
        results = discovery.discover()
        assert len(results) == 1  # اتفحصت مرة واحدة بس رغم ظهورها في مصدرين
        assert len(client.batch_calls[0]) == 1


def test_manual_watchlist_included():
    pair = sample_pair(address="ManualAddr")
    with tempfile.TemporaryDirectory() as tmp:
        # نعدّل نسخة مؤقتة من الـconfig بواتش ليست يدوية
        import yaml
        with open(CONFIG_PATH) as f:
            cfg = yaml.safe_load(f)
        cfg["discovery_sources"]["manual_watchlist_addresses"] = ["ManualAddr"]
        cfg["discovery_sources"]["use_token_profiles"] = False
        cfg["discovery_sources"]["use_token_boosts"] = False
        tmp_config_path = os.path.join(tmp, "discovery_test_config.yaml")
        with open(tmp_config_path, "w") as f:
            yaml.dump(cfg, f)

        client = _FakeClient(pairs_by_address={"ManualAddr": pair})
        discovery = DiscoveryLayer(client=client, seen_store=fresh_seen_store(tmp), config_path=tmp_config_path)
        results = discovery.discover()
        assert len(results) == 1
        assert results[0].address == "ManualAddr"


def test_recently_seen_candidate_skipped_without_extra_batch_call():
    pair = sample_pair(address="RepeatAddr")
    with tempfile.TemporaryDirectory() as tmp:
        seen_store = fresh_seen_store(tmp)
        client = _FakeClient(
            profiles=[{"chainId": "solana", "tokenAddress": "RepeatAddr"}],
            pairs_by_address={"RepeatAddr": pair},
        )
        discovery = DiscoveryLayer(client=client, seen_store=seen_store, config_path=CONFIG_PATH)

        first_pass = discovery.discover()
        assert len(first_pass) == 1

        second_pass = discovery.discover()  # نفس العملة تاني — لسه جوه الـcooldown
        assert len(second_pass) == 0
        assert len(client.batch_calls) == 1  # مفيش batch request تاني اتعمل خالص


def test_one_failing_source_does_not_block_the_other():
    pair = sample_pair(address="BoostOnlyAddr")
    with tempfile.TemporaryDirectory() as tmp:
        client = _FakeClient(
            raise_on_profiles=True,  # مصدر الـprofiles فشل
            boosts=[{"chainId": "solana", "tokenAddress": "BoostOnlyAddr"}],
            pairs_by_address={"BoostOnlyAddr": pair},
        )
        discovery = discovery_with(client, tmp)
        results = discovery.discover()
        assert len(results) == 1  # مصدر الـboosts لسه شغال رغم فشل التاني


def test_max_candidates_per_cycle_respected():
    pairs = {f"Addr{i}": sample_pair(address=f"Addr{i}") for i in range(30)}
    with tempfile.TemporaryDirectory() as tmp:
        import yaml
        with open(CONFIG_PATH) as f:
            cfg = yaml.safe_load(f)
        cfg["limits"]["max_candidates_per_cycle"] = 5
        cfg["discovery_sources"]["use_token_boosts"] = False
        cfg["discovery_sources"]["manual_watchlist_addresses"] = list(pairs.keys())
        tmp_config_path = os.path.join(tmp, "discovery_limit_config.yaml")
        with open(tmp_config_path, "w") as f:
            yaml.dump(cfg, f)

        client = _FakeClient(pairs_by_address=pairs)
        discovery = DiscoveryLayer(client=client, seen_store=fresh_seen_store(tmp), config_path=tmp_config_path)
        results = discovery.discover()
        assert len(results) == 5


# ---------------- GeckoTerminal as additional source ----------------

class _FakeGeckoTerminalClient:
    def __init__(self, new_pools_response=None, trending_response=None):
        self._new_pools = new_pools_response or {"data": []}
        self._trending = trending_response or {"data": []}

    def get_new_pools(self):
        return self._new_pools

    def get_trending_pools(self):
        return self._trending


def _gecko_pools_response(address="GeckoAddr1", symbol="GECKO"):
    return {
        "data": [{
            "id": f"solana_{address}",
            "attributes": {"address": "PoolXYZ", "reserve_in_usd": "0", "volume_usd": {}},
            "relationships": {"base_token": {"data": {"id": f"solana_{address}"}}},
        }],
        "included": [{
            "id": f"solana_{address}",
            "type": "token",
            "attributes": {"address": address, "symbol": symbol},
        }],
    }


def test_geckoterminal_new_pools_added_as_source_when_enabled():
    pair = sample_pair(address="GeckoAddr1", symbol="GECKO")
    with tempfile.TemporaryDirectory() as tmp:
        import yaml
        with open(CONFIG_PATH) as f:
            cfg = yaml.safe_load(f)
        cfg["discovery_sources"]["use_token_profiles"] = False
        cfg["discovery_sources"]["use_token_boosts"] = False
        cfg["discovery_sources"]["use_geckoterminal_new_pools"] = True
        tmp_config_path = os.path.join(tmp, "discovery_gecko_config.yaml")
        with open(tmp_config_path, "w") as f:
            yaml.dump(cfg, f)

        client = _FakeClient(pairs_by_address={"GeckoAddr1": pair})
        gecko_client = _FakeGeckoTerminalClient(new_pools_response=_gecko_pools_response())
        discovery = DiscoveryLayer(
            client=client, seen_store=fresh_seen_store(tmp),
            config_path=tmp_config_path, geckoterminal_client=gecko_client,
        )
        results = discovery.discover()
        assert len(results) == 1
        assert results[0].address == "GeckoAddr1"


def test_geckoterminal_disabled_by_default_in_shipped_config():
    """تأكيد إن الإعداد الافتراضي محافظ — مايفعّلش GeckoTerminal من غير قرار واعي."""
    import yaml
    with open(CONFIG_PATH) as f:
        cfg = yaml.safe_load(f)
    assert cfg["discovery_sources"]["use_geckoterminal_new_pools"] is False
    assert cfg["discovery_sources"]["use_geckoterminal_trending"] is False


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
