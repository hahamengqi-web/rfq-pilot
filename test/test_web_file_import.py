import asyncio
import tempfile
import unittest
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from bus import InboundMessage, MessageBus
from channels.web import WebChannel
from channels.customer_portal import CustomerPortalChannel
from gateway import Gateway
from trade_rag.contracts import Actor, QueryRequest
from trade_rag.knowledge_repository import KnowledgeRepository
from trade_rag.pipeline import RagPipeline


class WebFileImportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.bus = MessageBus()
        self.channel = WebChannel(self.bus)
        self.channel._knowledge_repository = KnowledgeRepository(Path(self.temporary.name) / "knowledge")
        self.channel._app = FastAPI()
        self.channel._register_routes()
        self.client = TestClient(self.channel._app)

    def tearDown(self):
        self.client.close()
        self.temporary.cleanup()

    def test_page_exposes_expandable_import_menu_and_styles(self):
        page = self.client.get("/")
        styles = self.client.get("/static/import.css")
        self.assertEqual(200, page.status_code)
        self.assertIn('id="importToggle"', page.text)
        self.assertIn('id="readFileAction"', page.text)
        self.assertIn('id="knowledgeFileAction"', page.text)
        self.assertEqual(200, styles.status_code)

    def test_page_exposes_localized_workflow_and_shell_controls(self):
        page = self.client.get("/")
        self.assertIn('id="railToggle"', page.text)
        self.assertIn('id="workspaceToggle"', page.text)
        self.assertIn('data-i18n="workflow.title"', page.text)
        script = self.client.get("/static/app.js").text
        for key in ("workflow.foreign_trade_quote", "workflow.extract_rfq", "refreshWorkflow", "scheduleWorkflowRefresh"):
            self.assertIn(key, script)

    def test_workspace_loads_safe_markdown_before_chat_renderer(self):
        page = self.client.get("/").text
        markdown = self.client.get("/static/js/markdown.js")
        styles = self.client.get("/static/css/markdown.css")
        script = self.client.get("/static/app.js").text
        self.assertEqual(200, markdown.status_code)
        self.assertEqual(200, styles.status_code)
        self.assertLess(page.index('/static/js/markdown.js'), page.index('/static/app.js'))
        self.assertIn("window.NanoClawMarkdown?.render(text)", script)
        self.assertIn("escapeHtml", markdown.text)
        self.assertIn("safeHref", markdown.text)

    def test_workflow_drag_uses_stage_coordinates_without_default_offset_subtraction(self):
        page = self.client.get("/")
        script = self.client.get("/static/app.js").text
        styles = self.client.get("/static/app.css").text
        self.assertIn('id="workflowDragHandle"', page.text)
        self.assertIn("nanoclaw-workflow-position-v1", script)
        self.assertIn("translate3d(${position.x}px,${position.y}px,0)", script)
        self.assertNotIn("position.x - 18", script)
        self.assertIn(".workflow-panel.free-position{top:0;left:0}", styles)

    def test_operations_console_is_removed_and_email_owns_operational_cards(self):
        page = self.client.get("/")
        script = self.client.get("/static/app.js").text
        self.assertNotIn('data-view="ops"', page.text)
        self.assertNotIn('id="opsView"', page.text)
        self.assertIn('id="emailMetricPending"', page.text)
        self.assertIn('id="emailRuntimeStatus"', page.text)
        self.assertEqual(404, self.client.get("/ops").status_code)
        self.assertNotIn("/api/ops/dashboard", script)
        self.assertIn("/api/email/workflow", script)
        self.assertNotIn('id="customerPortalLink"', page.text)
        self.assertIn('href="/inquiries"', page.text)

    def test_workspace_links_to_the_dedicated_customer_portal(self):
        response = self.client.get("/api/ui/config")
        self.assertEqual(200, response.status_code)
        self.assertTrue(response.json()["customer_portal_enabled"])
        self.assertEqual("http://127.0.0.1:8766", response.json()["customer_portal_url"])

    def test_public_root_can_remain_landing_while_workspace_route_stays_available(self):
        channel = WebChannel(self.bus, root_page="landing", channel_name="public_web")
        channel._app = FastAPI()
        channel._register_routes()
        with TestClient(channel._app) as client:
            landing = client.get("/")
            workspace = client.get("/workspace")
        self.assertIn("AI WORKFLOW PROJECT MANAGEMENT", landing.text)
        self.assertNotIn('id="emailView"', landing.text)
        self.assertIn('id="emailView"', workspace.text)

    def test_customer_portal_returns_gone(self):
        response = self.client.get("/customer")
        self.assertEqual(410, response.status_code)
        self.assertEqual("customer_agent_removed", response.json()["detail"])

    def test_inquiry_workbench_is_served_with_responsive_styles(self):
        page = self.client.get("/inquiries")
        self.assertEqual(200, page.status_code)
        self.assertIn('/static/inquiries.js', page.text)
        self.assertIn('id="wa-import"', page.text)
        self.assertIn('@media(max-width:800px)', self.client.get('/static/inquiries.css').text)

    def test_read_file_returns_text_without_persisting_to_knowledge_base(self):
        response = self.client.post(
            "/api/files/read",
            content="RFQ reference only".encode(),
            headers={"X-File-Name": "quote.md", "Content-Type": "application/octet-stream"},
        )
        self.assertEqual(200, response.status_code)
        self.assertEqual("RFQ reference only", response.json()["content"])
        self.assertFalse(self.channel._knowledge_repository.manifest_path.exists())

    def test_html_scripts_are_not_returned_as_conversation_context(self):
        response = self.client.post(
            "/api/files/read",
            content=b"<style>.x{}</style><script>bad()</script><p>Safe text</p>",
            headers={"X-File-Name": "guide.html"},
        )
        self.assertEqual(200, response.status_code)
        self.assertIn("Safe text", response.json()["content"])
        self.assertNotIn("bad()", response.json()["content"])

    def test_knowledge_import_is_persistent_deduplicated_and_searchable(self):
        headers = {"X-File-Name": "shipping.md"}
        content = b"SKU-Z supports DDP shipping to Madrid."
        first = self.client.post("/api/knowledge/import", content=content, headers=headers)
        second = self.client.post("/api/knowledge/import", content=content, headers=headers)
        self.assertEqual(200, first.status_code)
        self.assertFalse(first.json()["duplicate"])
        self.assertTrue(second.json()["duplicate"])
        documents = self.channel._knowledge_repository.load_published()
        self.assertEqual(1, len(documents))
        pipeline = RagPipeline()
        pipeline.index(documents[0])
        result = pipeline.query(QueryRequest("SKU-Z DDP Madrid", Actor("web-user")))
        self.assertTrue(result["citations"])

    def test_binary_or_unsupported_file_is_rejected(self):
        response = self.client.post(
            "/api/files/read", content=b"\x00\x01", headers={"X-File-Name": "payload.exe"}
        )
        self.assertEqual(400, response.status_code)
        self.assertEqual("unsupported_file_type", response.json()["detail"])


class _Agent:
    def __init__(self):
        self.cleared = 0

    def clear_history(self):
        self.cleared += 1


class GatewayClearConversationTests(unittest.IsolatedAsyncioTestCase):
    async def test_clear_event_clears_server_history_without_model_call(self):
        bus = MessageBus()
        gateway = Gateway(bus, [], lambda _: self.fail("agent factory must not run"))
        agent = _Agent()
        gateway._agents["web:user-1"] = agent
        task = asyncio.create_task(gateway._process_inbound())
        await bus.publish_inbound(InboundMessage(
            channel="web", sender_id="user-1", chat_id="user-1", content="",
            raw={"event": "clear_conversation"},
        ))
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(1, agent.cleared)


class CustomerPortalServerTests(unittest.TestCase):
    def test_dedicated_port_app_serves_only_the_customer_entry(self):
        channel = CustomerPortalChannel(MessageBus(), host="127.0.0.1", port=8766)
        with TestClient(channel.create_app()) as client:
            self.assertEqual(200, client.get("/").status_code)
            self.assertEqual(200, client.get("/customer").status_code)
            self.assertEqual("customer_portal", client.get("/health").json()["service"])
            self.assertEqual(404, client.get("/ops").status_code)


if __name__ == "__main__":
    unittest.main()
