"""
Social Presence Signal
=========================
⚠️ صراحة مهمة: ده **مش** إشارة "فيروسية" (virality) حقيقية. قياس
انتشار حقيقي على X/Twitter محتاج API مدفوع (X API v2 بتكلفة شهرية،
أو خدمة زي LunarCrush) — مش موجود في المشروع ده حاليًا.

اللي موجود فعليًا: DexScreener بيرجع أحيانًا حقل `info` في بيانات
الـpair فيه روابط (website, twitter, telegram) لو المشروع سجّلها.
وجود الروابط دي مؤشر ضعيف جدًا على "المشروع كلّف نفسه يعبي بيانات
أساسية" — أقرب لـchecklist شرعية بدائي مش قياس اهتمام حقيقي. مشروع
نصاب بسيط جدًا يقدر يحط رابط تويتر فاضي بنفس السهولة.

استخدمناه كـwزن صغير جدًا (0.05) في الـScanner بالظبط عشان كده —
إشارة مساعدة ضعيفة، مش عامل حاسم. لو عايز إشارة فيروسية حقيقية
لاحقًا، الخطوة الصح هي دمج LunarCrush API أو X API — البنية هنا
مصممة (SocialSignalProvider) عشان تقدر تستبدل الـprovider من غير
ما تلمس باقي الـScanner.
"""

from __future__ import annotations

from typing import Optional, Protocol


class SocialSignalProvider(Protocol):
    def get_social_score(self, pair: dict) -> float:
        """بيرجع 0-100. لازم كل implementation تلتزم بالـscale ده."""
        ...


class NullSocialSignalProvider:
    """
    الافتراضي — بيرجع 50 (حيادي) دايمًا. استخدمه لو مش عايز الإشارة
    دي تأثر خالص على الـScore (بدل ما تحذفها من الـconfig وتعيد توزيع
    الأوزان، سيبها موجودة بس حيادية).
    """
    def get_social_score(self, pair: dict) -> float:
        return 50.0


class DexScreenerSocialPresenceProvider:
    """
    بياخد نفس الـpair JSON اللي DexScreenerClient بيرجعه، وبيدوّر على
    حقل `info.socials` و`info.websites`. النقط دي تقريبية ومقصودة تكون
    متواضعة — مش قياس حقيقي لأي حاجة غير "هل البيانات الأساسية موجودة".
    """
    SCORE_NO_INFO = 30.0
    SCORE_WEBSITE_ONLY = 45.0
    SCORE_ONE_SOCIAL = 55.0
    SCORE_MULTIPLE_SOCIALS = 65.0

    def get_social_score(self, pair: dict) -> float:
        info = pair.get("info") or {}
        websites = info.get("websites") or []
        socials = info.get("socials") or []

        if not websites and not socials:
            return self.SCORE_NO_INFO
        if socials and len(socials) >= 2:
            return self.SCORE_MULTIPLE_SOCIALS
        if socials:
            return self.SCORE_ONE_SOCIAL
        return self.SCORE_WEBSITE_ONLY
