from __future__ import annotations

import threading
import time
import unittest
from unittest.mock import Mock, patch

from widget import hook_patch
from widget.adapters.wechat_hook import IdentityDrift, WeChatHookAdapter
from widget.config import WidgetConfig
from widget.wechat import ports


class _Response:
    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return {"status": 0, "data": []}


class _ConcurrentClient:
    def __init__(self) -> None:
        self.active = 0
        self.max_active = 0
        self.guard = threading.Lock()

    def post(self, _path: str, json: dict, **_kwargs) -> _Response:
        with self.guard:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            time.sleep(0.04)
            return _Response()
        finally:
            with self.guard:
                self.active -= 1


class _BodyResponse:
    def __init__(self, body) -> None:
        self.body = body

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return self.body


class _ColdDatabaseHandleClient:
    def __init__(self) -> None:
        self.paths: list[str] = []
        self.execute_calls = 0

    def post(self, path: str, json: dict, **_kwargs) -> _BodyResponse:
        self.paths.append(path)
        if path == "/QueryDB/GetAllDBName":
            return _BodyResponse(["session.db", "contact.db"])
        self.execute_calls += 1
        if self.execute_calls == 1:
            return _BodyResponse({
                "status": -1,
                "desc": "get database handle which named session.db failed",
            })
        return _BodyResponse({
            "status": 0,
            "data": [{
                "username": "wxid_customer",
                "summary": "最近一条",
                "sort_timestamp": "2",
            }],
        })


class _StaleProfileSessionClient:
    def __init__(self) -> None:
        self.profile_wxid = "wxid_stale_profile"

    def post(self, path: str, json: dict, **_kwargs) -> _BodyResponse:
        if path == "/GetSelfProfile":
            return _BodyResponse({"wxid": self.profile_wxid})
        if path == "/QueryDB/execute":
            return _BodyResponse({
                "status": 0,
                "data": [{
                    "username": "wxid_customer",
                    "summary": "latest question",
                    "sort_timestamp": "2",
                }],
            })
        raise AssertionError(f"unexpected hook endpoint: {path}")


class _UnavailableProfileClient(_StaleProfileSessionClient):
    def post(self, path: str, json: dict, **_kwargs) -> _BodyResponse:
        if path == "/GetSelfProfile":
            raise RuntimeError("profile endpoint unavailable")
        return super().post(path, json)


class WeChatHookSafetyTests(unittest.TestCase):
    def test_failed_send_response_is_not_remembered_as_sent(self) -> None:
        client = Mock()
        client.post.return_value = _BodyResponse({"ret": 1, "retmsg": "send failed"})
        adapter = WeChatHookAdapter(WidgetConfig(), client=client, patch_login=False)
        adapter._require_port_ownership = Mock()

        result = adapter.send_message("wxid_customer", "没有实际发出的文本", provenance="ai")

        self.assertFalse(result.ok)
        self.assertIsNone(adapter.provenance_for("没有实际发出的文本"))
    def test_unavailable_profile_probe_triggers_immediate_background_confirmation(self) -> None:
        now = [100.0]
        confirmed = threading.Event()

        def owns_account(_pid: int, _wxid: str) -> bool:
            confirmed.set()
            return True

        adapter = WeChatHookAdapter(
            WidgetConfig(),
            client=_UnavailableProfileClient(),
            patch_login=False,
            base_url="http://127.0.0.1:30001",
            verified_pid=123,
            claimed_wxid="wxid_claimed",
            owns_port_fn=lambda _pid, _port: True,
            owns_account_fn=owns_account,
            clock=lambda: now[0],
        )
        now[0] += 6.0                 # 让启动时的权威结论过期

        self.assertFalse(adapter.guard_ok())
        self.assertTrue(confirmed.wait(0.5))
        adapter._identity_refresh_thread.join(0.5)
        self.assertTrue(adapter.guard_ok())

    def test_background_confirmation_keeps_changed_account_blocked(self) -> None:
        now = [100.0]
        confirmed = threading.Event()

        def owns_account(_pid: int, _wxid: str) -> bool:
            confirmed.set()
            return False

        adapter = WeChatHookAdapter(
            WidgetConfig(),
            client=_UnavailableProfileClient(),
            patch_login=False,
            base_url="http://127.0.0.1:30001",
            verified_pid=123,
            claimed_wxid="wxid_claimed",
            owns_port_fn=lambda _pid, _port: True,
            owns_account_fn=owns_account,
            clock=lambda: now[0],
        )
        now[0] += 6.0

        self.assertFalse(adapter.guard_ok())
        self.assertTrue(confirmed.wait(0.5))
        adapter._identity_refresh_thread.join(0.5)
        self.assertFalse(adapter.guard_ok())

    def test_authoritative_account_probe_recovers_from_stale_hook_profile(self) -> None:
        now = [100.0]
        adapter = WeChatHookAdapter(
            WidgetConfig(),
            client=_StaleProfileSessionClient(),
            patch_login=False,
            base_url="http://127.0.0.1:30001",
            verified_pid=123,
            claimed_wxid="wxid_claimed",
            owns_port_fn=lambda _pid, _port: True,
            owns_account_fn=lambda _pid, _wxid: True,
            clock=lambda: now[0],
        )

        self.assertFalse(adapter.guard_ok())
        self.assertTrue(adapter.refresh_identity(force=True))

        sessions = adapter.list_sessions()

        self.assertEqual("wxid_customer", sessions[0]["wxid"])

    def test_disproved_profile_value_is_reconfirmed_after_authority_expires(self) -> None:
        now = [100.0]
        confirmed = threading.Event()
        adapter = WeChatHookAdapter(
            WidgetConfig(), client=_StaleProfileSessionClient(), patch_login=False,
            base_url="http://127.0.0.1:30001", verified_pid=123,
            claimed_wxid="wxid_claimed", owns_port_fn=lambda _pid, _port: True,
            owns_account_fn=lambda _pid, _wxid: confirmed.set() or True,
            clock=lambda: now[0],
        )
        self.assertFalse(adapter.guard_ok())
        self.assertTrue(adapter.refresh_identity(force=True))
        confirmed.clear()
        now[0] += 6.0

        self.assertFalse(adapter.guard_ok())
        self.assertTrue(confirmed.wait(0.5))
        adapter._identity_refresh_thread.join(0.5)
        self.assertTrue(adapter.guard_ok())

    def test_session_list_reports_identity_rejection_instead_of_empty(self) -> None:
        adapter = WeChatHookAdapter(
            WidgetConfig(),
            client=_StaleProfileSessionClient(),
            patch_login=False,
            base_url="http://127.0.0.1:30001",
            verified_pid=123,
            claimed_wxid="wxid_claimed",
            owns_port_fn=lambda _pid, _port: True,
            owns_account_fn=lambda _pid, _wxid: False,
        )

        self.assertFalse(adapter.guard_ok())
        self.assertFalse(adapter.refresh_identity(force=True))

        with self.assertRaises(IdentityDrift):
            adapter.list_sessions()

    def test_new_profile_mismatch_still_blocks_after_stale_value_is_disproved(self) -> None:
        now = [100.0]
        client = _StaleProfileSessionClient()
        adapter = WeChatHookAdapter(
            WidgetConfig(),
            client=client,
            patch_login=False,
            base_url="http://127.0.0.1:30001",
            verified_pid=123,
            claimed_wxid="wxid_claimed",
            owns_port_fn=lambda _pid, _port: True,
            owns_account_fn=lambda _pid, _wxid: True,
            clock=lambda: now[0],
        )

        self.assertFalse(adapter.guard_ok())
        self.assertTrue(adapter.refresh_identity(force=True))
        self.assertTrue(adapter.guard_ok())

        client.profile_wxid = "wxid_new_mismatch"
        now[0] += 1.0

        self.assertFalse(adapter.guard_ok())

    def test_duplicate_hook_listeners_are_not_treated_as_one_owner(self) -> None:
        listen = "LISTEN"
        connection = lambda pid: Mock(  # noqa: E731 - compact socket fixture
            status=listen, laddr=Mock(port=30001), pid=pid,
        )

        owners = ports.listening_pids_for_port(
            30001, connections=lambda: [connection(22), connection(11)],
        )

        self.assertEqual([11, 22], owners)
        self.assertFalse(ports.pid_still_owns_port(
            11, 30001, port_for_pid=lambda _pid: 30001,
            owners_for_port=lambda _port: owners,
        ))

    def test_unique_hook_listener_keeps_port_ownership(self) -> None:
        self.assertTrue(ports.pid_still_owns_port(
            11, 30001, port_for_pid=lambda _pid: 30001,
            owners_for_port=lambda _port: [11],
        ))

    def test_hook_patch_refuses_ambiguous_port_owner(self) -> None:
        with patch("widget.wechat.ports.listening_pids_for_port", return_value=[11, 22]):
            self.assertIsNone(hook_patch._hook_pid(30001))
            self.assertIn("多个微信进程", hook_patch._pid_port_mismatch(11, 30001))

    def test_routed_patch_accepts_only_selected_listener_with_matching_account(self) -> None:
        patch_memory = Mock(return_value="patched selected pid")

        result = hook_patch.ensure_login_patched_routed(
            pid=22, port=30001, wxid="wxid_selected",
            process_age_fn=lambda _pid: 60.0,
            account_owner_fn=lambda pid, wxid: pid == 22 and wxid == "wxid_selected",
            port_for_pid=lambda _pid: 30001,
            owners_for_port=lambda _port: [11, 22],
            patch_memory_fn=patch_memory,
        )

        self.assertEqual("patched selected pid", result)
        patch_memory.assert_called_once_with(22, 30001, False)

        rejected = hook_patch.ensure_login_patched_routed(
            pid=11, port=30001, wxid="wxid_selected",
            process_age_fn=lambda _pid: 60.0,
            account_owner_fn=lambda _pid, _wxid: False,
            port_for_pid=lambda _pid: 30001,
            owners_for_port=lambda _port: [11, 22],
            patch_memory_fn=Mock(side_effect=AssertionError("must not patch wrong account")),
        )
        self.assertIn("账号身份不匹配", rejected)


    def test_restart_baseline_discards_backlog_before_current_start(self) -> None:
        adapter = WeChatHookAdapter(WidgetConfig(), client=Mock(), patch_login=False)
        adapter._baseline_ready = True
        adapter.guard_ok = Mock(return_value=True)
        adapter._startup_cutoff = 100
        adapter._cursors = {"legacy-session": 4}
        adapter._list_suffixes_strict = Mock(return_value=["legacy-session"])
        adapter._md52name = {"legacy-session": "customer"}
        adapter._query = Mock(return_value=[{"mx": 25}])
        adapter._list_fts_tables = Mock(return_value=[])

        self.assertTrue(adapter.baseline())

        self.assertEqual(25, adapter._cursors["legacy-session"])

    def test_start_does_not_block_the_gui_on_startup_baseline(self) -> None:
        adapter = WeChatHookAdapter(WidgetConfig(), client=Mock(), patch_login=False)
        adapter._load_state = Mock()
        adapter.baseline = Mock(return_value=True)
        loop_finished = threading.Event()
        adapter._loop = loop_finished.set

        adapter.start(lambda _message: None)

        self.assertTrue(loop_finished.wait(1.0))
        adapter.baseline.assert_not_called()

    def test_old_row_can_never_enter_pipeline_after_start(self) -> None:
        adapter = WeChatHookAdapter(
            WidgetConfig(), client=Mock(), patch_login=False,
            self_wxid_override="wxid_self",
        )
        adapter._startup_cutoff = 100
        adapter._id2name = {1: "wxid_customer"}
        adapter._md52name = {"session": "wxid_customer"}
        adapter._is_self_sender = Mock(return_value=False)
        row = {
            "local_id": 7,
            "local_type": 1,
            "real_sender_id": 1,
            "create_time": 99,
            "mc": "old question",
            "ct": 0,
            "mc_hex": "",
            "src": "",
            "src_ct": 0,
            "src_hex": "",
        }

        self.assertIsNone(adapter._build_msg("session", row))

    def test_message_at_start_cutoff_is_still_processed(self) -> None:
        adapter = WeChatHookAdapter(
            WidgetConfig(), client=Mock(), patch_login=False,
            self_wxid_override="wxid_self",
        )
        adapter._baseline_ready = True
        adapter._startup_cutoff = 100
        adapter.guard_ok = Mock(return_value=True)
        adapter._cursors = {"session": 6}
        adapter._list_suffixes = Mock(return_value=["session"])
        adapter._id2name = {1: "wxid_customer"}
        adapter._md52name = {"session": "wxid_customer"}
        adapter._is_self_sender = Mock(return_value=False)
        adapter._query = Mock(return_value=[{
            "local_id": 7,
            "local_type": 1,
            "real_sender_id": 1,
            "create_time": 100,
            "mc": "new question",
            "ct": 0,
            "mc_hex": "",
            "src": "",
            "src_ct": 0,
            "src_hex": "",
        }])
        adapter._poll_once_fts = Mock(return_value=[])

        messages = adapter.poll_once()

        self.assertEqual(1, len(messages))
        self.assertEqual("new question", messages[0]["text"])

    def test_poll_waits_until_a_real_startup_baseline_succeeds(self) -> None:
        adapter = WeChatHookAdapter(WidgetConfig(), client=Mock(), patch_login=False)
        adapter._baseline_ready = False
        adapter.baseline = Mock(return_value=False)
        adapter.guard_ok = Mock(return_value=True)
        adapter._list_suffixes = Mock(return_value=[])

        self.assertEqual([], adapter.poll_once())

        adapter.baseline.assert_called_once_with()
        adapter._list_suffixes.assert_not_called()

    def test_mixed_storage_polls_fts_only_conversations_too(self) -> None:
        adapter = WeChatHookAdapter(WidgetConfig(), client=Mock(), patch_login=False)
        adapter._baseline_ready = True
        adapter.guard_ok = Mock(return_value=True)
        adapter._list_suffixes = Mock(return_value=["legacy-session"])
        adapter._strangers_confirmed = Mock(return_value=True)
        adapter._md52name = {"legacy-session": "contact"}
        adapter._query = Mock(return_value=[])
        fts_message = {
            "channel": "wechat_personal",
            "msg_id": "fts:1",
            "contact_id": "fts-only-contact",
            "sender_id": "sender",
            "text": "hello",
            "is_group": False,
            "at_me": False,
            "timestamp": 1,
        }
        adapter._poll_once_fts = Mock(return_value=[fts_message])

        self.assertEqual([fts_message], adapter.poll_once())
        adapter._poll_once_fts.assert_called_once_with(
            excluded_session_suffixes={"legacy-session"}
        )

    def test_multiple_legacy_conversations_are_polled_in_one_query(self) -> None:
        adapter = WeChatHookAdapter(WidgetConfig(), client=Mock(), patch_login=False)
        adapter._baseline_ready = True
        adapter.guard_ok = Mock(return_value=True)
        adapter._list_suffixes = Mock(return_value=["first", "second"])
        adapter._strangers_confirmed = Mock(return_value=True)
        adapter._cursors = {"first": 3, "second": 8}
        adapter._md52name = {"first": "a", "second": "b"}
        adapter._query = Mock(return_value=[{
            "table_suffix": "second", "local_id": 9, "create_time": 10,
        }])
        message = {
            "channel": "wechat_personal", "msg_id": "second:9",
            "contact_id": "b", "sender_id": "b", "text": "new",
            "is_group": False, "at_me": False, "timestamp": 10,
        }
        adapter._build_msg = Mock(return_value=message)
        adapter._poll_once_fts = Mock(return_value=[])
        adapter._save_state = Mock()

        self.assertEqual([message], adapter.poll_once())

        adapter._query.assert_called_once()
        sql = adapter._query.call_args.args[0]
        self.assertIn("UNION ALL", sql)
        self.assertIn("ORDER BY local_id ASC LIMIT 200", sql)
        self.assertEqual(9, adapter._cursors["second"])

    def test_batched_poll_has_no_outer_limit_that_can_skip_table_prefixes(self) -> None:
        adapter = WeChatHookAdapter(WidgetConfig(), client=Mock(), patch_login=False)
        suffixes = [format(index, "032x") for index in range(6)]
        adapter._cursors = {suffix: 0 for suffix in suffixes}
        adapter._query = Mock(return_value=[])

        adapter._poll_legacy_rows(suffixes)

        sql = adapter._query.call_args.args[0]
        self.assertEqual(6, sql.count("ORDER BY local_id ASC LIMIT 200"))
        self.assertNotIn("ORDER BY create_time ASC, local_id ASC LIMIT", sql)

    def test_legacy_table_listing_ignores_unexpected_identifiers(self) -> None:
        adapter = WeChatHookAdapter(WidgetConfig(), client=Mock(), patch_login=False)
        valid = "a" * 32
        adapter._query = Mock(return_value=[
            {"name": f"Msg_{valid}"},
            {"name": "Msg_bad-name"},
            {"name": "Msg_x;DROP TABLE SessionTable"},
        ])

        self.assertEqual([valid], adapter._list_suffixes_strict())

    def test_cross_storage_copy_is_delivered_only_once(self) -> None:
        adapter = WeChatHookAdapter(WidgetConfig(), client=Mock(), patch_login=False)
        base = {
            "channel": "wechat_personal",
            "contact_id": "customer",
            "sender_id": "customer",
            "text": "business hours?",
            "is_group": False,
            "at_me": False,
            "timestamp": 123,
        }
        legacy = dict(base, msg_id="abc:1")
        fts = dict(base, msg_id="fts:message_fts_v4_0:9")

        self.assertEqual([legacy], adapter._dedupe_cross_store([legacy, fts]))
        # 同一存储中客户真的重复发送，不因内容相同而被吞掉。
        self.assertEqual(
            [dict(base, msg_id="abc:2")],
            adapter._dedupe_cross_store([dict(base, msg_id="abc:2")]),
        )

    def test_mixed_storage_baselines_both_cursor_families(self) -> None:
        adapter = WeChatHookAdapter(WidgetConfig(), client=Mock(), patch_login=False)
        adapter.guard_ok = Mock(return_value=True)
        adapter._list_suffixes_strict = Mock(return_value=["legacy-session"])
        adapter._md52name = {"legacy-session": "contact"}
        adapter._query = Mock(return_value=[{"mx": 11}])
        adapter._list_fts_tables = Mock(return_value=["message_fts_v4_0"])
        adapter._query_db = Mock(return_value=[{"mx": 22}])

        adapter.baseline()

        self.assertEqual(11, adapter._cursors["legacy-session"])
        self.assertEqual(22, adapter._cursors["fts:message_fts_v4_0"])

    def test_querydb_requests_are_serialized_per_hook_port(self) -> None:
        client = _ConcurrentClient()
        adapters = [
            WeChatHookAdapter(
                WidgetConfig(), client=client, patch_login=False,
                base_url="http://127.0.0.1:30001",
            )
            for _ in range(2)
        ]
        barrier = threading.Barrier(3)

        def query(adapter: WeChatHookAdapter) -> None:
            barrier.wait()
            adapter.query_db("message_0.db", "SELECT 1")

        threads = [
            threading.Thread(target=query, args=(adapter,))
            for adapter in adapters
        ]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=2)

        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(1, client.max_active)

    def test_session_list_recovers_when_cold_hook_has_no_database_handles(self) -> None:
        client = _ColdDatabaseHandleClient()
        adapter = WeChatHookAdapter(
            WidgetConfig(), client=client, patch_login=False,
        )

        sessions = adapter.list_sessions()

        self.assertEqual("wxid_customer", sessions[0]["wxid"])
        self.assertEqual(
            ["/QueryDB/execute", "/QueryDB/GetAllDBName", "/QueryDB/execute"],
            client.paths,
        )

    def test_periodic_patch_does_not_probe_querydb(self) -> None:
        with patch.object(hook_patch, "_hook_pid", return_value=123), \
                patch.object(hook_patch, "_login_ready_for_patch", return_value=(True, "")), \
                patch.object(hook_patch, "_pid_port_mismatch", return_value=""), \
                patch.object(hook_patch, "_find_hook_bases", return_value=[0x100000]), \
                patch.object(hook_patch, "_read", return_value=(1).to_bytes(8, "little")), \
                patch.object(hook_patch, "_querydb_count") as querydb, \
                patch.object(hook_patch._k32, "OpenProcess", return_value=1), \
                patch.object(hook_patch._k32, "CloseHandle"):
            result = hook_patch.ensure_login_patched(pid=123, port=30001)

        self.assertIn("g_IsLogin", result)
        querydb.assert_not_called()

    def test_launcher_readiness_can_explicitly_probe_querydb(self) -> None:
        with patch.object(hook_patch, "_hook_pid", return_value=123), \
                patch.object(hook_patch, "_login_ready_for_patch", return_value=(True, "")), \
                patch.object(hook_patch, "_pid_port_mismatch", return_value=""), \
                patch.object(hook_patch, "_find_hook_bases", return_value=[0x100000]), \
                patch.object(hook_patch, "_read", return_value=(1).to_bytes(8, "little")), \
                patch.object(hook_patch, "_querydb_count", return_value=4) as querydb, \
                patch.object(hook_patch._k32, "OpenProcess", return_value=1), \
                patch.object(hook_patch._k32, "CloseHandle"):
            result = hook_patch.ensure_login_patched(
                pid=123, port=30001, verify_querydb=True,
            )

        self.assertIn("QueryDB=4", result)
        querydb.assert_called_once_with(30001)

    def test_safe_scanner_hook_skips_legacy_login_memory_write(self) -> None:
        identity = (
            hook_patch.SAFE_SCANNER_PE_TIMESTAMP,
            hook_patch.SAFE_SCANNER_SIZEOFIMAGE,
        )
        with patch.object(hook_patch, "_hook_pid", return_value=123), \
                patch.object(hook_patch, "_login_ready_for_patch", return_value=(True, "")), \
                patch.object(hook_patch, "_pid_port_mismatch", return_value=""), \
                patch.object(hook_patch, "_find_hook_bases", return_value=[0x100000]), \
                patch.object(hook_patch, "_hook_image_identity", return_value=identity), \
                patch.object(hook_patch._k32, "OpenProcess", return_value=1), \
                patch.object(hook_patch._k32, "WriteProcessMemory") as write, \
                patch.object(hook_patch._k32, "CloseHandle"):
            result = hook_patch.ensure_login_patched(pid=123, port=30001)

        self.assertIn("g_IsLogin 已=1", result)
        write.assert_not_called()

if __name__ == "__main__":
    unittest.main()
