from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.channels.wecom.service import process_kf_event


class _Db:
    def rollback(self) -> None:
        pass


class _Client:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str, str]] = []

    def get_access_token(self, _corp_id: str, _secret: str) -> str:
        return "token"

    def sync_msg(self, _token: str, _cursor: str, _open_kfid: str, _saved_cursor: str) -> dict:
        return {
            "msg_list": [{
                "msgtype": "text",
                "external_userid": "customer",
                "text": {"content": "知识库里没有的问题"},
            }],
        }

    def send_text(self, token: str, open_kfid: str, customer: str, content: str) -> bool:
        self.sent.append((token, open_kfid, customer, content))
        return True


class WeComServiceTests(unittest.TestCase):
    @patch("app.channels.wecom.service.update_customer_profile")
    @patch("app.channels.wecom.service.answer")
    def test_handoff_notice_is_sent_for_kb_miss(self, answer_mock, _profile_mock) -> None:
        answer_mock.return_value = {
            "action": "handoff",
            "reply_text": "帮您转接人工客服回复中，请稍等",
        }
        cfg = SimpleNamespace(
            corp_id="corp",
            secret="secret",
            sync_cursor="",
            tenant_id=1,
        )
        client = _Client()

        process_kf_event(_Db(), object(), cfg, client, "kf", "cursor")

        self.assertEqual(
            [("token", "kf", "customer", "帮您转接人工客服回复中，请稍等")],
            client.sent,
        )


if __name__ == "__main__":
    unittest.main()
