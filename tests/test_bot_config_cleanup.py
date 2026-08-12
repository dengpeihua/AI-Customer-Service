from __future__ import annotations

import inspect
import unittest

from app.config import Settings, validate_agent_routing_config
from app.crud import bot as bot_crud
from app.models.config import BotConfig
from app.routers.chat import ChatOut


class BotConfigCleanupTests(unittest.TestCase):
    def test_removed_routing_fields_are_not_in_model_or_crud(self) -> None:
        self.assertNotIn("auto_reply_threshold", BotConfig.__table__.columns)
        self.assertNotIn("handoff_rules", BotConfig.__table__.columns)
        parameters = inspect.signature(bot_crud.update).parameters
        self.assertNotIn("auto_reply_threshold", parameters)
        self.assertNotIn("handoff_rules", parameters)

    def test_chat_response_has_no_route_confidence_field(self) -> None:
        self.assertNotIn("confidence", ChatOut.model_fields)

    def test_agent_routing_rejects_fake_and_missing_provider_keys(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "fake"):
            validate_agent_routing_config(Settings(llm_provider="fake"))
        with self.assertRaisesRegex(RuntimeError, "dashscope_api_key"):
            validate_agent_routing_config(Settings(
                llm_provider="dashscope",
                dashscope_api_key="",
            ))
        with self.assertRaisesRegex(RuntimeError, "deepseek_api_key"):
            validate_agent_routing_config(Settings(
                llm_provider="deepseek",
                deepseek_api_key="",
                dashscope_api_key="embedding-key",
            ))

    def test_agent_routing_accepts_supported_configurations(self) -> None:
        validate_agent_routing_config(Settings(
            llm_provider="dashscope",
            dashscope_api_key="chat-and-embedding-key",
        ))
        validate_agent_routing_config(Settings(
            llm_provider="deepseek",
            deepseek_api_key="chat-key",
            dashscope_api_key="embedding-key",
        ))


if __name__ == "__main__":
    unittest.main()
