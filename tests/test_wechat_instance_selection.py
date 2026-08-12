from __future__ import annotations

import inspect
import os
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from widget.ui.wechat_instance_picker import WechatInstancePicker
from widget.ui.theme import QSS
from widget.app import build_routed_wechat_adapter, prepare_legacy_wechat_selection
from widget.app import run as run_widget
from widget.instances import InstanceConfig
from widget.wechat.selection import (
    WechatProcessChoice,
    choose_exclusive_wechat_process,
    ensure_selected_wechat_routable,
    pick_wechat_window,
    wxid_from_open_paths,
)
from widget.wechat.hook_rebind import (
    _close_payload,
    _loopback_host_string,
    ensure_unique_hook_port,
)


class WechatProcessSelectionTests(unittest.TestCase):
    def test_rebound_hook_is_bound_to_loopback_only(self) -> None:
        host = _loopback_host_string()

        self.assertEqual(b"127.0.0.1\0", host[:10])
        self.assertEqual(9, int.from_bytes(host[16:24], "little"))
        self.assertEqual(15, int.from_bytes(host[24:32], "little"))

    def test_native_close_payload_branches_only_to_complete_instructions(self) -> None:
        payload = _close_payload(0x1111, 0x2222, 0x3333)
        fail_at = payload.index(b"\xb8\x01\x00\x00\xe0")
        stop_at = payload.index(b"\xc6\x43\x10\x00")

        missing_server_jump = payload.index(b"\x0f\x84")
        missing_server_rel = int.from_bytes(
            payload[missing_server_jump + 2:missing_server_jump + 6],
            "little",
            signed=True,
        )
        self.assertEqual(
            fail_at,
            missing_server_jump + 6 + missing_server_rel,
        )

        closed_socket_jump = payload.index(b"\x74", missing_server_jump + 6)
        closed_socket_rel = int.from_bytes(
            payload[closed_socket_jump + 1:closed_socket_jump + 2],
            "little",
            signed=True,
        )
        self.assertEqual(stop_at, closed_socket_jump + 2 + closed_socket_rel)

    def test_blocked_shared_listener_is_migrated_without_restarting_wechat(self) -> None:
        rebound: list[tuple[int, int, int]] = []
        probes = iter((False, True))

        port = ensure_unique_hook_port(
            41400,
            30001,
            probe_fn=lambda _pid, _port: next(probes),
            owners_fn=lambda port: [39940, 41400] if port == 30001 else [],
            allocate_fn=lambda **_kwargs: 30002,
            rebind_fn=lambda pid, old_port, new_port: rebound.append(
                (pid, old_port, new_port)
            ),
            sleep_fn=lambda _seconds: None,
            verify_attempts=1,
        )

        self.assertEqual(30002, port)
        self.assertEqual([(41400, 30001, 30002)], rebound)

    def test_reachable_selected_wechat_keeps_its_existing_port(self) -> None:
        rebound = Mock()

        port = ensure_unique_hook_port(
            41400,
            30001,
            probe_fn=lambda _pid, _port: True,
            owners_fn=lambda _port: [39940, 41400],
            rebind_fn=rebound,
        )

        self.assertEqual(30001, port)
        rebound.assert_not_called()

    def test_routed_adapter_uses_the_migrated_port_for_patch_and_http(self) -> None:
        inst = self._legacy_instance()
        selected = WechatProcessChoice(41400, "wxid_selected", "wxid_selected")
        patch_fn = Mock(return_value="g_IsLogin 已是 1")
        client_factory = Mock(return_value=object())
        adapter_factory = Mock(return_value="adapter")

        adapter, _result = build_routed_wechat_adapter(
            object(),
            inst,
            selected,
            30001,
            route_port_fn=lambda _pid, _port: 30002,
            client_factory=client_factory,
            adapter_factory=adapter_factory,
            patch_fn=patch_fn,
        )

        self.assertEqual("adapter", adapter)
        patch_fn.assert_called_once_with(pid=41400, port=30002, wxid="wxid_selected")
        client_factory.assert_called_once_with(41400, 30002)
        self.assertEqual(
            "http://127.0.0.1:30002",
            adapter_factory.call_args.kwargs["base_url"],
        )

    def test_normal_startup_never_restarts_the_selected_logged_in_wechat(self) -> None:
        source = inspect.getsource(run_widget)

        self.assertNotIn("ensure_selected_wechat_routable", source)

    def test_single_hooked_wechat_on_nondefault_port_is_reused_without_prompt(self) -> None:
        inst = self._legacy_instance()
        choice = WechatProcessChoice(
            pid=41400,
            wxid="wxid_alice",
            account_label="Alice",
            hook_port=30002,
        )
        chooser = Mock(side_effect=AssertionError("唯一已登录微信不应弹选择框"))

        runtime_legacy, selected, error = prepare_legacy_wechat_selection(
            [inst],
            legacy=True,
            port=30001,
            chooser=chooser,
            pids_fn=lambda: [41400],
            port_fn=lambda _pid: 30002,
            choices_fn=lambda _pids: [choice],
        )

        self.assertFalse(runtime_legacy)
        self.assertEqual(choice, selected)
        self.assertEqual("", error)
        self.assertEqual("wxid_alice", inst.self_wxid)
        chooser.assert_not_called()

    @staticmethod
    def _legacy_instance() -> InstanceConfig:
        return InstanceConfig(
            account_id="default", platform="wechat", display_name="个人微信",
            tenant_id=1, login="demo", password_enc="", channel_key="wechat_personal",
            hook_base_url="http://127.0.0.1:30001", synthesized=True,
        )

    def test_extracts_account_identity_from_xwechat_data_directory(self) -> None:
        wxid = wxid_from_open_paths([
            r"D:\WeChatData\xwechat_files\wxid_alice_7fa2\db_storage\session\session.db",
        ])

        self.assertEqual("wxid_alice", wxid)

    def test_public_all_users_directory_is_never_treated_as_an_account(self) -> None:
        wxid = wxid_from_open_paths([
            r"D:\WeChatData\xwechat_files\all_users\config\global.db",
            r"D:\WeChatData\xwechat_files\wxid_alice_7fa2\db_storage\session\session.db",
        ])

        self.assertEqual("wxid_alice", wxid)

    def test_selected_process_is_routed_without_stopping_other_wechat(self) -> None:
        choices = [
            WechatProcessChoice(pid=11, wxid="wxid_alice", account_label="wxid_alice"),
            WechatProcessChoice(pid=22, wxid="wxid_bob", account_label="wxid_bob"),
        ]
        stopped = Mock()

        inst = self._legacy_instance()
        runtime_legacy, selected, error = prepare_legacy_wechat_selection(
            [inst], legacy=True,
            port=30001,
            chooser=lambda _choices: choices[0],
            owners_fn=lambda _port: [11, 22],
            choices_fn=lambda _owners: choices,
            stop_fn=stopped,
            sleep_fn=lambda _seconds: None,
            attempts=2,
        )

        self.assertFalse(runtime_legacy)
        self.assertEqual(choices[0], selected)
        stopped.assert_not_called()
        self.assertEqual("", error)

    def test_hidden_wechat_main_window_is_used_when_no_visible_window_exists(self) -> None:
        hwnd, title = pick_wechat_window([
            (101, "WxTrayIconMessageWindow", False),
            (202, "微信", False),
            (303, "", False),
        ])

        self.assertEqual((202, "微信"), (hwnd, title))

    def test_cancel_keeps_every_wechat_process_running(self) -> None:
        stop = Mock()

        selected, error = choose_exclusive_wechat_process(
            30001,
            chooser=lambda _choices: None,
            owners_fn=lambda _port: [11, 22],
            choices_fn=lambda _owners: [
                WechatProcessChoice(11, "wxid_alice", "wxid_alice"),
                WechatProcessChoice(22, "wxid_bob", "wxid_bob"),
            ],
            stop_fn=stop,
            sleep_fn=lambda _seconds: None,
        )

        self.assertIsNone(selected)
        self.assertIn("取消", error)
        stop.assert_not_called()

    def test_unidentified_account_is_never_terminated_or_claimed(self) -> None:
        stop = Mock()
        unidentified = WechatProcessChoice(11, "", "账号识别中")

        selected, error = choose_exclusive_wechat_process(
            30001,
            chooser=lambda _choices: unidentified,
            owners_fn=lambda _port: [11, 22],
            choices_fn=lambda _owners: [unidentified, WechatProcessChoice(22, "wxid_bob", "bob")],
            stop_fn=stop,
        )

        self.assertIsNone(selected)
        self.assertIn("无法识别", error)
        stop.assert_not_called()

    def test_refuses_to_continue_when_unselected_process_did_not_exit(self) -> None:
        choice = WechatProcessChoice(11, "wxid_alice", "wxid_alice")

        selected, error = choose_exclusive_wechat_process(
            30001,
            chooser=lambda _choices: choice,
            owners_fn=lambda _port: [11, 22],
            choices_fn=lambda _owners: [choice, WechatProcessChoice(22, "wxid_bob", "wxid_bob")],
            stop_fn=lambda _pid: False,
            sleep_fn=lambda _seconds: None,
            attempts=1,
        )

        self.assertIsNone(selected)
        self.assertIn("仍被多个", error)

    def test_legacy_selection_promotes_runtime_to_verified_manager_path(self) -> None:
        inst = self._legacy_instance()
        choice = WechatProcessChoice(11, "wxid_alice", "wxid_alice")
        owner_samples = iter(([11, 22], [11]))

        runtime_legacy, selected, error = prepare_legacy_wechat_selection(
            [inst], legacy=True, port=30001,
            chooser=lambda _choices: choice,
            owners_fn=lambda _port: list(next(owner_samples)),
            choices_fn=lambda _owners: [choice, WechatProcessChoice(22, "wxid_bob", "wxid_bob")],
            stop_fn=lambda _pid: True,
            sleep_fn=lambda _seconds: None,
            attempts=2,
        )

        self.assertFalse(runtime_legacy)
        self.assertEqual(choice, selected)
        self.assertEqual("wxid_alice", inst.self_wxid)
        self.assertEqual("", error)

    def test_single_listener_does_not_show_picker_or_change_legacy_mode(self) -> None:
        inst = self._legacy_instance()
        chooser = Mock()

        runtime_legacy, selected, error = prepare_legacy_wechat_selection(
            [inst], legacy=True, port=30001, chooser=chooser,
            owners_fn=lambda _port: [11],
        )

        self.assertTrue(runtime_legacy)
        self.assertIsNone(selected)
        self.assertEqual("", error)
        chooser.assert_not_called()

    def test_distinct_hook_ports_still_offer_every_logged_in_wechat(self) -> None:
        inst = self._legacy_instance()
        choices = [
            WechatProcessChoice(11, "wxid_alice", "微信账号 wxid_alice", hook_port=30001),
            WechatProcessChoice(22, "wxid_bob", "微信账号 wxid_bob", hook_port=30002),
        ]
        chooser = Mock(return_value=choices[1])

        runtime_legacy, selected, error = prepare_legacy_wechat_selection(
            [inst], legacy=True, port=30001, chooser=chooser,
            owners_fn=lambda selected_port: [11] if selected_port == 30001 else [],
            pids_fn=lambda: [11, 22],
            port_fn=lambda pid: {11: 30001, 22: 30002}.get(pid),
            choices_fn=lambda pids: [choice for choice in choices if choice.pid in pids],
        )

        self.assertFalse(runtime_legacy)
        self.assertEqual(22, selected.pid)
        self.assertEqual(30002, selected.hook_port)
        self.assertEqual("", error)
        chooser.assert_called_once()

    def test_selected_process_builds_pid_routed_adapter_without_legacy_patch(self) -> None:
        inst = self._legacy_instance()
        selected = WechatProcessChoice(22, "wxid_selected", "微信号 wxid_selected")
        client = object()
        adapter_factory = Mock(return_value="routed-adapter")
        patch_fn = Mock(return_value="g_IsLogin 已=1")

        adapter, patch_result = build_routed_wechat_adapter(
            object(), inst, selected, 30001,
            route_port_fn=lambda _pid, port: port,
            client_factory=lambda _pid, _port: client,
            adapter_factory=adapter_factory,
            patch_fn=patch_fn,
            owns_port_fn=lambda _pid, _port: True,
        )

        self.assertEqual("routed-adapter", adapter)
        self.assertEqual("g_IsLogin 已=1", patch_result)
        patch_fn.assert_called_once_with(pid=22, port=30001, wxid="wxid_selected")
        kwargs = adapter_factory.call_args.kwargs
        self.assertIs(client, kwargs["client"])
        self.assertEqual(22, kwargs["verified_pid"])
        self.assertEqual("wxid_selected", kwargs["claimed_wxid"])
        self.assertFalse(kwargs["legacy_patch"])
        self.assertFalse(kwargs["patch_login"])

    def test_routed_adapter_waits_until_starting_wechat_is_patch_ready(self) -> None:
        inst = self._legacy_instance()
        selected = WechatProcessChoice(33, "wxid_selected", "微信号 wxid_selected")
        patch_fn = Mock(side_effect=["微信仍在启动，暂不补丁", "g_IsLogin 已=1（幂等）"])
        sleep_fn = Mock()

        _adapter, result = build_routed_wechat_adapter(
            object(), inst, selected, 30001,
            route_port_fn=lambda _pid, port: port,
            client_factory=lambda _pid, _port: object(),
            adapter_factory=Mock(return_value="adapter"),
            patch_fn=patch_fn,
            patch_attempts=2,
            sleep_fn=sleep_fn,
        )

        self.assertIn("g_IsLogin 已=1", result)
        self.assertEqual(2, patch_fn.call_count)
        sleep_fn.assert_called_once_with(0.25)

    def test_routed_adapter_fails_closed_on_account_identity_rejection(self) -> None:
        inst = self._legacy_instance()
        selected = WechatProcessChoice(33, "wxid_selected", "微信号 wxid_selected")
        adapter_factory = Mock(return_value="adapter")

        with self.assertRaises(RuntimeError):
            build_routed_wechat_adapter(
                object(), inst, selected, 30001,
                route_port_fn=lambda _pid, port: port,
                client_factory=lambda _pid, _port: object(),
                adapter_factory=adapter_factory,
                patch_fn=lambda **_kwargs: "pid=33 的账号身份不匹配，放弃写入",
            )

        adapter_factory.assert_not_called()

    def test_reachable_selected_wechat_is_not_restarted(self) -> None:
        selected = WechatProcessChoice(11, "wxid_alice", "微信号 wxid_alice")
        stopped = Mock()
        spawned = Mock()

        routed, error = ensure_selected_wechat_routable(
            selected, 30001,
            probe_fn=lambda _pid, _port: True,
            stop_fn=stopped,
            spawn_fn=spawned,
        )

        self.assertEqual(selected, routed)
        self.assertEqual("", error)
        stopped.assert_not_called()
        spawned.assert_not_called()

    def test_blocked_selected_wechat_is_reopened_but_unselected_is_not_stopped(self) -> None:
        selected = WechatProcessChoice(11, "wxid_alice", "微信号 wxid_alice")
        unselected_pid = 22
        stopped: list[int] = []
        spawned: list[str] = []
        probe_results = iter((False, True))

        routed, error = ensure_selected_wechat_routable(
            selected, 30001,
            probe_fn=lambda _pid, _port: next(probe_results),
            stop_fn=lambda pid: stopped.append(pid) or True,
            exe_fn=lambda pid: f"C:/Weixin/{pid}/Weixin.exe",
            spawn_fn=lambda exe: spawned.append(exe) or 33,
            owners_fn=lambda _port: [unselected_pid, 33],
            choices_fn=lambda _owners: [
                WechatProcessChoice(unselected_pid, "wxid_bob", "微信号 wxid_bob"),
                WechatProcessChoice(33, "wxid_alice", "微信号 wxid_alice"),
            ],
            sleep_fn=lambda _seconds: None,
            attempts=1,
        )

        self.assertEqual(33, routed.pid)
        self.assertEqual("wxid_alice", routed.wxid)
        self.assertEqual("", error)
        self.assertEqual([11], stopped)
        self.assertNotIn(unselected_pid, stopped)
        self.assertEqual(["C:/Weixin/11/Weixin.exe"], spawned)


class WechatInstancePickerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_user_can_select_a_process_and_preview_its_window(self) -> None:
        focused: list[int] = []
        choices = [
            WechatProcessChoice(11, "wxid_alice", "微信号 wxid_alice"),
            WechatProcessChoice(22, "wxid_bob", "微信号 wxid_bob"),
        ]
        dialog = WechatInstancePicker(choices, focus_fn=focused.append)

        dialog.preview_pid(22)
        dialog.select_pid(22)

        self.assertEqual(2, dialog.choice_count())
        self.assertEqual(22, dialog.selected_choice().pid)
        self.assertEqual([22], focused)

    def test_preview_selects_row_and_minimizes_picker_before_showing_wechat(self) -> None:
        observations: list[tuple[int, bool]] = []
        choices = [
            WechatProcessChoice(11, "wxid_alice", "微信号 wxid_alice"),
            WechatProcessChoice(22, "wxid_bob", "微信号 wxid_bob"),
        ]
        dialog = WechatInstancePicker(choices)
        dialog._focus_fn = lambda pid: observations.append((pid, dialog.isMinimized())) or True
        dialog.show()
        self.app.processEvents()

        dialog.preview_pid(22)
        self.app.processEvents()

        self.assertEqual(22, dialog.selected_choice().pid)
        self.assertEqual([(22, True)], observations)
        self.assertTrue(dialog.windowState() & Qt.WindowState.WindowMinimized)
        dialog.close()

    def test_picker_has_a_high_contrast_radio_indicator(self) -> None:
        choices = [WechatProcessChoice(11, "wxid_alice", "微信号 wxid_alice")]
        dialog = WechatInstancePicker(choices)
        try:
            radio = dialog._radios[11]
            self.assertEqual("WechatChoiceRadio", radio.objectName())
            self.assertIn(
                "QDialog#WechatInstancePicker QRadioButton#WechatChoiceRadio::indicator",
                QSS,
            )
            self.assertIn(
                "QDialog#WechatInstancePicker QRadioButton#WechatChoiceRadio::indicator:checked",
                QSS,
            )
        finally:
            dialog.close()
            dialog.deleteLater()

    def test_preview_failure_uses_inline_status_instead_of_warning_dialog(self) -> None:
        choice = WechatProcessChoice(11, "wxid_alice", "微信号 wxid_alice")
        dialog = WechatInstancePicker([choice], focus_fn=lambda _pid: False)
        dialog.show()
        self.app.processEvents()
        try:
            with patch("widget.ui.wechat_instance_picker.QMessageBox.warning") as warning:
                dialog.preview_pid(11)
                self.app.processEvents()
            warning.assert_not_called()
            self.assertFalse(dialog.isMinimized())
            self.assertIn("任务栏", dialog.preview_status())
        finally:
            dialog.close()
            dialog.deleteLater()


if __name__ == "__main__":
    unittest.main()
