import asyncio
from pathlib import Path
from types import SimpleNamespace

import main
import pytest
from agent.context import ContextBuilder
from agent.customer_security import CustomerDataGuard
from agent.tools.base import Tool
from agent.tools.customer_public import CustomerPublicCatalogTool, CustomerPublicKnowledgeTool
from bus import InboundMessage, MessageBus
from gateway import Gateway
from providers.base import LLMResponse, ToolCallRequest


def test_customer_context_uses_public_identity_locale_and_no_internal_memory(tmp_path: Path):
    (tmp_path / "customer_identity.md").write_text("PUBLIC SALES IDENTITY", encoding="utf-8")
    memory = tmp_path / "workspace" / "memory"
    memory.mkdir(parents=True)
    (memory / "MEMORY.md").write_text("INTERNAL MAILBOX SECRET", encoding="utf-8")
    context = ContextBuilder(
        workspace=str(tmp_path), identity_file="customer_identity.md",
        include_memory=False, include_workspace_details=False,
    )

    messages = context.build_messages(
        current_message="Bitte senden Sie ein Angebot.",
        request_context={"channel": "customer_portal", "language": "de"},
    )
    combined = "\n".join(message["content"] for message in messages)
    assert "PUBLIC SALES IDENTITY" in combined
    assert "Reply entirely in German" in combined
    assert "INTERNAL MAILBOX SECRET" not in combined
    assert str(tmp_path) not in combined














def test_normal_rfq_is_not_blocked():
    assert CustomerDataGuard.inspect_request(
        "Please quote 500 blue widgets, FOB Hamburg, contact buyer@example.com.", "en"
    ) is None




def test_customer_knowledge_tool_reads_only_explicitly_public_documents(tmp_path: Path):
    from trade_rag.knowledge_repository import KnowledgeRepository

    repository = KnowledgeRepository(tmp_path / "knowledge")
    repository.import_bytes(
        "public.md", b"PublicWidget uses a blue anodized housing.", classification="public"
    )
    repository.import_bytes(
        "private.md", b"PrivateMargin is forty percent.", classification="internal"
    )
    tool = CustomerPublicKnowledgeTool(repository)
    result = asyncio.run(tool.execute(query="What housing does PublicWidget use?"))
    assert "blue anodized housing" in result
    assert "PrivateMargin" not in result


def test_customer_catalog_tool_allowlists_fields_and_hides_exact_stock_and_price(monkeypatch):
    product = SimpleNamespace(
        sku="PUBLIC-1", name_en="Public Widget", name_cn="公开产品", category="widget",
        specification="Blue", unit="pcs", moq=100, inventory=9876,
        price_usd=12.34, lead_time_days=15, active=True,
    )
    import agent.business.database as database
    monkeypatch.setattr(database, "list_products", lambda **kwargs: [product])
    monkeypatch.setattr(database, "get_product_by_sku", lambda sku: product)
    monkeypatch.setattr(database, "check_inventory", lambda sku, quantity: {
        "available": True, "inventory": 9876, "moq": 100,
    })
    result = asyncio.run(CustomerPublicCatalogTool().execute(keyword="widget", quantity=500))
    payload = __import__("json").loads(result)
    row = payload["results"][0]
    assert row["requested_quantity_available"] is True
    assert row["price_status"] == "sales_confirmation_required"
    assert "inventory" not in row
    assert "price_usd" not in row
    assert "9876" not in result
    assert "12.34" not in result










def test_gateway_passes_customer_locale_as_trusted_context():
    class Agent:
        context = None

        def set_request_context(self, context):
            self.context = context

        async def run(self, content):
            return "ok"

    async def scenario():
        bus = MessageBus()
        agent = Agent()
        gateway = Gateway(bus, [], lambda _: agent)
        task = asyncio.create_task(gateway._process_inbound())
        await bus.publish_inbound(InboundMessage(
            channel="customer_portal", sender_id="anonymous:test", chat_id="socket-1",
            content="Hallo", raw={"language": "de", "conversation_id": "conversation-1"},
        ))
        outbound = await asyncio.wait_for(bus.consume_outbound(), timeout=1)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return agent, outbound

    agent, outbound = asyncio.run(scenario())
    assert agent.context["channel"] == "customer_portal"
    assert agent.context["language"] == "de"
    assert agent.context["conversation_id"] == "conversation-1"
    assert callable(agent.context["_progress_callback"])
    assert outbound.content == "ok"

def test_customer_agent_entry_uses_the_dedicated_customer_harness():
    with pytest.raises(RuntimeError, match="customer_agent_model_not_configured"):
        main.build_agent(SimpleNamespace(api_key=""), "customer_portal:anonymous:test")
    assert hasattr(main, "build_customer_agent")
