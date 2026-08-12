from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QLabel, QPushButton

from widget.adapters.wechat_hook import WeChatHookAdapter
from widget.config import WidgetConfig
from widget.ui.message_content import MessageContent, _emit_preview_safely
from widget.wechat_media import (WechatMediaResolver, decrypt_wechat_image,
                                 parse_wechat_message, split_local_type)


class WechatMediaParserTests(unittest.TestCase):
    def test_splits_packed_app_type(self) -> None:
        self.assertEqual((49, 6), split_local_type((6 << 32) | 49))

    def test_parses_core_media_types(self) -> None:
        image = parse_wechat_message(3, '<msg><img md5="0123456789abcdef0123456789abcdef"/></msg>')
        emoji = parse_wechat_message(47, '<msg><emoji md5="a" cdnurl="https://example.test/e.gif"/></msg>')
        voice = parse_wechat_message(34, '<msg><voicemsg voicelength="3200"/></msg>')
        video = parse_wechat_message(43, '<msg><videomsg md5="b" playlength="8"/></msg>')

        self.assertEqual("image", image["kind"])
        self.assertEqual("emoji", emoji["kind"])
        self.assertEqual("https://example.test/e.gif", emoji["remote_url"])
        self.assertEqual(3200, voice["duration_ms"])
        self.assertEqual(8000, video["duration_ms"])

    def test_parses_file_and_app_cards(self) -> None:
        file_xml = """<msg><appmsg><title>报价单</title><type>6</type>
          <appattach><totallen>2048</totallen><fileext>pdf</fileext></appattach>
        </appmsg></msg>"""
        link_xml = """<msg><appmsg><title>小红书帖子</title><des>帖子摘要</des>
          <url>https://www.xiaohongshu.com/explore/abc</url></appmsg></msg>"""

        file_message = parse_wechat_message((6 << 32) | 49, file_xml)
        link_message = parse_wechat_message((5 << 32) | 49, link_xml)

        self.assertEqual("file", file_message["kind"])
        self.assertEqual("报价单.pdf", file_message["file_name"])
        self.assertEqual(2048, file_message["file_size"])
        self.assertEqual("link", link_message["kind"])
        self.assertEqual("小红书帖子", link_message["title"])
        self.assertTrue(link_message["url"].startswith("https://"))

    def test_uses_xml_app_type_when_local_type_is_plain_49(self) -> None:
        message = parse_wechat_message(
            49, "<msg><appmsg><type>5</type><title>网页</title><url>https://example.test</url></appmsg></msg>")
        self.assertEqual("link", message["kind"])

    def test_parses_quoted_reply_instead_of_application_post(self) -> None:
        quote_xml = """<msg><appmsg><title>我同意这个方案</title><type>57</type>
          <refermsg><type>1</type><displayname>张三</displayname>
            <content>明天下午三点开会</content>
          </refermsg>
        </appmsg></msg>"""

        message = parse_wechat_message((57 << 32) | 49, quote_xml)

        self.assertEqual("quote", message["kind"])
        self.assertEqual("我同意这个方案", message["text"])
        self.assertEqual("张三", message["quote_sender"])
        self.assertEqual("明天下午三点开会", message["quote_text"])
        self.assertEqual(1, message["quote_type"])

    def test_uses_xml_quote_type_when_local_type_is_plain_49(self) -> None:
        quote_xml = """<msg><appmsg><title>收到</title><type>57</type>
          <refermsg><type>1</type><displayname>李四</displayname><content>原消息</content></refermsg>
        </appmsg></msg>"""

        message = parse_wechat_message(49, quote_xml)

        self.assertEqual("quote", message["kind"])
        self.assertEqual("收到", message["text"])
        self.assertEqual("原消息", message["quote_text"])

    def test_rejects_unsafe_app_url(self) -> None:
        message = parse_wechat_message(
            (5 << 32) | 49,
            "<msg><appmsg><title>x</title><url>file:///C:/secret.txt</url></appmsg></msg>",
        )
        self.assertEqual("", message["url"])

    def test_decrypts_v1_image_with_fixed_key(self) -> None:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        from cryptography.hazmat.primitives.padding import PKCS7

        plain = b"\x89PNG\r\n\x1a\n" + b"preview"
        padder = PKCS7(128).padder()
        padded = padder.update(plain) + padder.finalize()
        encrypted = Cipher(algorithms.AES(b"cfcd208495d565ef"), modes.ECB()).encryptor().update(padded)
        data = b"\x07\x08V1\x08\x07" + len(plain).to_bytes(4, "little") + (0).to_bytes(4, "little") \
            + b"\x00" + encrypted

        self.assertEqual(plain, decrypt_wechat_image(data))


class WechatMediaResolverTests(unittest.TestCase):
    def test_resolves_image_inside_current_account_only(self) -> None:
        md5 = "0123456789abcdef0123456789abcdef"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            image = root / "msg" / "attach" / "chat-md5" / "2026-08" / "Img" / "sample.dat"
            image.parent.mkdir(parents=True)
            image.write_bytes(b"encrypted")

            def query(db: str, sql: str) -> list[dict]:
                self.assertEqual("hardlink.db", db)
                if "FROM dir2id" in sql:
                    return [{"rid": 1, "username": "chat-md5"},
                            {"rid": 2, "username": "2026-08"}]
                return [{"file_name": "sample.dat", "dir1": 1, "dir2": 2,
                         "file_size": 9, "type": 2}]

            resolver = WechatMediaResolver(query, root)
            result = resolver.enrich({"kind": "image", "md5": md5})

            self.assertEqual(str(image.resolve()), result["media_path"])

    def test_resolves_file_when_month_is_stored_in_dir1(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            file_path = root / "msg" / "file" / "2026-08" / "报价单.pdf"
            file_path.parent.mkdir(parents=True)
            file_path.write_bytes(b"pdf")

            def query(_db: str, sql: str) -> list[dict]:
                if "FROM dir2id" in sql:
                    return [{"rid": 7, "username": "2026-08"}]
                return [{"file_name": "报价单.pdf", "dir1": 7, "dir2": 0,
                         "file_size": 3, "type": 1}]

            result = WechatMediaResolver(query, root).enrich(
                {"kind": "file", "file_name": "报价单.pdf"})
            self.assertEqual(str(file_path.resolve()), result["media_path"])


class WechatMessageDatabaseTests(unittest.TestCase):
    def test_uses_message_database_opened_by_claimed_process(self) -> None:
        adapter = WeChatHookAdapter(WidgetConfig(), patch_login=False,
                                    self_wxid_override="wxid_self")
        adapter._message_db_from_process = lambda: ("message_7.db", None)
        adapter._query_db = Mock(return_value=[{"name": "Msg_contact"}])
        self.assertEqual("message_7.db", adapter._ensure_message_db())

    def test_verified_process_candidate_is_retried_without_cross_account_probe(self) -> None:
        adapter = WeChatHookAdapter(WidgetConfig(), patch_login=False,
                                    self_wxid_override="wxid_self", verified_pid=123)
        adapter._message_db_from_process = lambda: ("message_0.db", None)
        attempts = []

        def query(db: str, _sql: str) -> list[dict]:
            attempts.append(db)
            if len(attempts) == 1:
                raise RuntimeError("get database handle failed")
            return [{"name": "Msg_contact"}]

        adapter._query_db = query

        self.assertEqual("message_0.db", adapter._ensure_message_db())
        self.assertEqual("", adapter._msg_db_name)
        self.assertEqual("message_0.db", adapter._ensure_message_db())
        self.assertEqual("message_0.db", adapter._msg_db_name)
        self.assertEqual(["message_0.db", "message_0.db"], attempts)

    def test_failed_database_discovery_is_not_cached(self) -> None:
        adapter = WeChatHookAdapter(WidgetConfig(), patch_login=False,
                                    self_wxid_override="wxid_self")
        adapter._message_db_from_process = lambda: ("message_0.db", None)
        adapter._query_db = Mock(side_effect=RuntimeError("hook not ready"))

        self.assertEqual("message_0.db", adapter._ensure_message_db())
        self.assertEqual("", adapter._msg_db_name)

    def test_accessible_database_without_legacy_tables_is_valid(self) -> None:
        adapter = WeChatHookAdapter(WidgetConfig(), patch_login=False,
                                    self_wxid_override="wxid_self")
        adapter._message_db_from_process = lambda: ("", None)
        seen = []

        def query(db: str, _sql: str) -> list[dict]:
            seen.append(db)
            return []

        adapter._query_db = query
        self.assertEqual("message_0.db", adapter._ensure_message_db())
        self.assertEqual(["message_0.db"], seen)

    def test_legacy_candidate_failure_uses_queryable_hook_database(self) -> None:
        adapter = WeChatHookAdapter(WidgetConfig(), patch_login=False,
                                    self_wxid_override="wxid_self")
        adapter._message_db_from_process = lambda: ("message_0.db", None)
        seen = []

        def query(db: str, _sql: str) -> list[dict]:
            seen.append(db)
            if db == "message_0.db":
                raise RuntimeError("get database handle failed")
            return []

        adapter._query_db = query
        self.assertEqual("message_1.db", adapter._ensure_message_db())
        self.assertEqual(["message_0.db", "message_1.db"], seen)


class MessageContentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_file_card_is_visible_when_attachment_missing(self) -> None:
        card = MessageContent({"kind": "file", "title": "报价单.pdf", "file_size": 2048})
        labels = [label.text() for label in card.findChildren(QLabel)]
        self.assertIn("报价单.pdf", labels)
        self.assertTrue(any("尚未下载" in text for text in labels))

    def test_link_card_has_open_button_but_unsafe_url_does_not(self) -> None:
        safe = MessageContent({"kind": "link", "title": "帖子", "url": "https://example.test/p"})
        unsafe = MessageContent({"kind": "link", "title": "帖子", "url": "file:///C:/x"})
        self.assertEqual(["打开内容"], [b.text() for b in safe.findChildren(QPushButton)])
        self.assertEqual([], [b.text() for b in unsafe.findChildren(QPushButton)])

    def test_quoted_reply_shows_reply_and_quoted_message(self) -> None:
        widget = MessageContent({
            "kind": "quote",
            "text": "我同意这个方案",
            "quote_sender": "张三",
            "quote_text": "明天下午三点开会",
        })
        labels = [label.text() for label in widget.findChildren(QLabel)]

        self.assertIn("我同意这个方案", labels)
        self.assertIn("引用 张三", labels)
        self.assertIn("明天下午三点开会", labels)
        self.assertNotIn("应用帖子", labels)
        self.assertNotIn("应用消息", labels)

    def test_deleted_preview_widget_drops_late_worker_result(self) -> None:
        class DeletedSignal:
            def emit(self, *_args) -> None:
                raise RuntimeError("Signal source has been deleted")

        class DeletedWidget:
            _preview_ready = DeletedSignal()

        _emit_preview_safely(DeletedWidget(), b"preview", "")


if __name__ == "__main__":
    unittest.main()
