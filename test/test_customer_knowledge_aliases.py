import asyncio
import json

from agent.customer_service.harness import CustomerServiceHarness
from agent.tools.customer_public import CustomerPublicKnowledgeTool


class _Repository:
    def __init__(self):
        self.documents = {
            "info": {
                "document_id": "info", "title": "AX5400双频Super Wi-Fi 6 无线路由器",
                "version": 1, "document_type": "product_info",
                "product_models": ["TL-XDR5480"], "status": "published",
                "classification": "public",
            },
            "quick": {
                "document_id": "quick", "title": "TL-XDR5480 快速安装指南",
                "version": 1, "document_type": "product_manual",
                "product_models": ["TL-XDR5480"], "status": "published",
                "classification": "public",
            },
            "manual": {
                "document_id": "manual", "title": "TL-XDR5480 用户手册",
                "version": 1, "document_type": "maintenance_manual",
                "product_models": ["TL-XDR5480"], "status": "published",
                "classification": "public",
            },
        }

    def list_documents(self, **kwargs):
        return list(self.documents.values()), len(self.documents), None

    def get_document(self, document_id):
        return self.documents[document_id]


class _Pipeline:
    def __init__(self):
        self.request = None

    def query(self, request):
        self.request = request
        return {"status": "NO_EVIDENCE", "answer": "", "citations": []}


def test_marketing_model_alias_expands_to_all_canonical_manuals():
    tool = CustomerPublicKnowledgeTool(repository=_Repository(), pipeline=_Pipeline())

    assert tool.canonical_product_model("AX5400Super") == "TL-XDR5480"
    documents = tool.public_documents_for_model("AX5400Super")

    assert {item["document_id"] for item in documents} == {"info", "quick", "manual"}


def test_alias_scoped_after_sales_query_reaches_manuals():
    pipeline = _Pipeline()
    tool = CustomerPublicKnowledgeTool(repository=_Repository(), pipeline=pipeline)

    payload = json.loads(asyncio.run(tool.execute(
        query="如何快速配置上网", product_model="AX5400Super",
        document_types=("product_manual", "maintenance_manual"), fast=True,
    )))

    assert payload["status"] == "NO_EVIDENCE"
    assert set(pipeline.request.allowed_document_ids) == {"quick", "manual"}
    assert "missing_document_types" not in payload


def test_retrieval_question_drops_long_marketing_title():
    assert CustomerServiceHarness._focused_retrieval_question(
        "AX5400双频Super Wi-Fi 6 无线路由器有什么软件功能", "TL-XDR5480",
    ) == "Product model: TL-XDR5480\n有什么软件功能"

