import asyncio
import sys
import types


try:
    import anthropic  # noqa: F401
except ModuleNotFoundError:
    anthropic_module = types.ModuleType("anthropic")
    anthropic_module.AsyncAnthropic = object
    sys.modules["anthropic"] = anthropic_module

try:
    import chromadb  # noqa: F401
except ModuleNotFoundError:
    sys.modules["chromadb"] = types.ModuleType("chromadb")

from mcp.knowledge_base import KnowledgeBase
from mcp.tool_manager import MCPToolManager, ToolResult


def test_knowledge_search_returns_stable_document_ids():
    class Collection:
        def query(self, **kwargs):
            return {
                "ids": [["doc-1"]],
                "documents": [["退款通常原路返回"]],
                "metadatas": [[{"title": "退款政策", "chunk_index": 0}]],
                "distances": [[0.1]],
            }

    kb = KnowledgeBase.__new__(KnowledgeBase)
    kb._collection = Collection()

    assert kb.search("退款", 5)[0]["id"] == "doc-1"


def test_multi_query_merge_deduplicates_by_id_and_keeps_best_score():
    async def scenario():
        manager = MCPToolManager.__new__(MCPToolManager)

        async def rewrite_query(query, n=3):
            return [query, "退款规则"]

        responses = [
            ToolResult(
                success=True,
                tool_name="knowledge_search",
                data=[{"id": "doc-1", "title": "退款政策", "content": "原路返回", "score": 0.72}],
            ),
            ToolResult(
                success=True,
                tool_name="knowledge_search",
                data=[
                    {"id": "doc-1", "title": "退款政策", "content": "原路返回", "score": 0.93},
                    {"id": "doc-2", "title": "到账时间", "content": "三到五天", "score": 0.81},
                ],
            ),
        ]

        async def call(*args, **kwargs):
            return responses.pop(0)

        async def rerank(query, items, top_k):
            return items[:top_k]

        manager.rewrite_query = rewrite_query
        manager.call = call
        manager._rerank = rerank

        result = await manager.search_with_rewrite("knowledge_search", "退款", top_k=5)

        assert [item["id"] for item in result.data] == ["doc-1", "doc-2"]
        assert result.data[0]["score"] == 0.93

    asyncio.run(scenario())


def test_rerank_discards_duplicate_and_invalid_indices():
    class Messages:
        async def create(self, **kwargs):
            return types.SimpleNamespace(content=[{"type": "text", "text": "[1, 1, 99, 0]"}])

    async def scenario():
        manager = MCPToolManager.__new__(MCPToolManager)
        manager._client = types.SimpleNamespace(messages=Messages())
        manager._model = "test-model"
        items = [{"id": "a"}, {"id": "b"}, {"id": "c"}, {"id": "d"}]

        result = await manager._rerank("query", items, top_k=3)

        assert result == [{"id": "b"}, {"id": "a"}, {"id": "c"}]

    asyncio.run(scenario())
