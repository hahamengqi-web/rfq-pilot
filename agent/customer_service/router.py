"""Rules-first, LLM-fallback intent routing for customer support."""

from __future__ import annotations

import asyncio
import json
import re

from .models import ConversationContext, RouteDecision


class HybridIntentRouter:
    ALLOWED = frozenset({
        "product_consultation", "after_sales", "commercial_inquiry",
        "human_handoff", "memory_update", "greeting", "catalog_overview",
        "conversation", "service_policy", "out_of_scope",
    })
    _MODEL = re.compile(
        r"(?<![A-Za-z0-9])(?:Archer|Aecher|Deco|Tapo|Omada|TL|RE|UE|AX|BE|AC)"
        r"[- ]?[A-Z0-9]{2,16}(?![A-Za-z0-9])",
        re.IGNORECASE,
    )
    _HANDOFF = re.compile(
        r"人工|真人|客服人员|投诉|human agent|real person|complaint|Mitarbeiter|Beschwerde",
        re.IGNORECASE,
    )
    _CONTACT = re.compile(
        r"如何|怎么|怎样|哪里|how|where|wie|wo",
        re.IGNORECASE,
    )
    _CONTACT_TARGET = re.compile(
        r"联系.{0,8}(?:人工|真人|客服)|(?:人工|真人|客服).{0,8}联系|contact.{0,12}(?:support|human|agent)|reach.{0,12}(?:support|human|agent)|(?:Support|Mitarbeiter).{0,12}(?:kontakt|erreich)",
        re.IGNORECASE,
    )
    _COMMERCIAL = re.compile(
        r"报价|价格|库存|现货|交期|起订量|MOQ|采购|批发|quote|price|stock|inventory|lead time|purchase|Angebot|Preis|Lager",
        re.IGNORECASE,
    )
    _ORDER_STATUS = re.compile(
        r"订单(?:状态|进度|查询|到哪|什么时候到)|物流|快递|运单|发货了吗|"
        r"order status|track(?:ing)? (?:my |the )?(?:order|parcel|shipment)|"
        r"shipment status|Lieferstatus|Sendungsverfolgung",
        re.IGNORECASE,
    )
    _PAYMENT = re.compile(
        r"付款方式|支付方式|怎么付款|如何付款|开发票|发票|账期|"
        r"payment method|how (?:can|do) I pay|invoice|billing terms|"
        r"Zahlungsart|Rechnung",
        re.IGNORECASE,
    )
    _SHIPPING = re.compile(
        r"运费|包邮|配送方式|能(?:寄|送|发)到|可以(?:寄|送|发)到|"
        r"shipping cost|shipping fee|shipping options|ship to|Versandkosten|Lieferoptionen",
        re.IGNORECASE,
    )
    _SERVICE_PROCESS = re.compile(
        r"售后.{0,8}(?:流程|怎么处理|怎么办)|怎么申请售后|维修流程|"
        r"after-sales process|how to request support|service process|Kundendienstablauf",
        re.IGNORECASE,
    )
    _CONTACT_DETAILS = re.compile(
        r"客服电话|客服邮箱|客服联系方式|人工客服联系方式|"
        r"support (?:phone|email|contact)|customer service (?:phone|email)|"
        r"Support(?:telefon|adresse|kontakt)",
        re.IGNORECASE,
    )
    _WARRANTY = re.compile(
        r"保修|质保|保固|warranty|guarantee period|Garantie|Gewährleistung",
        re.IGNORECASE,
    )
    _RETURNS = re.compile(
        r"退款|退货|退换|换货|索赔|refund|return|exchange|claim|"
        r"Rückerstattung|Rückgabe|Umtausch|Reklamation",
        re.IGNORECASE,
    )
    _SAFETY = re.compile(
        r"着火|冒烟|触电|烧焦|发烫严重|\bon fire\b|\bcaught fire\b|"
        r"\bsmok(?:e|ing)\b|\belectric shock\b|\bburning smell\b|"
        r"Rauch|Stromschlag",
        re.IGNORECASE,
    )
    _IDENTITY = re.compile(
        r"你们(?:是|属于)(?:哪|什么)(?:家)?公司|你(?:是|们是).{0,12}(?:官方|厂家|品牌)|"
        r"(?:是|有)(?:官方|授权)(?:客服|经销商|代理商)|"
        r"are you (?:the )?(?:official|manufacturer)|what company (?:are you|is this)|"
        r"seid ihr (?:offiziell|der Hersteller)",
        re.IGNORECASE,
    )
    _AFTER_SALES = re.compile(
        r"故障|坏了|断网|掉线|重启|恢复出厂|安装|配置|连接不上|维修|售后|"
        r"设置.{0,15}(?:MU-MIMO|Wi-Fi|路由器|网络|功能|Mesh|频段)|"
        r"(?:不能|无法)(?:上网|联网|连接|开机|启动|使用|配网)|"
        r"troubleshoot|not working|disconnect|reset|setup|install|repair|"
        r"Fehler|funktioniert nicht|zurücksetzen",
        re.IGNORECASE,
    )
    _PRODUCT = re.compile(
        r"参数|规格|功能|兼容|接口|端口|网口|LAN口|WAN口|区别|产品|功耗|覆盖范围|"
        r"支持.{0,12}(?:MU-MIMO|Wi-Fi|Mesh|OFDMA|IPv6|VPN|WPA|频段|速率|协议|接口|设备)|"
        r"推荐.{0,10}(?:路由器|网卡|交换机|摄像头|产品)|怎么选(?:路由器|产品)|"
        r"(?:路由器|网卡|交换机|摄像头).{0,10}(?:适合|推荐)|使用场景|"
        r"\b(?:spec(?:ification)?s?|features?|compatible|ports?|differences?)\b|"
        r"\b(?:Produkt|Spezifikation|kompatibel)\b",
        re.IGNORECASE,
    )
    _GREETING = re.compile(
        r"^(你好|您好|嗨|在吗|hello|hi|hey|guten tag|hallo)[!！,.，。\s]*$",
        re.IGNORECASE,
    )
    _CATALOG = re.compile(
        r"(?:有|卖|提供|主营).{0,8}(?:哪些|什么).{0,5}产品|产品.{0,5}(?:有哪些|目录|列表)|what products|product catalog|which products|welche produkte|produktkatalog",
        re.IGNORECASE,
    )
    _MANUAL_CATALOG = re.compile(
        r"(?:有|提供|能看|可以看|哪里有|有没有|这里有).{0,10}(?:用户手册|使用手册|说明书|操作手册|文档|资料)|"
        r"(?:用户手册|使用手册|说明书|操作手册).{0,8}(?:有吗|在哪|列表|目录|下载|链接)|"
        r"下载.{0,24}(?:用户手册|使用手册|说明书|操作手册)|"
        r"(?:download|get).{0,24}(?:manual|user guide)|"
        r"(?:manual|user guide|documentation).{0,10}(?:available|list|where)|"
        r"(?:handbuch|anleitung).{0,10}(?:verfügbar|liste|wo)",
        re.IGNORECASE,
    )
    _GENERIC_FAULT = re.compile(
        r"坏了|故障了|怎么维修|怎末维修|如何维修|需要维修|"
        r"broken|needs repair|repair it|kaputt|reparieren",
        re.IGNORECASE,
    )
    _CAPABILITY = re.compile(
        r"你(?:能|可以|会)(?:做|回答|帮助).{0,10}(?:什么|哪些)|能聊什么|怎么用|what can you do|how can you help|was kannst du|wobei kannst du helfen",
        re.IGNORECASE,
    )
    _PREFERENCE = re.compile(
        r"(?:我|我们)(?:喜欢|偏好|想要|需要).{0,20}(?:颜色|黑色|白色|银色|红色|蓝色|black|white|silver|red|blue)|I (?:like|prefer|want).{0,20}(?:black|white|silver|red|blue)|ich (?:mag|bevorzuge|möchte).{0,20}(?:schwarz|weiß|silber|rot|blau)",
        re.IGNORECASE,
    )
    _PREFERENCE_RECALL = re.compile(
        r"(?:你(?:还)?记得.{0,8})?我(?:喜欢|偏好)(?:的)?(?:什么|哪种|哪个)颜色|"
        r"what colou?r do I (?:like|prefer)|do you remember.{0,20}(?:my|I).{0,12}(?:colou?r|prefer)|"
        r"welche farbe (?:mag|bevorzuge) ich|erinnerst du dich.{0,20}farbe",
        re.IGNORECASE,
    )
    _THANKS = re.compile(
        r"^(?:谢谢|多谢|感谢|好的|明白了|知道了|thank you|thanks|ok|okay|danke)[!！,.，。\s]*$",
        re.IGNORECASE,
    )
    _REMEMBER = re.compile(
        r"^(?:请记住|记住|remember(?: that)?|bitte merke dir)\s*[:：,，]?\s*(.+)$",
        re.IGNORECASE | re.DOTALL,
    )

    def __init__(
        self, provider, *, model: str, confidence_threshold: float = 0.70,
        private_context_transfer_approved: bool = False,
    ) -> None:
        self.provider, self.model = provider, model
        self.confidence_threshold = confidence_threshold
        self.private_context_transfer_approved = private_context_transfer_approved

    @classmethod
    def _model(cls, text: str, context: ConversationContext) -> str:
        match = cls._MODEL.search(text)
        if match:
            model = match.group(0).strip()
            if model.casefold().startswith("aecher"):
                model = "Archer" + model[len("Aecher"):]
            return model
        return str(context.state.get("product_model") or "")

    def _rule(self, text: str, context: ConversationContext) -> RouteDecision | None:
        explicit_model = bool(self._MODEL.search(text))
        product = self._model(text, context)
        remember = self._REMEMBER.match(text.strip())
        if remember:
            return RouteDecision("memory_update", 1.0, product, reason=remember.group(1).strip())
        if self._SAFETY.search(text):
            return RouteDecision("human_handoff", 1.0, product, handoff_required=True,
                                 reason="product_safety")
        if self._RETURNS.search(text):
            return RouteDecision("human_handoff", 1.0, product, handoff_required=True,
                                 reason="returns_refunds")
        if self._CONTACT_DETAILS.search(text) or (
                self._CONTACT_TARGET.search(text) and self._CONTACT.search(text)):
            return RouteDecision("human_handoff", 1.0, product, handoff_required=True,
                                 reason="contact_request")
        if self._HANDOFF.search(text):
            return RouteDecision("human_handoff", 1.0, product, handoff_required=True,
                                 reason="explicit_or_high_risk_handoff")
        if self._ORDER_STATUS.search(text):
            return RouteDecision("commercial_inquiry", .99, product,
                                 handoff_required=True, reason="order_status")
        if self._PAYMENT.search(text):
            return RouteDecision("commercial_inquiry", .99, product,
                                 handoff_required=True, reason="payment_invoice")
        if self._SHIPPING.search(text):
            return RouteDecision("commercial_inquiry", .99, product,
                                 handoff_required=True, reason="shipping_policy")
        if self._COMMERCIAL.search(text):
            return RouteDecision("commercial_inquiry", .98, product,
                                 handoff_required=True, reason="commercial_scope")
        if self._GREETING.match(text.strip()):
            return RouteDecision("greeting", .99, product)
        if self._IDENTITY.search(text):
            return RouteDecision("conversation", .99, product,
                                 reason="company_identity_rule")
        if self._SERVICE_PROCESS.search(text):
            return RouteDecision("conversation", .99, product,
                                 reason="service_process_rule")
        if self._MANUAL_CATALOG.search(text):
            return RouteDecision("catalog_overview", .99, product,
                                 reason="manual_catalog_rule")
        if self._CATALOG.search(text):
            return RouteDecision("catalog_overview", .99, product,
                                 reason="catalog_overview_rule")
        if self._CAPABILITY.search(text):
            return RouteDecision("conversation", .99, product, reason="capability_rule")
        if self._PREFERENCE_RECALL.search(text):
            return RouteDecision("conversation", .99, product,
                                 reason="preference_recall_rule")
        if self._PREFERENCE.search(text):
            return RouteDecision("conversation", .99, product, reason="preference_rule")
        if self._THANKS.match(text.strip()):
            return RouteDecision("conversation", .99, product, reason="thanks_rule")
        if self._WARRANTY.search(text):
            return RouteDecision("service_policy", .98, product,
                                 needs_retrieval=True, reason="warranty_policy_rule")
        if self._AFTER_SALES.search(text):
            generic_fault = bool(self._GENERIC_FAULT.search(text))
            return RouteDecision(
                "after_sales", .94, product, needs_retrieval=bool(product),
                needs_clarification=(not bool(product) or generic_fault),
                missing_fields=(
                    ("product_model",) if not product else
                    ("issue_symptom",) if generic_fault else ()
                ),
                reason="after_sales_rule",
            )
        if explicit_model or self._PRODUCT.search(text):
            return RouteDecision(
                "product_consultation", .92, product,
                needs_retrieval=bool(product),
                needs_clarification=not bool(product),
                missing_fields=(() if product else ("product_model",)),
                reason="product_rule",
            )
        return None

    async def route(self, text: str, context: ConversationContext) -> RouteDecision:
        ruled = self._rule(text, context)
        if ruled is not None:
            return ruled
        recent = list(context.recent_turns)[-5:]
        prompt = {
            "task": "Classify one customer-support message. Return JSON only.",
            "allowed_intents": sorted(self.ALLOWED - {"memory_update"}),
            "rules": [
                "product facts => product_consultation",
                "setup, fault or maintenance => after_sales",
                "price, stock, MOQ or delivery => commercial_inquiry and handoff",
                "order tracking, payment and invoice questions => commercial_inquiry and handoff",
                "warranty duration or coverage => service_policy; search only public warranty documents",
                "unsafe, complaint, refund or explicit human request => human_handoff",
                "unclear after-sales request without a model => needs_clarification",
                "ordinary social chat within a customer-service conversation => conversation",
            ],
            "older_summary": context.summary,
            "recent_turns": recent,
            "confirmed_user_memories": (
                list(context.long_term_memories)
                if self.private_context_transfer_approved else []
            ),
            "working_state": context.state,
            "message": text,
            "schema": {
                "intent": "string", "confidence": "0..1", "product_model": "string",
                "needs_retrieval": "boolean", "needs_clarification": "boolean",
                "missing_fields": ["string"], "handoff_required": "boolean",
                "reason": "short string",
            },
        }
        try:
            response = await asyncio.wait_for(
                self.provider.chat(
                    [{"role": "user", "content": json.dumps(prompt, ensure_ascii=False)}],
                    tools=None, model=self.model,
                ),
                timeout=12,
            )
            raw = str(response.content or "").strip()
            start, end = raw.find("{"), raw.rfind("}")
            data = json.loads(raw[start:end + 1])
            intent = str(data.get("intent") or "out_of_scope")
            if intent not in self.ALLOWED:
                intent = "out_of_scope"
            confidence = max(0.0, min(1.0, float(data.get("confidence") or 0)))
            model = str(data.get("product_model") or self._model(text, context)).strip()[:80]
            handoff = bool(data.get("handoff_required")) or intent in {
                "commercial_inquiry", "human_handoff",
            }
            missing = tuple(str(item)[:40] for item in data.get("missing_fields") or ())
            clarify = bool(data.get("needs_clarification"))
            if intent == "out_of_scope":
                intent, clarify, missing = "conversation", False, ()
            elif confidence < self.confidence_threshold:
                clarify, missing = True, missing or ("customer_intent",)
            if intent == "service_policy":
                handoff = False
            return RouteDecision(
                intent, confidence, model,
                (intent == "service_policy" or bool(data.get("needs_retrieval"))) and not handoff,
                clarify, missing, handoff, str(data.get("reason") or "llm_route")[:160],
            )
        except Exception:
            return RouteDecision(
                "conversation", 0.0, self._model(text, context),
                reason="router_unavailable",
            )
