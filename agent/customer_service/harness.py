"""Deterministic customer-service Harness around the public RAG tool."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import re
import unicodedata
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

from agent.customer_security import CustomerDataGuard
from agent.tools.registry import ToolRegistry

from .memory import CustomerLongTermMemory, RedisConversationMemory
from .models import ConversationContext, RouteDecision
from .reliability import CircuitOpen, ReliableToolExecutor
from .router import HybridIntentRouter


class CustomerServiceHarness:
    """Run one customer turn through memory, routing, RAG and escalation."""

    _TOOL_MARKER = re.compile(r"</?tool(?:call|_call)>|<arg(?:key|value)>", re.I)
    _CONCEPTUAL_PRODUCT_QUERY = re.compile(
        r"使用场景|适用|适合|推荐|怎么选|优缺点|场景|use case|suitable|recommend|scenario|geeignet|einsatz",
        re.I,
    )
    _PROCEDURE_QUERY = re.compile(
        r"怎么|怎样|如何|设置|配置|安装|操作|使用|开启|关闭|重置|复位|"
        r"setup|configure|configuration|install|enable|disable|reset|how to|"
        r"einrichten|konfigurieren|installieren|zurücksetzen",
        re.I,
    )
    _QUESTION_FOCUS = re.compile(
        r"(?:如何|怎么|怎样|有什么|有哪些|哪些|是否|能否|为什么|为何|哪里|多少|"
        r"how\s+to|how|what|which|does|do|can|where|why|"
        r"wie|was|welche|warum|wo).*$",
        re.I,
    )
    _COLOR_PREFERENCE = re.compile(
        r"黑色|白色|银色|红色|蓝色|绿色|金色|灰色|粉色|"
        r"black|white|silver|red|blue|green|gold|gr[ae]y|pink|"
        r"schwarz|weiß|silber|rot|blau|grün|gold|grau|rosa",
        re.I,
    )
    _PERSONAL_MEMORY_QUERY = re.compile(
        r"你(?:还)?记得.{0,24}(?:我|我的)|"
        r"我.{0,24}(?:什么|哪些).{0,16}(?:场景|用途|偏好|需求|习惯)|"
        r"what do you remember.{0,24}(?:about me|my)|my (?:use case|preference|need)|"
        r"was wei(?:ß|ss)t du.{0,24}(?:über mich|meine)|meine (?:vorliebe|anforderung)",
        re.I,
    )

    def __init__(
        self, *, session_key: str, provider, model: str, knowledge_tool,
        memory: RedisConversationMemory, router: HybridIntentRouter,
        executor: ReliableToolExecutor,
        catalog_tool=None,
        long_term_memory: CustomerLongTermMemory | None = None,
        private_context_transfer_approved: bool = False,
        public_contact_email: str = "",
    ) -> None:
        self.session_key = session_key
        self.provider = provider
        self.model = model
        self.knowledge_tool = knowledge_tool
        self.catalog_tool = catalog_tool
        self.memory = memory
        self.router = router
        self.executor = executor
        self.long_term_memory = long_term_memory
        self.private_context_transfer_approved = private_context_transfer_approved
        self.public_contact_email = public_contact_email.strip()
        self._request_context: dict[str, Any] = {}
        self._response_metadata: dict[str, Any] = {}
        self.tools = ToolRegistry()
        self.tools.register(knowledge_tool)
        if catalog_tool is not None:
            self.tools.register(catalog_tool)

    def set_request_context(self, context: dict | None) -> None:
        self._request_context = dict(context or {})

    def clear_history(self) -> None:
        self._schedule_clear()

    def clear_peer_history(self) -> None:
        self._schedule_clear()

    def consume_response_metadata(self) -> dict[str, Any]:
        metadata = dict(self._response_metadata)
        self._response_metadata.clear()
        return metadata

    def _schedule_clear(self) -> None:
        try:
            asyncio.get_running_loop().create_task(self.memory.clear(self.session_key))
        except RuntimeError:
            asyncio.run(self.memory.clear(self.session_key))

    def _language(self) -> str:
        language = str(self._request_context.get("language") or "en").lower()
        return language if language in {"zh", "en", "de"} else "en"

    async def _progress(self, stage: str) -> None:
        """Send a safe progress milestone without affecting answer reliability."""
        callback = self._request_context.get("_progress_callback")
        if callback is None:
            return
        try:
            result = callback(stage)
            if inspect.isawaitable(result):
                await result
        except Exception:
            # Browser disconnects must never discard a completed RAG answer.
            return

    @staticmethod
    def _text(language: str, key: str, **values: str) -> str:
        messages = {
            "zh": {
                "greeting": "你好！你是在了解产品，还是遇到了使用问题？可以直接告诉我型号或你的需求。",
                "capability": "当然可以聊天。关于产品，我可以帮你查规格、接口、兼容性和使用方法，也可以一起排查安装或售后问题。你可以直接说产品型号，或者先告诉我你想解决什么问题。",
                "thanks": "不客气。你可以继续问这个产品，换一个型号，或者描述遇到的使用问题。",
                "preference": "明白，在当前对话中我会记住你喜欢{color}。你想找路由器、配件，还是其他类型的产品？",
                "preference_recall": "你在当前对话中说过喜欢{color}。",
                "preference_unknown": "当前对话里还没有记录你的颜色偏好。你可以直接告诉我，例如“我喜欢黑色”。",
                "catalog_empty": "目前知识库里还没有整理出可展示的完整产品目录。你可以告诉我产品型号，我先帮你查询已有的产品资料。",
                "catalog_intro": "目前知识库中可以查询这些公开产品资料：\n{products}\n\n你可以直接问某个型号的参数、兼容性、使用方法或售后问题。",
                "contact": "我已经记录了你的人工客服请求。你也可以发送邮件到 {email} 联系客服；提交时请附上产品型号和问题描述，处理会更快。",
                "contact_without_email": "我已经记录了你的人工客服请求，客服人员可以看到当前问题和会话内容。你也可以继续补充产品型号和具体情况。",
                "clarify_model": "请告诉我具体的产品型号，并简要说明您想查询的内容或遇到的故障。",
                "clarify_intent": "请补充产品型号，并说明您要咨询产品参数、安装使用，还是售后故障。",
                "clarify_symptom": "我理解您要维修 {product}。请告诉我具体表现，例如无法上网、频繁掉线、无法开机、指示灯异常或频繁重启，我再按故障现象继续排查。",
                "commercial": "可以，我已记录你的询价并转交人工处理。请补充产品型号、采购数量和交付地区；价格、库存与交期以销售确认结果为准。",
                "order_status": "我目前不能查询实时订单或物流状态，已转交人工客服。客服接手后请提供订单编号核对；请不要在公开聊天里发送完整收货地址。",
                "payment_invoice": "付款方式和发票要求可能因订单与地区而异，我已记录问题并转交人工处理。你可以补充采购地区以及需要的发票类型。",
                "shipping_policy": "运费和配送方式要按收货地区、产品及数量核实，我已记录问题并转交人工处理。请补充产品型号、数量和收货国家或地区。",
                "returns_refunds": "退换货或退款需要核对订单和适用政策，我已转交人工客服处理。请在客服接手后提供订单编号和遇到的问题。",
                "product_safety": "如果设备正在冒烟、着火或有触电风险，请先停止使用并远离设备；我已转交人工客服。有人身危险时请联系当地应急服务。",
                "company_identity": "我是这个页面的产品与售后咨询助手，可以根据已收录的公开资料帮你查询。销售主体或品牌授权关系需要由人工客服提供可核实的信息；我不会代替企业作资质承诺。",
                "service_process": "可以先告诉我产品型号和具体故障，我会查公开的排障资料。若需要维修、退换或核对保修资格，我可以转交人工客服；届时请准备购买凭证和故障描述。",
                "warranty_unknown": "保修期限和范围通常要按型号、购买地区及凭证核对。目前没有检索到足以确认条款的公开资料。请告诉我完整型号和购买地区，或要求转人工核实。",
                "handoff": "这个问题需要人工客服处理，我已记录当前问题和会话上下文，请等待客服人员接手。",
                "temporary": "知识库服务暂时不可用，我已将问题转交人工客服，请稍后查看回复。",
                "no_evidence": "这点我暂时没查到可靠资料，先不猜。告诉我完整型号和你想解决的具体问题，我可以继续查对应资料。",
                "no_model_documents": "我知道你问的是 {product}，但目前没有找到这个型号可用的公开资料，不能准确回答。你可以补充硬件版本，或让我转人工继续核实。",
                "no_evidence_handoff": "连续检索仍没有找到可靠依据，我已转交人工客服，避免给出不准确的答案。",
                "model_evidence_gap": "我理解您在问 {product} 的“{question}”。目前知识库中与该型号匹配的是：{documents}，但这些资料没有提供可核实的答案。要继续准确回答，需要补充该型号的{needed_document}或确认硬件版本；也可以要求转人工继续处理。",
                "manual_content_gap": "我已经识别到您咨询的是 {product}，并定位到对应的用户手册：{documents}。但当前版本没有提供“{question}”的独立操作路径；我不会把其他型号的设置步骤套用过来。请提供路由器的固件版本或管理后台截图，我可以继续帮您确认；也可以转人工客服处理。",
                "manual_intro": "目前知识库中有这些公开用户手册：\n{manuals}",
                "manual_empty": "目前知识库中还没有公开用户手册。",
                "manual_mismatch": "知识库中有公开用户手册，但当前手册适用于 {manual_models}，并不是 {product}。{product} 目前只有 {product_documents}；如果要查询具体设置步骤，还需要补充该型号的用户手册。",
                "remembered": "已按您的明确要求保存这条用户信息。",
                "remember_login": "长期保存用户信息需要先登录客户账号；当前对话内容仍会作为短期会话上下文使用。",
                "remember_failed": "这条信息暂时无法保存，请稍后重试。",
                "memory_recall": "我记得你明确保存过：\n{memories}",
                "memory_empty": "你还没有明确要求我长期保存这类信息。你可以说“请记住：……”来保存。",
                "scope": "我可以陪你聊聊，也能帮你查产品资料和排查使用问题。你现在最想解决什么？",
            },
            "en": {
                "greeting": "Hello. I can help with product specifications, compatibility, setup, and after-sales troubleshooting. Please share the product model and your question.",
                "capability": "Of course, we can chat. I can look up product specifications, ports, compatibility, and setup instructions, or help troubleshoot an after-sales issue. Tell me the model or what you are trying to solve.",
                "thanks": "You're welcome. You can keep asking about this product, switch to another model, or describe a setup or support issue.",
                "preference": "Got it. In this conversation I will remember that you prefer {color}. Are you looking for a router, an accessory, or another product type?",
                "preference_recall": "You said that you prefer {color} in this conversation.",
                "preference_unknown": "You have not shared a color preference in this conversation yet. You can say, for example, 'I prefer black.'",
                "catalog_empty": "The knowledge base does not yet contain a complete product catalog. Tell me a product model and I will check the available public material.",
                "catalog_intro": "These public product materials are currently available:\n{products}\n\nAsk about a model's specifications, compatibility, setup, or after-sales support.",
                "contact": "I recorded your request for human support. You can also email {email}; include the product model and issue description for faster handling.",
                "contact_without_email": "I recorded your request for human support. The support team can see your current question and conversation. You can add the model and a brief description of the issue here.",
                "clarify_model": "Please provide the exact product model and briefly describe what you want to know or what is not working.",
                "clarify_intent": "Please provide the product model and say whether you need specifications, setup help, or troubleshooting.",
                "clarify_symptom": "I understand that {product} needs repair. Tell me the specific symptom, such as no internet, frequent disconnects, no power, unusual LEDs, or repeated restarts, and I will continue from there.",
                "commercial": "I have recorded your inquiry for human follow-up. Please share the model, quantity, and delivery region; sales must confirm price, availability, and lead time.",
                "order_status": "I cannot check live order or shipment status here, so I have passed your request to human support. Please provide the order number when an agent takes over, and avoid posting your full delivery address in this chat.",
                "payment_invoice": "Payment and invoice options depend on the order and region. I have recorded your question for human follow-up; you can add the purchasing region and invoice type you need.",
                "shipping_policy": "Shipping cost and options depend on the destination, product, and quantity. I have recorded this for human follow-up; please share the model, quantity, and destination country or region.",
                "returns_refunds": "A return or refund requires an order and policy review. I have passed your request to human support; please provide the order number when an agent takes over.",
                "product_safety": "If the device is smoking, burning, or poses an electric shock risk, stop using it and keep away. I have passed this to human support. Contact local emergency services if anyone is in danger.",
                "company_identity": "I am this site's product and after-sales assistant and can search the public materials available here. A human agent can provide verifiable details about the seller or brand authorization; I cannot make that claim for the business.",
                "service_process": "Tell me the model and what is happening, and I can check the public troubleshooting materials first. For repair, returns, or warranty eligibility, I can pass the case to a human agent; please have proof of purchase and a description of the issue ready.",
                "warranty_unknown": "Warranty coverage can depend on the model, purchase region, and proof of purchase. I could not find a public source that confirms the terms. Please share the full model and purchase region, or ask for a human agent to verify them.",
                "handoff": "This request needs a human support agent. I have recorded the question and conversation context for follow-up.",
                "temporary": "The knowledge service is temporarily unavailable. I have passed the request to a human support agent.",
                "no_evidence": "I could not find a reliable source for that yet, so I will not guess. Share the full model and the specific issue, and I can check the matching material.",
                "no_model_documents": "I understand you mean {product}, but I could not find usable public material for that model. Share the hardware version, or ask me to pass this to a human agent for verification.",
                "no_evidence_handoff": "Repeated searches found no reliable evidence, so I have passed the request to a human agent instead of guessing.",
                "model_evidence_gap": "I understand that you are asking about “{question}” for {product}. The matching material is {documents}, but it does not contain a verifiable answer. Please add the model's {needed_document} or confirm its hardware version, or ask for a human agent.",
                "manual_content_gap": "I identified the product as {product} and found its user guide: {documents}. This version does not give a separate procedure for “{question}”, so I will not apply steps from another model. Please share the firmware version or an admin-page screenshot so I can check further, or request a human agent.",
                "manual_intro": "These public user manuals are currently available:\n{manuals}",
                "manual_empty": "There are no public user manuals in the knowledge base yet.",
                "manual_mismatch": "A public manual is available for {manual_models}, but it is not a {product} manual. For {product}, the knowledge base currently has only {product_documents}; its user manual is still needed for exact setup steps.",
                "remembered": "I saved that user information at your explicit request.",
                "remember_login": "Please sign in before saving long-term user information. This conversation will still be used as short-term context.",
                "remember_failed": "I could not save that information right now. Please try again later.",
                "memory_recall": "You explicitly asked me to remember:\n{memories}",
                "memory_empty": "You have not explicitly asked me to save this kind of information yet. Say 'Remember that ...' to save it.",
                "scope": "We can chat, and I can also help with product information or a support issue. What would you like to sort out?",
            },
            "de": {
                "greeting": "Hallo. Ich helfe bei Produktspezifikationen, Kompatibilität, Einrichtung und Fehlerbehebung. Bitte nennen Sie Modell und Frage.",
                "capability": "Natürlich können wir uns unterhalten. Ich helfe bei Spezifikationen, Anschlüssen, Kompatibilität, Einrichtung und Kundendienst. Nennen Sie das Modell oder beschreiben Sie Ihr Ziel.",
                "thanks": "Gern. Sie können weiter zu diesem Produkt fragen, ein anderes Modell nennen oder ein Problem beschreiben.",
                "preference": "Verstanden. In diesem Gespräch merke ich mir, dass Sie {color} bevorzugen. Suchen Sie einen Router, Zubehör oder eine andere Produktart?",
                "preference_recall": "Sie haben in diesem Gespräch gesagt, dass Sie {color} bevorzugen.",
                "preference_unknown": "In diesem Gespräch wurde noch keine Farbpräferenz genannt.",
                "catalog_empty": "Die Wissensbasis enthält noch keinen vollständigen Produktkatalog. Nennen Sie ein Modell, dann prüfe ich die verfügbaren öffentlichen Unterlagen.",
                "catalog_intro": "Derzeit sind diese öffentlichen Produktunterlagen verfügbar:\n{products}\n\nFragen Sie zu Spezifikationen, Kompatibilität, Einrichtung oder Kundendienst.",
                "contact": "Ihre Anfrage an einen Mitarbeiter wurde erfasst. Sie können auch an {email} schreiben; Modell und Fehlerbeschreibung beschleunigen die Bearbeitung.",
                "contact_without_email": "Ihre Anfrage an einen Mitarbeiter wurde erfasst. Das Supportteam kann Ihre aktuelle Frage und den Gesprächsverlauf sehen. Sie können Modell und Problem noch ergänzen.",
                "clarify_model": "Bitte nennen Sie das genaue Produktmodell und beschreiben Sie kurz Ihre Frage oder den Fehler.",
                "clarify_intent": "Bitte nennen Sie das Produktmodell und ob es um Spezifikationen, Einrichtung oder Fehlerbehebung geht.",
                "clarify_symptom": "Ich verstehe, dass {product} repariert werden soll. Bitte beschreiben Sie das genaue Symptom, etwa kein Internet, Abbrüche, keine Stromversorgung, auffällige LEDs oder Neustarts.",
                "commercial": "Ich habe Ihre Anfrage für die Bearbeitung durch einen Mitarbeiter erfasst. Bitte ergänzen Sie Modell, Menge und Lieferregion; Preis, Verfügbarkeit und Lieferzeit muss der Vertrieb bestätigen.",
                "order_status": "Ich kann den aktuellen Bestell- oder Versandstatus hier nicht abrufen und habe Ihre Anfrage an den Support weitergegeben. Bitte nennen Sie die Bestellnummer, wenn ein Mitarbeiter übernimmt; veröffentlichen Sie hier keine vollständige Lieferadresse.",
                "payment_invoice": "Zahlungs- und Rechnungsoptionen hängen von Bestellung und Region ab. Ich habe die Frage zur Bearbeitung durch einen Mitarbeiter erfasst; Sie können Einkaufsregion und gewünschte Rechnungsart ergänzen.",
                "shipping_policy": "Versandkosten und Optionen hängen von Zielort, Produkt und Menge ab. Ich habe die Anfrage zur Bearbeitung durch einen Mitarbeiter erfasst; bitte nennen Sie Modell, Menge und Lieferland oder Region.",
                "returns_refunds": "Rückgabe oder Erstattung erfordern die Prüfung von Bestellung und Richtlinie. Ich habe die Anfrage an einen Mitarbeiter weitergegeben; bitte nennen Sie dann die Bestellnummer.",
                "product_safety": "Wenn das Gerät raucht, brennt oder Stromschlaggefahr besteht, verwenden Sie es nicht weiter und halten Sie Abstand. Ich habe den Support informiert. Bei Gefahr für Personen kontaktieren Sie den örtlichen Notdienst.",
                "company_identity": "Ich bin der Produkt- und Kundendienstassistent dieser Seite und kann verfügbare öffentliche Unterlagen durchsuchen. Angaben zum Verkäufer oder zur Markenautorisierung muss ein Mitarbeiter belegen; ich kann sie nicht zusichern.",
                "service_process": "Nennen Sie Modell und Fehlerbild, dann prüfe ich zuerst die öffentlichen Hinweise. Für Reparatur, Rückgabe oder Garantieansprüche kann ich an einen Mitarbeiter übergeben; halten Sie Kaufnachweis und Fehlerbeschreibung bereit.",
                "warranty_unknown": "Garantiebedingungen hängen vom Modell, Kaufland und Kaufnachweis ab. Ich habe keine öffentliche Quelle gefunden, die die Bedingungen bestätigt. Bitte nennen Sie Modell und Kaufland oder lassen Sie dies von einem Mitarbeiter prüfen.",
                "handoff": "Diese Anfrage benötigt einen Servicemitarbeiter. Frage und Gesprächskontext wurden zur Bearbeitung erfasst.",
                "temporary": "Der Wissensdienst ist vorübergehend nicht verfügbar. Ich habe die Anfrage an einen Servicemitarbeiter weitergegeben.",
                "no_evidence": "Dazu habe ich noch keine verlässliche Quelle gefunden und möchte nicht raten. Nennen Sie das vollständige Modell und das konkrete Problem, dann prüfe ich passende Unterlagen.",
                "no_model_documents": "Ich verstehe, dass Sie {product} meinen. Für dieses Modell finde ich derzeit keine nutzbaren öffentlichen Unterlagen. Nennen Sie die Hardwareversion oder lassen Sie dies von einem Mitarbeiter prüfen.",
                "no_evidence_handoff": "Auch nach wiederholter Suche gibt es keinen verlässlichen Beleg. Die Anfrage wurde deshalb an einen Mitarbeiter weitergegeben.",
                "model_evidence_gap": "Ich verstehe Ihre Frage „{question}“ zu {product}. Passend ist {documents} vorhanden, aber ohne belegbare Antwort. Bitte ergänzen Sie {needed_document} für dieses Modell oder bestätigen Sie die Hardwareversion.",
                "manual_content_gap": "Ich habe {product} erkannt und das zugehörige Handbuch gefunden: {documents}. Diese Version enthält keinen eigenen Ablauf für „{question}“. Daher übertrage ich keine Schritte eines anderen Modells. Bitte senden Sie Firmware-Version oder einen Screenshot der Verwaltungsoberfläche, oder fordern Sie einen Mitarbeiter an.",
                "manual_intro": "Diese öffentlichen Benutzerhandbücher sind verfügbar:\n{manuals}",
                "manual_empty": "Derzeit sind keine öffentlichen Benutzerhandbücher verfügbar.",
                "manual_mismatch": "Ein öffentliches Handbuch ist für {manual_models} vorhanden, jedoch nicht für {product}. Zu {product} ist derzeit nur {product_documents} vorhanden; für genaue Schritte fehlt das Benutzerhandbuch.",
                "remembered": "Diese Benutzerinformation wurde auf Ihre ausdrückliche Bitte gespeichert.",
                "remember_login": "Für dauerhaft gespeicherte Benutzerinformationen müssen Sie sich anmelden. Das aktuelle Gespräch bleibt als Kurzzeitkontext erhalten.",
                "remember_failed": "Die Information konnte derzeit nicht gespeichert werden. Bitte versuchen Sie es später erneut.",
                "memory_recall": "Sie haben mich ausdrücklich gebeten, Folgendes zu speichern:\n{memories}",
                "memory_empty": "Sie haben mich noch nicht gebeten, solche Informationen dauerhaft zu speichern.",
                "scope": "Wir können uns unterhalten, und ich helfe auch bei Produktfragen oder Problemen nach dem Kauf. Wobei kann ich Ihnen helfen?",
            },
        }
        return messages[language][key].format(**values)

    async def _handoff(
        self, user_message: str, context: ConversationContext,
        decision: RouteDecision, reason: str,
    ) -> None:
        if (context.state.get("handoff_required")
                and context.state.get("handoff_id")
                and context.state.get("handoff_reason") == reason):
            return
        record = {
            "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "status": "pending",
            "reason": reason,
            "intent": decision.intent,
            "product_model": decision.product_model,
            "question": user_message[:4000],
            "summary": context.summary[:6000],
            "recent_turns": list(context.recent_turns)[-5:],
            "tenant_id": str(self._request_context.get("tenant_id") or ""),
            "account_id": str(self._request_context.get("account_id") or ""),
            "conversation_id": str(self._request_context.get("conversation_id") or self.session_key),
            "request_id": str(self._request_context.get("request_id") or ""),
        }
        record.update({
            "customer_id": str(self._request_context.get("customer_id") or ""),
            "session_key": self.session_key,
        })
        created = await self.memory.enqueue_handoff(record)
        await self.memory.update_state(
            self.session_key, handoff_required=True, handoff_reason=reason,
            handoff_id=created["handoff_id"],
        )

    async def _finish(self, user: str, answer: str, language: str) -> str:
        answer = CustomerDataGuard.sanitize_response(str(answer or "").strip(), language)
        if self._TOOL_MARKER.search(answer):
            answer = self._text(language, "scope")
        await self.memory.append_turn(self.session_key, user, answer)
        return answer

    async def _catalog_answer(
        self, language: str, query: str = "", context: ConversationContext | None = None,
    ) -> str:
        if self.catalog_tool is not None:
            catalog_query = query
            remembered_color = str((context.state if context else {}).get(
                "color_preference", ""
            )).strip()
            if remembered_color and remembered_color.casefold() not in catalog_query.casefold():
                catalog_query = f"{catalog_query}\nPreferred color: {remembered_color}"
            try:
                payload = json.loads(await self.catalog_tool.execute(query=catalog_query))
            except Exception:
                payload = {}
            products = payload.get("products") if isinstance(payload, dict) else None
            if products:
                lines = []
                citations = []
                for product in products[:5]:
                    model = str(product.get("model") or product.get("name") or "")
                    product_type = str(product.get("type") or "")
                    colors = "、".join(str(item) for item in product.get("colors") or ())
                    features = "、".join(
                        str(item) for item in (product.get("features") or ())[:4]
                    )
                    scenarios = "、".join(
                        str(item) for item in (product.get("scenarios") or ())[:4]
                    )
                    if language == "zh":
                        lines.append(
                            f"- **{model}**（{product_type}；颜色：{colors or '未标注'}）\n"
                            f"  适用场景：{scenarios or '未标注'}\n  主要功能：{features or '未标注'}"
                        )
                    else:
                        lines.append(
                            f"- **{model}** ({product_type}; colors: {colors or 'not listed'})\n"
                            f"  Scenarios: {scenarios or 'not listed'}\n"
                            f"  Features: {features or 'not listed'}"
                        )
                    if product.get("source_document_id"):
                        citations.append({
                            "document_id": product["source_document_id"],
                            "version": int(product.get("source_version") or 1),
                            "title": str(product.get("source_title") or "产品资料")[:200],
                            "page_number": product.get("source_page"),
                            "location": "",
                            "excerpt": str(product.get("source_excerpt") or "")[:600],
                        })
                self._response_metadata["citations"] = citations[:5]
                intro = {
                    "zh": "根据已整理的公开产品目录，我推荐：",
                    "en": "Based on the public product catalog, I recommend:",
                    "de": "Auf Grundlage des öffentlichen Produktkatalogs empfehle ich:",
                }[language]
                return intro + "\n\n" + "\n".join(lines)
        titles = await asyncio.to_thread(self.knowledge_tool.public_document_titles, 12)
        if not titles:
            return self._text(language, "catalog_empty")
        products = "\n".join(f"- {title}" for title in titles[:8])
        return self._text(language, "catalog_intro", products=products)

    async def _manual_answer(self, language: str, product: str = "") -> str:
        manuals = await asyncio.to_thread(self.knowledge_tool.public_manuals, 12)
        if not manuals:
            return self._text(language, "manual_empty")
        normalized_product = self.knowledge_tool._normalized_model(product)
        matching_manuals = [
            item for item in manuals
            if not normalized_product or any(
                normalized_product == self.knowledge_tool._normalized_model(model)
                or normalized_product in self.knowledge_tool._normalized_model(model)
                or self.knowledge_tool._normalized_model(model) in normalized_product
                for model in item.get("product_models") or ()
            )
        ]
        if product and not matching_manuals:
            model_names = list(dict.fromkeys(
                str(model) for item in manuals
                for model in item.get("product_models") or () if model
            ))
            product_documents = await asyncio.to_thread(
                self.knowledge_tool.public_documents_for_model, product,
            )
            source_rows = list(manuals[:2]) + list(product_documents[:2])
            self._response_metadata["citations"] = [{
                "document_id": item["document_id"],
                "version": int(item.get("version") or 1),
                "title": str(item.get("title") or "产品资料")[:200],
                "location": "",
                "page_number": None,
                "excerpt": "知识库文档目录中的公开资料",
            } for item in source_rows]
            return self._text(
                language, "manual_mismatch", product=product,
                manual_models="、".join(model_names) or "其他型号",
                product_documents="、".join(
                    str(item.get("title") or "产品资料")
                    for item in product_documents[:3]
                ) or "产品资料",
            )
        manuals = matching_manuals or manuals
        lines = []
        citations = []
        for item in manuals[:8]:
            models = "、".join(item.get("product_models") or ())
            version = str(item.get("document_version") or "").strip()
            suffix = "；".join(part for part in (
                f"型号：{models}" if models else "",
                f"版本：{version}" if version else "",
            ) if part)
            lines.append(f"- {item['title']}" + (f"（{suffix}）" if suffix else ""))
            citations.append({
                "document_id": item["document_id"],
                "version": int(item.get("version") or 1),
                "title": item["title"],
                "location": "",
                "page_number": None,
                "excerpt": (f"公开用户手册；{suffix}" if suffix else "公开用户手册"),
            })
        self._response_metadata["citations"] = citations[:5]
        return self._text(language, "manual_intro", manuals="\n".join(lines))

    def _model_gap_answer(
        self, language: str, user_message: str, product: str,
        matched_documents: list[dict[str, Any]],
        missing_document_types: list[str] | None = None,
    ) -> str:
        titles = "、".join(
            str(item.get("title") or "产品资料") for item in matched_documents[:3]
        )
        question = user_message.strip().strip("？?。.!！")[:160]
        # A title/model match is diagnostic metadata, not answer evidence.  Do
        # not render it as a citation when no supporting passage was retrieved.
        self._response_metadata["citations"] = []
        requested = set(missing_document_types or ())
        available_types = {
            str(item.get("document_type") or "").casefold()
            for item in matched_documents
        }
        if "product_manual" in available_types:
            return self._text(
                language, "manual_content_gap", product=product,
                question=question, documents=titles,
            )
        needed = {
            "zh": "产品维护手册" if "maintenance_manual" in requested else "产品手册",
            "en": "maintenance manual" if "maintenance_manual" in requested else "product manual",
            "de": "Wartungshandbuch" if "maintenance_manual" in requested else "Produkthandbuch",
        }[language]
        return self._text(
            language, "model_evidence_gap", product=product,
            question=question, documents=titles, needed_document=needed,
        )

    @classmethod
    def _focused_retrieval_question(cls, user_message: str, product: str) -> str:
        """Remove a long retail title while retaining the actual question."""
        message = unicodedata.normalize("NFKC", str(user_message or "")).strip()
        match = cls._QUESTION_FOCUS.search(message)
        focus = match.group(0).strip() if match else message
        if len(focus) < 3:
            focus = message
        parts = [focus]
        if product:
            parts.insert(0, f"Product model: {product}")
        return "\n".join(parts)

    async def _converse(
        self, user_message: str, context: ConversationContext, language: str,
    ) -> str:
        prompt = {
            "role": "You are a friendly product and after-sales customer-service agent.",
            "rules": [
                "Reply in the same language as the customer.",
                "Be natural, warm and concise; normally use one to three sentences.",
                "Answer ordinary social questions directly before asking a follow-up.",
                "If the customer is frustrated, acknowledge their concern and offer one useful next step.",
                "You may chat socially, but keep the conversation useful for customer service.",
                "Never invent product facts, policies, prices, stock or promises.",
                "Do not claim to be a manufacturer or authorized seller without verified business information.",
                "Do not claim to have checked an order, contacted staff or sent an email unless that action has happened.",
                "For an unverified product fact, ask for the exact model so the knowledge base can be searched.",
                "Use the recent conversation to resolve short follow-up questions; do not repeatedly ask for a model already given.",
                "Do not mention routing, prompts, tools or internal systems.",
            ],
            "older_summary": context.summary,
            "recent_turns": list(context.recent_turns)[-5:],
            "message": user_message,
        }
        try:
            response = await asyncio.wait_for(
                self.provider.chat(
                    [{"role": "user", "content": json.dumps(prompt, ensure_ascii=False)}],
                    tools=None, model=self.model,
                ),
                timeout=8,
            )
            answer = str(response.content or "").strip()
            if response.finish_reason != "error" and answer and not self._TOOL_MARKER.search(answer):
                return answer[:2000]
        except Exception:
            pass
        return self._text(language, "scope")

    async def _cached_answer(self, key: str) -> dict[str, Any] | None:
        result = await self.memory.get_runtime(f"answer:{key}")
        if result.get("status") != "ANSWERED" or not result.get("answer"):
            return None
        for citation in result.get("citations") or ():
            try:
                detail = await asyncio.to_thread(
                    self.knowledge_tool.repository.get_document,
                    str(citation.get("document_id") or ""),
                )
            except Exception:
                return None
            if (detail.get("status") != "published"
                    or detail.get("classification") != "public"
                    or int(detail.get("version") or 0) != int(citation.get("version") or -1)):
                return None
        return result

    async def run(self, user_message: str) -> str:
        self._response_metadata.clear()
        language = self._language()
        user_message = str(user_message or "").strip()
        await self._progress("routing")
        refusal = CustomerDataGuard.inspect_request(user_message, language)
        if refusal:
            return await self._finish(user_message, refusal, language)

        context = await self.memory.load(self.session_key)
        tenant_id = str(self._request_context.get("tenant_id") or "")
        account_id = str(self._request_context.get("account_id") or "")
        if self.long_term_memory is not None and tenant_id and account_id:
            recalled = await self.long_term_memory.recall(tenant_id, account_id, user_message)
            context = replace(context, long_term_memories=recalled)

        if self._PERSONAL_MEMORY_QUERY.search(user_message):
            if context.long_term_memories:
                memories = "\n".join(
                    f"- {item}" for item in context.long_term_memories[:3]
                )
                answer = self._text(language, "memory_recall", memories=memories)
            else:
                answer = self._text(language, "memory_empty")
            return await self._finish(user_message, answer, language)

        decision = await self.router.route(user_message, context)
        if decision.product_model:
            canonical_model = await asyncio.to_thread(
                self.knowledge_tool.canonical_product_model,
                decision.product_model,
            )
            if canonical_model and canonical_model != decision.product_model:
                decision = replace(decision, product_model=canonical_model)
        color_match = self._COLOR_PREFERENCE.search(user_message)
        color_preference = color_match.group(0) if color_match else ""
        state_updates: dict[str, Any] = {
            "last_intent": decision.intent,
            "last_route_confidence": decision.confidence,
            "missing_fields": list(decision.missing_fields),
        }
        if decision.intent not in {"product_consultation", "after_sales", "service_policy"}:
            # A social or control turn breaks a run of consecutive failed
            # knowledge lookups.  Otherwise an unrelated preference message
            # can make the next product question escalate immediately.
            state_updates["no_evidence_count"] = 0
        if decision.product_model:
            state_updates["product_model"] = decision.product_model
        if decision.reason == "preference_rule" and color_preference:
            state_updates["color_preference"] = color_preference
        await self.memory.update_state(self.session_key, **state_updates)

        if decision.intent == "greeting":
            return await self._finish(user_message, self._text(language, "greeting"), language)

        if decision.intent == "catalog_overview":
            if decision.reason == "manual_catalog_rule":
                return await self._finish(
                    user_message,
                    await self._manual_answer(language, decision.product_model),
                    language,
                )
            return await self._finish(
                user_message,
                await self._catalog_answer(language, user_message, context), language,
            )

        if decision.intent == "conversation":
            if decision.reason == "capability_rule":
                answer = self._text(language, "capability")
            elif decision.reason == "thanks_rule":
                answer = self._text(language, "thanks")
            elif decision.reason == "preference_rule":
                if self.catalog_tool is not None and self._CONCEPTUAL_PRODUCT_QUERY.search(user_message):
                    answer = await self._catalog_answer(language, user_message, context)
                else:
                    answer = self._text(
                        language, "preference",
                        color=(color_preference or str(
                            context.state.get("color_preference") or "这种颜色"
                        )),
                    )
            elif decision.reason == "preference_recall_rule":
                remembered_color = str(context.state.get("color_preference") or "").strip()
                answer = self._text(
                    language,
                    "preference_recall" if remembered_color else "preference_unknown",
                    **({"color": remembered_color} if remembered_color else {}),
                )
            elif decision.reason == "company_identity_rule":
                answer = self._text(language, "company_identity")
            elif decision.reason == "service_process_rule":
                answer = self._text(language, "service_process")
            else:
                answer = await self._converse(user_message, context, language)
            return await self._finish(user_message, answer, language)

        if decision.intent == "memory_update":
            conversation_id = str(self._request_context.get("conversation_id") or "")
            request_id = str(self._request_context.get("request_id") or "")
            if not (self.long_term_memory and tenant_id and account_id):
                answer = self._text(language, "remember_login")
            else:
                stored = await self.long_term_memory.remember_explicit(
                    tenant_id, account_id, conversation_id, request_id, decision.reason,
                )
                answer = self._text(language, "remembered" if stored else "remember_failed")
            return await self._finish(user_message, answer, language)

        if decision.intent in {"commercial_inquiry", "human_handoff"}:
            await self._handoff(user_message, context, decision, decision.reason or decision.intent)
            if decision.reason == "contact_request":
                answer = (self._text(language, "contact", email=self.public_contact_email)
                          if self.public_contact_email else
                          self._text(language, "contact_without_email"))
            elif decision.reason in {
                    "order_status", "payment_invoice", "shipping_policy",
                    "returns_refunds", "product_safety"}:
                answer = self._text(language, decision.reason)
            else:
                key = "commercial" if decision.intent == "commercial_inquiry" else "handoff"
                answer = self._text(language, key)
            return await self._finish(user_message, answer, language)

        if (self.catalog_tool is not None and not decision.product_model
                and self._CONCEPTUAL_PRODUCT_QUERY.search(user_message)):
            answer = await self._catalog_answer(language, user_message, context)
            if answer != self._text(language, "catalog_empty"):
                return await self._finish(user_message, answer, language)

        if decision.needs_clarification:
            if "product_model" in decision.missing_fields:
                answer = self._text(language, "clarify_model")
            elif "issue_symptom" in decision.missing_fields:
                answer = self._text(
                    language, "clarify_symptom",
                    product=(decision.product_model or str(
                        context.state.get("product_model") or "该产品"
                    )),
                )
            else:
                answer = self._text(language, "clarify_intent")
            return await self._finish(user_message, answer, language)

        if decision.intent not in {"product_consultation", "after_sales", "service_policy"}:
            return await self._finish(user_message, self._text(language, "scope"), language)

        await self._progress("searching")

        product = decision.product_model or str(context.state.get("product_model") or "")
        conceptual = bool(self._CONCEPTUAL_PRODUCT_QUERY.search(user_message))
        procedural = bool(self._PROCEDURE_QUERY.search(user_message))
        if decision.intent == "service_policy":
            document_types = ("warranty_policy",)
        elif decision.intent == "after_sales":
            document_types = ("maintenance_manual", "product_manual")
        elif procedural:
            document_types = ("product_manual",)
        else:
            document_types = ("product_info", "product_manual")
        fast = bool(product and len(user_message) <= 120)
        query_parts = [self._focused_retrieval_question(user_message, product)]
        if conceptual and product:
            query_parts.append(
                "检索重点：产品定位、主要功能、适用环境、覆盖范围、连接设备、"
                "家庭网络；product positioning, key features, coverage, connected devices."
            )
        if context.recent_turns and not fast:
            recent = context.recent_turns[-2:]
            query_parts.append("Recent conversation: " + " | ".join(
                f"Customer: {turn['user'][:160]} Support: {turn['assistant'][:220]}"
                for turn in recent
            ))
        if context.summary and not fast:
            query_parts.append(f"Conversation context: {context.summary[:1200]}")
        if self.private_context_transfer_approved and context.long_term_memories:
            query_parts.append(
                "Confirmed customer context: "
                + " | ".join(context.long_term_memories)[:800]
            )
        query = "\n".join(query_parts)[:500]
        cache_key = hashlib.sha256(
            f"metadata-v3\n{language}\n{','.join(document_types)}\n{query}".encode("utf-8")
        ).hexdigest()
        result = await self._cached_answer(cache_key)
        try:
            if result is None:
                await self._progress("generating")
                result = await self.executor.execute_json(
                    self.knowledge_tool.name,
                    lambda: self.knowledge_tool.execute(
                        query=query, fast=fast, product_model=product,
                        document_types=document_types,
                    ),
                )
        except (CircuitOpen, RuntimeError):
            await self._handoff(user_message, context, decision, "knowledge_service_unavailable")
            return await self._finish(user_message, self._text(language, "temporary"), language)

        if result.get("status") == "ANSWERED" and str(result.get("answer") or "").strip():
            raw_answer = str(result["answer"])
            if self._TOOL_MARKER.search(raw_answer):
                await self._handoff(
                    user_message, context, decision, "malformed_model_output",
                )
                return await self._finish(
                    user_message, self._text(language, "temporary"), language,
                )
            await self.memory.update_state(
                self.session_key, no_evidence_count=0,
                last_no_evidence_key="", handoff_required=False,
            )
            await self.memory.set_runtime(
                f"answer:{cache_key}", result,
                self.memory.settings.response_cache_ttl_seconds,
            )
            self._response_metadata["citations"] = list(
                result.get("citations") or ()
            )[:5]
            return await self._finish(user_message, raw_answer, language)

        if result.get("status") in {"TEMPORARILY_UNAVAILABLE", "GENERATION_UNAVAILABLE"}:
            await self._handoff(
                user_message, context, decision, "knowledge_service_unavailable",
            )
            return await self._finish(
                user_message, self._text(language, "temporary"), language,
            )

        normalized_question = re.sub(r"\s+", "", user_message).casefold()
        no_evidence_key = hashlib.sha256(
            f"{decision.intent}|{product.casefold()}|{normalized_question}".encode("utf-8")
        ).hexdigest()[:20]
        previous_key = str(context.state.get("last_no_evidence_key") or "")
        no_evidence_count = (
            int(context.state.get("no_evidence_count") or 0) + 1
            if previous_key == no_evidence_key else 1
        )
        await self.memory.update_state(
            self.session_key, no_evidence_count=no_evidence_count,
            last_no_evidence_key=no_evidence_key,
        )
        if no_evidence_count >= 2:
            await self._handoff(user_message, context, decision, "repeated_no_evidence")
            answer = self._text(language, "no_evidence_handoff")
        elif decision.intent == "service_policy":
            answer = self._text(language, "warranty_unknown")
        elif product and result.get("matched_documents"):
            answer = self._model_gap_answer(
                language, user_message, product,
                list(result.get("matched_documents") or ()),
                list(result.get("missing_document_types") or ()),
            )
        elif product:
            answer = self._text(language, "no_model_documents", product=product)
        else:
            answer = self._text(language, "no_evidence")
        return await self._finish(user_message, answer, language)
