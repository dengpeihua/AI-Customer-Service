import asyncio
import sys
import types
import unittest


def _install_import_stubs_for_local_test_environment():
    """Allow this focused unit test to run when optional services are not installed."""
    try:
        import chromadb  # noqa: F401
    except ModuleNotFoundError:
        sys.modules["chromadb"] = types.ModuleType("chromadb")

    try:
        import redis.asyncio  # noqa: F401
    except ModuleNotFoundError:
        redis_module = types.ModuleType("redis")
        redis_module.__path__ = []
        redis_asyncio_module = types.ModuleType("redis.asyncio")
        redis_module.asyncio = redis_asyncio_module
        sys.modules["redis"] = redis_module
        sys.modules["redis.asyncio"] = redis_asyncio_module

    try:
        import anthropic  # noqa: F401
    except ModuleNotFoundError:
        anthropic_module = types.ModuleType("anthropic")
        anthropic_module.AsyncAnthropic = object
        sys.modules["anthropic"] = anthropic_module


_install_import_stubs_for_local_test_environment()

from memory.conversation_memory import MemoryManager, MsgRole


class InMemoryRedis:
    def __init__(self):
        self.values = {}

    async def lpush(self, key, value):
        self.values.setdefault(key, []).insert(0, value)

    async def expire(self, key, seconds):
        return True

    async def llen(self, key):
        return len(self.values.get(key, []))

    async def lrange(self, key, start, end):
        values = self.values.get(key, [])
        return values[start:] if end == -1 else values[start:end + 1]

    async def delete(self, key):
        self.values.pop(key, None)

    async def get(self, key):
        value = self.values.get(key)
        return value if isinstance(value, str) else None

    async def setex(self, key, seconds, value):
        self.values[key] = value


class SummaryClient:
    class Messages:
        async def create(self, **kwargs):
            return types.SimpleNamespace(
                content=[{"type": "text", "text": "测试摘要"}],
            )

    def __init__(self):
        self.messages = self.Messages()


def make_memory_manager():
    manager = MemoryManager.__new__(MemoryManager)
    manager._redis = InMemoryRedis()
    manager._client = SummaryClient()
    manager._model = "test-model"

    async def ignore_episodic_store(user_id, conv_id, text, summary):
        return None

    manager._store_episodic = ignore_episodic_store
    return manager


class ConversationMemoryOrderTest(unittest.TestCase):
    def test_compression_keeps_recent_messages_in_chronological_order(self):
        async def scenario():
            manager = make_memory_manager()

            for index in range(1, 16):
                await manager.add_message(
                    "user-1",
                    "conversation-1",
                    MsgRole.USER,
                    f"M{index}",
                )

            after_compression = await manager._get_working_memory(
                "user-1",
                "conversation-1",
            )
            self.assertEqual(
                [message.content for message in after_compression],
                ["M11", "M12", "M13", "M14", "M15"],
            )

            await manager.add_message(
                "user-1",
                "conversation-1",
                MsgRole.ASSISTANT,
                "M16",
            )
            after_new_message = await manager._get_working_memory(
                "user-1",
                "conversation-1",
            )
            self.assertEqual(
                [message.content for message in after_new_message],
                ["M11", "M12", "M13", "M14", "M15", "M16"],
            )

            for index in range(17, 26):
                await manager.add_message(
                    "user-1",
                    "conversation-1",
                    MsgRole.USER,
                    f"M{index}",
                )
            after_second_compression = await manager._get_working_memory(
                "user-1",
                "conversation-1",
            )
            self.assertEqual(
                [message.content for message in after_second_compression],
                ["M21", "M22", "M23", "M24", "M25"],
            )

        asyncio.run(scenario())

    def test_episodic_search_uses_compound_scope_then_same_user_fallback(self):
        async def scenario():
            manager = MemoryManager.__new__(MemoryManager)
            calls = []

            async def fake_query(query_text, n_results, where):
                calls.append(where)
                if len(calls) == 1:
                    return {"documents": [["当前会话记忆"]]}
                return {"documents": [["当前会话记忆", "同一用户的历史记忆"]]}

            manager._query_episodic = fake_query
            docs = await manager._search_episodic("user-1", "conversation-1", "退款")

            self.assertEqual(
                calls[0],
                {
                    "$and": [
                        {"user_id": {"$eq": "user-1"}},
                        {"conv_id": {"$eq": "conversation-1"}},
                    ]
                },
            )
            self.assertEqual(calls[1], {"user_id": {"$eq": "user-1"}})
            self.assertEqual(docs, ["当前会话记忆", "同一用户的历史记忆"])

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
