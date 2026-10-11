"""
اختبارات Social Signal Providers.

تشغيل: python3 tests/test_social_signals.py
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.data.social_signals import DexScreenerSocialPresenceProvider, NullSocialSignalProvider


def test_null_provider_always_returns_neutral():
    provider = NullSocialSignalProvider()
    assert provider.get_social_score({}) == 50.0
    assert provider.get_social_score({"info": {"socials": [{"type": "twitter"}]}}) == 50.0


def test_dexscreener_provider_no_info_scores_lowest():
    provider = DexScreenerSocialPresenceProvider()
    score = provider.get_social_score({})
    assert score == DexScreenerSocialPresenceProvider.SCORE_NO_INFO


def test_dexscreener_provider_website_only():
    provider = DexScreenerSocialPresenceProvider()
    pair = {"info": {"websites": [{"url": "https://example.com"}], "socials": []}}
    score = provider.get_social_score(pair)
    assert score == DexScreenerSocialPresenceProvider.SCORE_WEBSITE_ONLY


def test_dexscreener_provider_one_social():
    provider = DexScreenerSocialPresenceProvider()
    pair = {"info": {"websites": [], "socials": [{"type": "twitter", "url": "https://x.com/x"}]}}
    score = provider.get_social_score(pair)
    assert score == DexScreenerSocialPresenceProvider.SCORE_ONE_SOCIAL


def test_dexscreener_provider_multiple_socials_scores_highest():
    provider = DexScreenerSocialPresenceProvider()
    pair = {"info": {"socials": [
        {"type": "twitter", "url": "https://x.com/x"},
        {"type": "telegram", "url": "https://t.me/x"},
    ]}}
    score = provider.get_social_score(pair)
    assert score == DexScreenerSocialPresenceProvider.SCORE_MULTIPLE_SOCIALS


def test_dexscreener_provider_scores_are_ordered_sensibly():
    """التأكيد إن الترتيب منطقي: مفيش حاجة < موقع بس < social واحد < أكتر من واحد."""
    provider = DexScreenerSocialPresenceProvider()
    no_info = provider.get_social_score({})
    website_only = provider.get_social_score({"info": {"websites": [{"url": "x"}], "socials": []}})
    one_social = provider.get_social_score({"info": {"socials": [{"type": "twitter"}]}})
    multiple = provider.get_social_score({"info": {"socials": [{"type": "twitter"}, {"type": "telegram"}]}})

    assert no_info < website_only < one_social < multiple


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
