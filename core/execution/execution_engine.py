"""
Execution Engine
=================
بيدمج Jupiter quotes مع قواعد الأمان (execution_safety في
risk_config.yaml) قبل ما يوافق على أي صفقة. مسؤوليته: "هل التنفيذ ده
آمن ومنطقي؟" — مش "نفّذه فعليًا".

**الحد الفاصل مهم:** الكلاس ده بيوصل لحد ما يجهز unsigned transaction
(base64) ويرجعها. مايعملش أي توقيع ولا بيبعت أي حاجة للشبكة. التوقيع
والإرسال هيبقوا في طبقة Wallet/Signer منفصلة — لسه هنبنيها بعدين.

كمان بيوفر check_sellability() اللي بتحل الـplaceholder اللي سبناه في
core/data/onchain_mappers.py (is_sellable=False افتراضيًا) — بنعمل
quote حقيقي من التوكن رجوع لـSOL؛ لو مفيش route أو الـprice impact
جنوني، ده مؤشر قوي على honeypot أو سيولة ميتة.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import yaml

from core.execution.jupiter_client import (
    SOL_MINT,
    JupiterClient,
    JupiterError,
    JupiterNoRouteError,
)


@dataclass
class SellabilityResult:
    sellable: bool
    reason: str
    price_impact_pct: Optional[float] = None


@dataclass
class PreTradeCheckResult:
    approved: bool
    rejection_reasons: list[str] = field(default_factory=list)
    price_impact_pct: Optional[float] = None
    estimated_output_amount: Optional[int] = None
    route_summary: Optional[str] = None


@dataclass
class ExecutionPlan:
    """
    الناتج النهائي — لو ready_for_signing=True، unsigned_swap_transaction
    فيها الـbase64 transaction الجاهزة تتبعت لطبقة الـWallet/Signer.
    لو False، الصفقة اتوقفت هنا ومفيش transaction اتجهزت أصلًا.
    """
    ready_for_signing: bool
    pre_trade_check: PreTradeCheckResult
    sellability: Optional[SellabilityResult] = None
    unsigned_swap_transaction: Optional[str] = None
    rejection_summary: Optional[str] = None


class ExecutionEngine:
    def __init__(self, jupiter_client: JupiterClient, risk_config_path: str = "config/risk_config.yaml"):
        self.client = jupiter_client
        self.risk_config_path = risk_config_path
        self._load_config()

    def _load_config(self) -> None:
        with open(self.risk_config_path, "r", encoding="utf-8") as f:
            full_config = yaml.safe_load(f)
        self.safety_config = full_config["execution_safety"]

    def reload_config(self) -> None:
        self._load_config()

    # ------------------------------------------------------------------
    # Sellability check (يحل is_sellable placeholder من onchain_mappers)
    # ------------------------------------------------------------------
    def check_sellability(self, token_mint: str, token_amount_raw: int) -> SellabilityResult:
        """
        بيحاول ياخد quote من التوكن رجوع لـSOL. ده مش ضمان 100% (توكن
        ممكن يكون sellable دلوقتي ويتقفل بعدين)، بس أقوى إشارة عملية
        متاحة من غير simulate transaction فعلي على السلسلة.
        """
        try:
            quote = self.client.get_quote(
                input_mint=token_mint,
                output_mint=SOL_MINT,
                amount=token_amount_raw,
                slippage_bps=self._slippage_bps(),
            )
        except JupiterNoRouteError:
            return SellabilityResult(sellable=False, reason="no_sell_route_found")
        except JupiterError as e:
            return SellabilityResult(sellable=False, reason=f"quote_error:{e}")

        price_impact = self._extract_price_impact(quote)
        max_impact = self.safety_config["max_price_impact_pct"]

        if price_impact is not None and price_impact > max_impact * 3:
            # price impact عالي جدًا جدًا لما نحاول نبيع = مؤشر قوي على
            # سيولة وهمية من جهة واحدة (كلاسيكي في الـhoneypots)
            return SellabilityResult(
                sellable=False,
                reason="extreme_sell_price_impact",
                price_impact_pct=price_impact,
            )

        return SellabilityResult(sellable=True, reason="sell_route_confirmed", price_impact_pct=price_impact)

    # ------------------------------------------------------------------
    # Pre-trade safety check (buy side)
    # ------------------------------------------------------------------
    def _slippage_bps(self) -> int:
        return int(self.safety_config["max_slippage_pct"] * 100)

    def _extract_price_impact(self, quote: dict) -> Optional[float]:
        raw = quote.get("priceImpactPct")
        if raw is None:
            return None
        try:
            return float(raw) * 100  # Jupiter بيرجعها كنسبة عشرية (0.01 = 1%)
        except (TypeError, ValueError):
            return None

    def evaluate_pre_trade(self, quote: dict, liquidity_usd: float) -> PreTradeCheckResult:
        reasons: list[str] = []
        cfg = self.safety_config

        price_impact = self._extract_price_impact(quote)
        if price_impact is not None and price_impact > cfg["max_price_impact_pct"]:
            reasons.append(f"price_impact_too_high ({price_impact:.2f}% > {cfg['max_price_impact_pct']}%)")

        if liquidity_usd < cfg["min_liquidity_usd"]:
            reasons.append(f"liquidity_below_min (${liquidity_usd:,.0f})")

        out_amount = quote.get("outAmount")
        try:
            out_amount_int = int(out_amount) if out_amount is not None else None
        except (TypeError, ValueError):
            out_amount_int = None

        if out_amount_int is None or out_amount_int <= 0:
            reasons.append("invalid_or_zero_output_amount")

        route_plan = quote.get("routePlan") or []
        route_summary = " -> ".join(
            step.get("swapInfo", {}).get("label", "?") for step in route_plan
        ) if route_plan else None

        return PreTradeCheckResult(
            approved=len(reasons) == 0,
            rejection_reasons=reasons,
            price_impact_pct=price_impact,
            estimated_output_amount=out_amount_int,
            route_summary=route_summary,
        )

    # ------------------------------------------------------------------
    # Full flow: quote -> safety checks -> sellability -> unsigned tx
    # ------------------------------------------------------------------
    def prepare_buy(
        self,
        token_mint: str,
        sol_amount_lamports: int,
        liquidity_usd: float,
        user_public_key: str,
        check_sellability_first: bool = True,
    ) -> ExecutionPlan:
        """
        الترتيب مقصود: نتأكد إن التوكن قابل للبيع الأول (أهم فحص)
        قبل ما نضيع وقت/requests على تجهيز صفقة شراء لعملة هنعلق فيها.
        """
        if check_sellability_first:
            # تقدير مبسط لكمية التوكن المتوقعة (تفصيل دقيق هيحتاج quote
            # شراء الأول؛ هنا بنستخدم كمية رمزية صغيرة كفاية للفحص فقط)
            probe_result = self.check_sellability(token_mint, token_amount_raw=1_000_000)
            if not probe_result.sellable:
                empty_check = PreTradeCheckResult(approved=False, rejection_reasons=["blocked_by_sellability_check"])
                return ExecutionPlan(
                    ready_for_signing=False,
                    pre_trade_check=empty_check,
                    sellability=probe_result,
                    rejection_summary=f"sellability_check_failed: {probe_result.reason}",
                )
        else:
            probe_result = None

        try:
            quote = self.client.get_quote(
                input_mint=SOL_MINT,
                output_mint=token_mint,
                amount=sol_amount_lamports,
                slippage_bps=self._slippage_bps(),
            )
        except JupiterNoRouteError:
            check = PreTradeCheckResult(approved=False, rejection_reasons=["no_buy_route_found"])
            return ExecutionPlan(
                ready_for_signing=False, pre_trade_check=check, sellability=probe_result,
                rejection_summary="no_buy_route_found",
            )
        except JupiterError as e:
            check = PreTradeCheckResult(approved=False, rejection_reasons=[f"quote_error:{e}"])
            return ExecutionPlan(
                ready_for_signing=False, pre_trade_check=check, sellability=probe_result,
                rejection_summary=f"quote_error:{e}",
            )

        check = self.evaluate_pre_trade(quote, liquidity_usd)
        if not check.approved:
            return ExecutionPlan(
                ready_for_signing=False,
                pre_trade_check=check,
                sellability=probe_result,
                rejection_summary="; ".join(check.rejection_reasons),
            )

        try:
            swap_response = self.client.get_swap_transaction(quote, user_public_key)
        except JupiterError as e:
            check.approved = False
            check.rejection_reasons.append(f"swap_build_error:{e}")
            return ExecutionPlan(
                ready_for_signing=False, pre_trade_check=check, sellability=probe_result,
                rejection_summary=f"swap_build_error:{e}",
            )

        return ExecutionPlan(
            ready_for_signing=True,
            pre_trade_check=check,
            sellability=probe_result,
            unsigned_swap_transaction=swap_response.get("swapTransaction"),
        )
