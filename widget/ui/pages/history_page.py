from __future__ import annotations
import datetime as dt
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from PySide6.QtCore import Qt, QSize, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QIcon
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel,
                               QListWidget, QListWidgetItem, QPushButton, QScrollArea,
                               QMessageBox, QCheckBox, QLineEdit, QComboBox)
from widget.bridge import Bridge, BridgeError
from widget.ui.pages.customer_profile_dialog import CustomerProfileDialog
from widget.ui.message_content import message_content_widget
from widget.wechat_media import message_signature
from widget.live_conversation import (
    event_to_history_message,
    merge_history_snapshots,
    merge_live_messages,
)

# 消息来源标记：文字 + 颜色。客户=对方；我方出向按 AI记录/群发记录/都无 分三类。
_SOURCE_TAG = {
    "customer": ("客户", "#2A78D6"),
    "ai": ("AI 自动", "#12B3A6"),
    "broadcast": ("群发", "#8064A2"),
    "agent": ("人工", "#E0A100"),
}


class HistoryPage(QWidget):
    """聊天记录 = 真实微信聊天（读消息库），叠加 AI/人工来源标记。

    左侧：微信最近私聊会话；右侧：选中联系人的真实完整聊天。我方每条出向消息
    去后端比对：文本命中后端 AI 回复集 → 标「AI 自动」，否则「人工」。
    """

    # 后台 live_tick 读完（在工作线程里读 hook/本地库）→ 主线程渲染。绝不在 GUI 线程做阻塞读。
    _tick_ready = Signal(object)
    _chat_ready = Signal(object)
    _ai_state_ready = Signal(object)

    def __init__(self, bridge: Bridge, adapter=None, avatar_provider=None, on_open_chat=None,
                 tasks_path: str = "", source=None, on_followed=None, controller=None,
                 channels=None, adapter_for=None, default_channel: str = "", state=None,
                 defer_initial_load: bool = False):
        super().__init__()
        self.bridge = bridge
        self.controller = controller          # HandoffController：人工回复按渠道路由+释放待人工
        self.state = state                    # 会话级「是否接待」选择 + 持久化配置
        self.avatars = avatar_provider
        self.tasks_path = tasks_path          # 群发任务记录，用于给出向消息标「群发」
        self.source = source                  # ContactSource，渲染群发文案变量用
        self.on_followed = on_followed         # 跟随切换时通知外层把本页切到前台
        # 多渠道：channels=[(key,label),...] + adapter_for(key)->adapter（如 hub.adapter，实时解析
        # 便于重连后拿到新 adapter）。给了就在标题行渲染渠道下拉、可在个人微信/企微间切换；否则单
        # 渠道（adapter 直用），行为与老式一致。
        self._adapter_for = adapter_for
        self._channels: list = list(channels or [])
        if self._channels and adapter_for is not None:
            _keys = [k for k, _ in self._channels]
            self._channel_key = default_channel if default_channel in _keys else _keys[0]
            self.adapter = adapter_for(self._channel_key) or adapter
        else:
            self.adapter = adapter
            self._channel_key = getattr(adapter, "channel", "") if adapter is not None else ""
        self._sessions: list[dict] = []
        self._names: dict[str, str] = {}     # wxid -> 显示名（备注/昵称）
        self._current_wxid: str = ""
        self._profile_dialog = None
        self._last_active: str = ""      # 上次探到的微信活跃会话；只在它变化时才跟随
        self._last_msgs: list = []       # 当前会话已渲染的消息签名，用于实时刷新时判断有无变化
        self._message_avatar_labels: dict[str, list[tuple[QLabel, str]]] = {}
        self._conversation_cache: dict[tuple[str, str], list[dict]] = {}
        self._live_messages: dict[tuple[str, str], list[dict]] = {}
        self._chat_generation = 0
        self._chat_executor = ThreadPoolExecutor(
            max_workers=4, thread_name_prefix="history-chat",
        )
        self._chat_futures: list[Future] = []
        self._chat_pending: dict[int, set[str]] = {}
        self._chat_snapshots: dict[int, dict[str, list[dict]]] = {}
        self._chat_local_applied_generation = 0
        self._tick_busy = False          # live_tick 后台读进行中：跳过下一 tick，防阻塞任务堆积
        self._manual_refresh_requested = False
        self._empty_tick_count = 0
        self._ai_state_generation = 0
        self._channel_combo = None
        lay = QVBoxLayout(self)
        # 标题行：会话 +（多渠道时）右侧渠道下拉
        top = QHBoxLayout()
        t = QLabel("会话"); t.setObjectName("Title"); top.addWidget(t)
        top.addStretch(1)
        if len(self._channels) >= 2:
            top.addWidget(QLabel("渠道"))
            self._channel_combo = QComboBox()
            for key, label in self._channels:
                self._channel_combo.addItem(label, key)
            self._channel_combo.setCurrentIndex([k for k, _ in self._channels].index(self._channel_key))
            self._channel_combo.currentIndexChanged.connect(self._on_channel_changed)
            top.addWidget(self._channel_combo)
        lay.addLayout(top)
        body = QHBoxLayout(); lay.addLayout(body, 1)
        # 左：微信会话列表
        left = QVBoxLayout(); body.addLayout(left, 1)
        self._list = QListWidget(); self._list.currentRowChanged.connect(self._open_row)
        self._list.itemDoubleClicked.connect(self._open_profile)
        self._list.setIconSize(QSize(32, 32))
        left.addWidget(self._list, 1)
        # ⚠️ 必须用 lambda 丢掉 Qt clicked 信号的 checked bool——直接 connect(self.refresh) 会把
        # 那个 bool 当 refresh 的第一个位置参 sessions 传进来，self._sessions 被写成 False，之后
        # _apply_tick 迭代它就 'bool' object is not iterable，聊天记录页彻底崩（真机 packaged 实测）。
        reload_btn = QPushButton("刷新"); reload_btn.setObjectName("Ghost")
        reload_btn.clicked.connect(self.request_refresh)
        left.addWidget(reload_btn)
        # 右：真实聊天气泡
        right = QVBoxLayout(); body.addLayout(right, 2)
        self._title = QLabel(""); self._title.setObjectName("Muted"); right.addWidget(self._title)
        self._detail_host = QWidget(); self._detail_lay = QVBoxLayout(self._detail_host)
        self._detail_lay.addStretch(1)
        self._scroll = QScrollArea(); self._scroll.setWidgetResizable(True)
        self._scroll.setWidget(self._detail_host)
        right.addWidget(self._scroll, 1)
        self._monitor_switch = QCheckBox("接待此客户(关=不进AI、不留后台记录、不弹窗)")
        self._monitor_switch.setChecked(True)
        self._monitor_switch.setEnabled(False)
        self._monitor_switch.toggled.connect(self._on_toggle_monitor)
        right.addWidget(self._monitor_switch)
        # 会话级 AI 托管开关（关=转人工，开=恢复 AI）——对当前选中客户生效
        self._ai_switch = QCheckBox("AI托管(关=转人工，开=允许AI处理)")
        self._ai_switch.toggled.connect(self._on_toggle_ai)
        right.addWidget(self._ai_switch)
        self._delivery_hint = QLabel("")
        self._delivery_hint.setObjectName("Muted")
        self._delivery_hint.setWordWrap(True)
        right.addWidget(self._delivery_hint)
        self._refresh_delivery_hint()
        # 人工回复：输入框 + 发送（回车或点按钮）
        send_row = QHBoxLayout()
        self._input = QLineEdit(); self._input.setPlaceholderText("输入消息，回车或点发送…")
        self._input.returnPressed.connect(self._on_send)
        send_row.addWidget(self._input, 1)
        self._send_btn = QPushButton("发送"); self._send_btn.setObjectName("Primary")
        self._send_btn.clicked.connect(self._on_send)
        send_row.addWidget(self._send_btn)
        right.addLayout(send_row)
        self._send_status = QLabel(""); self._send_status.setObjectName("Muted")
        right.addWidget(self._send_status)
        self._stick_bottom = True     # 是否黏在底部（打开会话/在底部看新消息时=True；用户上翻看历史时=False）
        bar = self._scroll.verticalScrollBar()
        bar.rangeChanged.connect(self._on_range_changed)   # 内容尺寸变化(气泡布局完成)时贴底
        bar.valueChanged.connect(self._on_scroll_moved)    # 用户上翻离开底部则不再强制贴底
        self._kb_admin_button = QPushButton("后台管理知识库")
        self._kb_admin_button.setObjectName("Primary")
        self._kb_admin_button.setToolTip("打开后台知识库管理，可手动新增、上传或删除知识文档")
        self._kb_admin_button.clicked.connect(self._open_kb_admin)
        right.addWidget(self._kb_admin_button)
        if self.avatars is not None:
            self.avatars.avatar_ready.connect(self._on_avatar_ready)
        self._tick_ready.connect(self._apply_tick)
        self._chat_ready.connect(self._apply_chat_read)
        self._ai_state_ready.connect(self._apply_ai_state)
        if defer_initial_load:
            self._list.addItem("（正在加载会话…）")
        else:
            self.refresh()

    # ---------- 左侧会话列表 ----------
    def refresh(self, sessions=None, names=None, *, select_first: bool = True) -> None:
        # sessions/names 可由后台线程预取传入（live_tick），此时不在 GUI 线程做阻塞读。
        if self.adapter is None:
            self._list.clear()
            self._sessions = []
            self._list.addItem("（未连接微信）")
            return
        # 防御纵深：只接受真正的 list（预取数据）。收到 None / bool / 其它任何非 list 都当「没传」
        # 处理、重新拉取——绝不把非 list 存进 self._sessions，否则后续迭代它会崩（见 line 81 注释）。
        read_from_adapter = not isinstance(sessions, list)
        if read_from_adapter:
            try:
                sessions = self.adapter.list_sessions()
            except Exception as exc:
                # 刷新失败不等于账号没有会话。已有快照时继续显示，避免页面切换/重连窗口
                # 把左侧列表和右侧聊天一起变成空白；首次加载没有快照才显示错误状态。
                if self._sessions:
                    detail = str(exc).strip()
                    self._send_status.setText(
                        "会话刷新暂未完成，继续显示上次内容"
                        + (f"：{detail[:120]}" if detail else "")
                    )
                    return
                detail = str(exc).strip()
                text = "（会话读取未就绪）"
                if detail:
                    text += f"\n{detail[:160]}"
                self._list.clear()
                self._list.addItem(text)
                return
        # 手动刷新时，hook/session.db 偶尔会成功返回一次空列表。已有非空快照时再读一次确认，
        # 避免用户点一下“刷新”就把真实会话替换成“暂无会话”；连续两次都为空才视为真实清空。
        if read_from_adapter and not sessions and self._sessions:
            try:
                confirmed_sessions = self.adapter.list_sessions()
            except Exception as exc:
                detail = str(exc).strip()
                self._send_status.setText(
                    "会话刷新暂未完成，继续显示上次内容"
                    + (f"：{detail[:120]}" if detail else "")
                )
                return
            if not isinstance(confirmed_sessions, list):
                self._send_status.setText("会话刷新暂未完成，继续显示上次内容")
                return
            sessions = confirmed_sessions
        self._list.clear()
        self._sessions = sessions
        if not self._sessions:
            reason = str(getattr(self.adapter, "reason", "") or "").strip()
            self._list.addItem(f"（微信连接不可用）\n{reason}" if reason else "（暂无会话）")
            return
        self._names = names if names is not None else self._resolve_names(
            [s["wxid"] for s in self._sessions])
        for s in self._sessions:
            name = self._names.get(s["wxid"], s["wxid"])
            item = QListWidgetItem(f"{name}\n{s.get('summary', '')[:24]}")
            if self.avatars is not None:
                item.setIcon(QIcon(self.avatars.get_pixmap(s["wxid"], name)))
            self._list.addItem(item)
        # 启动进入“会话”页时直接展示最近一位客户的聊天，不要求用户先去待人工回复或手动点行。
        # 刷新时若当前会话仍存在则保持它，否则回到最近会话（SessionTable 已按时间倒序）。
        if select_first:
            target = self._index_for_contact(self._current_wxid)
            self._list.setCurrentRow(target if target is not None else 0)

    def request_refresh(self) -> None:
        """手动刷新也走后台单飞读取，不在 GUI 线程等待 hook/本地数据库。"""
        if not self._sessions and self._list.count():
            self._list.item(0).setText("（正在加载会话…）")
        self._send_status.setText("正在后台刷新会话和消息…")
        self._manual_refresh_requested = True
        self.live_tick()

    def _resolve_names(self, wxids: list[str]) -> dict:
        if self.adapter is None:
            return {w: w for w in wxids}
        try:
            return self.adapter.display_names(wxids)
        except Exception:
            return {w: w for w in wxids}

    def _display_name(self, wxid: str) -> str:
        cached = getattr(self, "_names", {}).get(wxid)
        return cached or self._resolve_names([wxid]).get(wxid, wxid)

    def _index_for_contact(self, contact_id: str) -> int | None:
        for i, s in enumerate(self._sessions):
            if s.get("wxid") == contact_id:
                return i
        return None

    def select_contact(self, contact_id: str) -> bool:
        """定位并异步打开联系人聊天，任何 hook/本地库读取都不阻塞 GUI。"""
        self._stick_bottom = True            # 打开会话默认看最新（贴底）
        idx = self._index_for_contact(contact_id)
        if idx is None:
            self.request_refresh()
            idx = self._index_for_contact(contact_id)
        if idx is not None:
            if self._list.currentRow() == idx:
                self._request_chat(contact_id)  # 已在该行(setCurrentRow 不触发)：主动后台刷新
            else:
                self._list.setCurrentRow(idx)
            return True
        self._request_chat(contact_id)       # 会话列表里没有（如刚双击的好友），仍异步读取
        return True

    def _open_row(self, i: int) -> None:
        if 0 <= i < len(self._sessions):
            self._stick_bottom = True        # 手动点开一个会话：看最新
            self._request_chat(self._sessions[i]["wxid"])

    def _on_avatar_ready(self, wxid: str) -> None:
        for i, s in enumerate(self._sessions):
            if s.get("wxid") == wxid:
                name = self._names.get(wxid, wxid)
                self._list.item(i).setIcon(QIcon(self.avatars.get_pixmap(wxid, name)))
        # 会话气泡头像与左侧列表共用同一个异步 AvatarProvider；字节到达后只替换 pixmap，
        # 不重读聊天、不重建整页，因此不会打断滚动位置或在 GUI 线程再次查询 hook。
        for label, name in self._message_avatar_labels.get(wxid, []):
            label.setPixmap(self.avatars.get_pixmap(wxid, name))

    # ---------- 渠道切换（个人微信 / 企微） ----------
    def switch_to_channel(self, key: str) -> None:
        """外部（实例控制台「看会话」）请求切到某渠道视图。渠道不存在则忽略。"""
        if self._channels and key in [k for k, _ in self._channels]:
            self._switch_channel(key)

    def _switch_channel(self, key: str) -> None:
        """切到某渠道：换 adapter + 同步下拉 + 清空当前选中/会话（不 refresh，调用方决定何时刷）。"""
        keys = [k for k, _ in self._channels]
        if self._adapter_for is None or key not in keys or key == self._channel_key:
            return
        self._channel_key = key
        self.adapter = self._adapter_for(key)
        if self._channel_combo is not None:
            self._channel_combo.blockSignals(True)
            self._channel_combo.setCurrentIndex(keys.index(key))
            self._channel_combo.blockSignals(False)
        self._current_wxid = ""
        self._sessions = []
        self._names = {}
        self._list.clear()
        self._list.addItem("（正在加载会话…）")
        self._chat_generation += 1           # 丢弃旧渠道尚未返回的聊天读取结果
        self._last_active = ""
        self._last_msgs = []
        self._title.setText("")
        self._clear_detail()
        self._refresh_ai_switch()
        self._refresh_monitor_switch()

    def _on_channel_changed(self, idx: int) -> None:
        if 0 <= idx < len(self._channels):
            self._switch_channel(self._channels[idx][0])
            self.request_refresh()

    def channel_reconnected(self, channel_key: str) -> None:
        """某渠道 adapter 重连（如个人微信 hook 恢复）：正显示该渠道时换用新 adapter 并刷新。"""
        if self._adapter_for is None or channel_key != self._channel_key:
            return
        self._chat_generation += 1           # 旧 adapter 的后台结果不可落到新连接
        self.adapter = self._adapter_for(channel_key)
        self.request_refresh()

    # ---------- 右侧真实聊天 + AI/人工标记 ----------
    def _channel(self) -> str:
        return getattr(self.adapter, "channel", "") if self.adapter is not None else ""

    def _refresh_ai_switch(self) -> None:
        """按当前客户读取 AI 托管状态刷新开关（后端不可达/无 contact → 默认托管中，不打扰）。"""
        muted = False
        if self._current_wxid:
            try:
                muted = bool(self.bridge.get_ai_mute(self._channel(), self._current_wxid))
            except Exception:
                muted = False
        self._ai_switch.blockSignals(True)
        self._ai_switch.setChecked(not muted)     # 勾选=托管中(AI 开)；取消=转人工
        self._ai_switch.setEnabled(bool(self._current_wxid))
        self._ai_switch.blockSignals(False)

    def _refresh_ai_switch_async(self) -> None:
        """后台读取会话 AI 托管状态，避免后端慢/未启动时阻塞聊天气泡首屏。"""
        contact_id = self._current_wxid
        channel = self._channel()
        self._ai_state_generation += 1
        generation = self._ai_state_generation
        if not contact_id:
            self._refresh_ai_switch()
            return

        # 状态回来前不允许用户基于上一位客户的旧开关值操作；聊天内容本身立即渲染。
        self._ai_switch.blockSignals(True)
        self._ai_switch.setEnabled(False)
        self._ai_switch.blockSignals(False)

        def worker() -> None:
            try:
                muted = bool(self.bridge.get_ai_mute(channel, contact_id))
            except Exception:
                muted = False
            self._ai_state_ready.emit({
                "generation": generation,
                "channel": channel,
                "contact_id": contact_id,
                "muted": muted,
            })

        threading.Thread(
            target=worker, daemon=True, name="history-ai-state"
        ).start()

    def _apply_ai_state(self, data: dict) -> None:
        if (
            data.get("generation") != self._ai_state_generation
            or data.get("contact_id") != self._current_wxid
            or data.get("channel") != self._channel()
        ):
            return
        self._ai_switch.blockSignals(True)
        self._ai_switch.setChecked(not bool(data.get("muted")))
        self._ai_switch.setEnabled(True)
        self._ai_switch.blockSignals(False)
        self._refresh_delivery_hint()

    def _refresh_monitor_switch(self) -> None:
        enabled = True
        if self.state is not None and self._current_wxid:
            enabled = self.state.is_conversation_enabled(self._channel(), self._current_wxid)
        self._monitor_switch.blockSignals(True)
        self._monitor_switch.setChecked(enabled)
        self._monitor_switch.setEnabled(self.state is not None and bool(self._current_wxid))
        self._monitor_switch.blockSignals(False)
        self._refresh_delivery_hint()

    def _refresh_delivery_hint(self) -> None:
        blockers: list[str] = []
        if not self._current_wxid:
            blockers.append("先选择一个会话")
        elif not self._monitor_switch.isChecked():
            blockers.append("①“接待此客户”未开启")
        if self._current_wxid and not self._ai_switch.isChecked():
            blockers.append("②“AI 托管”未开启")
        if self.state is None or not self.state.cfg.auto_send:
            blockers.append("③“自动发送总闸”未开启")

        if not blockers:
            self._delivery_hint.setText("自动回复已就绪：知识库明确命中时会发给微信，无法确认时转人工")
            self._delivery_hint.setStyleSheet("color:#138a72;")
        else:
            self._delivery_hint.setText(
                "暂不会自动回复：" + "；".join(blockers) + "。"
                "自动发送总闸在“状态设置”中。"
            )
            self._delivery_hint.setStyleSheet("color:#b26a00;")

    def _on_toggle_monitor(self, checked: bool) -> None:
        if self.state is None or not self._current_wxid:
            return
        try:
            self.state.set_conversation_enabled(self._channel(), self._current_wxid, checked)
            self._send_status.setText(
                "已接待该客户的后续消息" if checked
                else "已忽略该客户的后续消息：不进 AI、不留后台记录、不弹窗"
            )
            self._refresh_delivery_hint()
        except Exception as e:                    # noqa: BLE001
            self._send_status.setText(f"保存接待设置失败：{e}")
            self._refresh_monitor_switch()

    def _on_toggle_ai(self, checked: bool) -> None:
        """会话级 AI 托管：勾=恢复 AI(取消静音)，取消勾=转人工(静音)。"""
        if not self._current_wxid:
            return
        try:
            self.bridge.set_ai_mute(self._channel(), self._current_wxid, not checked)
            self._send_status.setText("已" + ("恢复 AI 托管" if checked else "转为人工"))
            self._refresh_delivery_hint()
        except Exception as e:                    # noqa: BLE001
            self._send_status.setText(f"切换失败：{e}")
            self._refresh_ai_switch()
            self._refresh_delivery_hint()

    def _on_send(self) -> None:
        """人工回复当前客户：优先经 controller 按渠道路由+释放待人工，否则直发 adapter。"""
        if not self._current_wxid:
            return
        text = self._input.text().strip()
        if not text:
            return
        try:
            if self.controller is not None:
                ok = bool(self.controller.send(self._current_wxid, text, self._channel()))
            else:
                res = self.adapter.send_message(self._current_wxid, text, provenance="human")
                ok = bool(res.get("ok")) if isinstance(res, dict) else bool(getattr(res, "ok", False))
        except Exception as e:                    # noqa: BLE001
            self._send_status.setText(f"发送失败：{e}"); return
        if ok:
            self._input.clear()
            self._send_status.setText("已发送")
            self._stick_bottom = True
            self._request_chat(self._current_wxid)
        else:
            self._send_status.setText("发送失败")

    def _conversation_cache_key(self, contact_id: str) -> tuple[str, str]:
        channel = self._channel_key or self._channel()
        return channel, contact_id

    def _show_chat_notice(self, contact_id: str, text: str) -> None:
        """后台读取期间立即显示状态，避免右侧消息区长时间空白。"""
        self._current_wxid = contact_id
        # 点击路径必须保持无阻塞；名称若尚未预取，先显示 wxid，下一轮会话刷新再补齐。
        self._title.setText(f"与 {self._names.get(contact_id, contact_id)} 的聊天")
        self._refresh_ai_switch_async()
        self._refresh_monitor_switch()
        self._clear_detail()
        notice = QLabel(text)
        notice.setObjectName("Muted")
        notice.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._detail_lay.insertWidget(0, notice)

    def _request_chat(self, contact_id: str) -> None:
        """优先显示缓存，并行读取客服快照与微信本机历史。"""
        if not contact_id:
            return
        adapter = self.adapter
        if adapter is None:
            self._show_chat_notice(contact_id, "微信尚未连接")
            return

        self._chat_generation += 1
        generation = self._chat_generation
        self._chat_futures = [future for future in self._chat_futures
                              if not future.cancel() and not future.done()]
        self._chat_pending = {generation: {"backend", "adapter"}}
        self._chat_snapshots = {generation: {}}
        channel_key = self._channel_key or self._channel()
        cached = self._conversation_cache.get((channel_key, contact_id))
        if cached:
            self._render_chat(contact_id, msgs=cached)
            self._send_status.setText("正在后台刷新聊天记录…")
        else:
            self._show_chat_notice(contact_id, "正在加载聊天记录…")
            self._send_status.setText("正在加载聊天记录…")

        def emit_result(source: str, msgs, *, error: str = "") -> None:
            try:
                self._chat_ready.emit({
                    "generation": generation,
                    "adapter": adapter,
                    "channel_key": channel_key,
                    "contact_id": contact_id,
                    "source": source,
                    "msgs": msgs,
                    "error": error,
                    "backend_fallback": source == "backend" and bool(msgs),
                })
            except RuntimeError:
                return                          # 页面已销毁，丢弃迟到结果

        def adapter_worker() -> None:
            if generation != self._chat_generation:
                return
            error = ""
            try:
                msgs = adapter.read_conversation(contact_id)
                if not isinstance(msgs, list):
                    msgs = None
            except Exception as exc:
                msgs = None
                error = str(exc).strip()
            emit_result("adapter", msgs, error=error)

        def backend_worker() -> None:
            if generation != self._chat_generation:
                return
            saved = self._load_backend_messages(channel_key, contact_id)
            emit_result("backend", saved)

        self._chat_futures.extend((
            self._chat_executor.submit(backend_worker),
            self._chat_executor.submit(adapter_worker),
        ))

    def _apply_chat_read(self, data: dict) -> None:
        """只接收最新选中会话的结果，丢弃快速切换产生的迟到数据。"""
        if (
            data.get("generation") != self._chat_generation
            or data.get("adapter") is not self.adapter
            or data.get("channel_key") != (self._channel_key or self._channel())
            or data.get("contact_id") != self._current_wxid
        ):
            self._chat_pending.pop(int(data.get("generation") or 0), None)
            return
        generation = int(data.get("generation") or 0)
        source = str(data.get("source") or "adapter")
        pending = self._chat_pending.setdefault(generation, {"backend", "adapter"})
        pending.discard(source)
        contact_id = str(data.get("contact_id") or "")
        msgs = data.get("msgs")
        cached = self._conversation_cache.get(self._conversation_cache_key(contact_id))
        if msgs:
            snapshots = self._chat_snapshots.setdefault(generation, {})
            snapshots[source] = list(msgs)
            if source == "adapter":
                self._chat_local_applied_generation = generation
            local_messages = snapshots.get("adapter", [])
            backend_messages = snapshots.get("backend", [])
            msgs = merge_history_snapshots(local_messages, backend_messages)
            self._send_status.setText(
                "微信本机历史暂未就绪，已显示客服系统记录"
                if backend_messages and not local_messages else ""
            )
            self._render_chat(contact_id, msgs=msgs)
            return
        if cached or self._conversation_cache.get(self._conversation_cache_key(contact_id)):
            if pending:
                return
            self._send_status.setText("聊天刷新暂未完成，继续显示上次内容")
            return
        if pending:
            return
        detail = str(data.get("error") or "").strip()
        notice = "暂未读取到聊天记录，请稍后重试"
        if detail:
            notice += f"\n{detail[:160]}"
        self._show_chat_notice(contact_id, notice)
        self._send_status.setText("聊天记录暂未就绪")

    def _render_chat(self, contact_id: str, msgs=None) -> None:
        if msgs is None:
            self._request_chat(contact_id)
            return
        same_contact = contact_id == self._current_wxid
        self._current_wxid = contact_id
        self._title.setText(f"与 {self._names.get(contact_id, contact_id)} 的聊天")
        self._refresh_ai_switch_async()
        self._refresh_monitor_switch()
        if self.adapter is None:
            self._clear_detail(); return
        # hook 在数据库切换/微信忙碌的短窗口里会成功返回空列表。对于已经展示过的同一
        # 会话，空结果不能证明聊天真的被清空，因此保留最后一次非空快照，下一轮再重试。
        if not msgs and same_contact and self._last_msgs:
            self._send_status.setText("聊天正在刷新，继续显示上次内容")
            return
        cache_key = self._conversation_cache_key(contact_id)
        msgs = merge_live_messages(msgs, self._live_messages.get(cache_key, []))
        if msgs:
            self._conversation_cache[cache_key] = list(msgs)
        self._clear_detail()
        self._last_msgs = [message_signature(m) for m in msgs]   # 媒体/卡片变化也触发实时重绘
        # 出向来源=挂件发件账本的溯源标记（发时即知；取代原先"拿文本去后端 AI 集/群发 JSON 事后猜"，
        # 省掉每次渲染/每 3 秒 live_tick 的 2 次后端 HTTP + 群发文件读）。账本是会话内存：
        # 挂件重启前发的、或直接在微信里手打的，provenance_for 返回 None → 落"人工"（诚实默认）。
        prov_for = getattr(self.adapter, "provenance_for", None)
        avatar_provider = self.avatars if self._channel().split("#")[0] in {
            "wechat_personal", "wechat"} else None
        for m in msgs:
            if not m["is_self"]:
                sender = "customer"
            else:
                src = prov_for(str(m.get("text", ""))) if prov_for else None
                live_source = str(m.get("provenance") or "")
                sender = live_source if live_source in ("ai", "broadcast") else (
                    src if src in ("ai", "broadcast") else "agent"
                )
            row = _bubble_row(m, sender, bool(m["is_self"]), avatar_provider)
            avatar_label = getattr(row, "_avatar_label", None)
            avatar_wxid = str(getattr(row, "_avatar_wxid", "") or "")
            avatar_name = str(getattr(row, "_avatar_name", "") or "")
            if avatar_label is not None and avatar_wxid:
                self._message_avatar_labels.setdefault(avatar_wxid, []).append(
                    (avatar_label, avatar_name))
            self._detail_lay.insertWidget(self._detail_lay.count() - 1, row)
        # 若当前在底部（打开会话或正看最新），布局完成后 rangeChanged 会把它稳稳贴到底；
        # 用户上翻看历史时(_stick_bottom=False)则不动，实时刷新不打断阅读。
        if self._stick_bottom:
            self._scroll_to_bottom()

    def _scroll_to_bottom(self) -> None:
        bar = self._scroll.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _on_range_changed(self, _min: int, _max: int) -> None:
        if self._stick_bottom:
            self._scroll.verticalScrollBar().setValue(_max)

    def _on_scroll_moved(self, val: int) -> None:
        bar = self._scroll.verticalScrollBar()
        self._stick_bottom = val >= bar.maximum() - 8    # 贴到底部±8px 才继续自动贴底

    def _clear_detail(self) -> None:
        self._message_avatar_labels.clear()
        while self._detail_lay.count() > 1:      # 保留末尾 stretch
            item = self._detail_lay.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    # ---------- 跟随 ----------
    def note_incoming(self, contact_id: str, channel: str = "") -> None:
        """收到某联系人新消息：跟到 ta、刷新会话列表并弹到前台。
        这是最可靠的『跟随』触发（挂件轮询到的消息 100% 可靠，不依赖微信是否清未读）。
        消息来自别的渠道时先切过去，保证跟随到正确渠道的联系人。"""
        if not contact_id:
            return
        self.apply_live_event({
            "event_id": f"legacy-in:{channel}:{contact_id}",
            "direction": "inbound", "channel": channel,
            "contact_id": contact_id, "sender_id": contact_id,
            "text": "", "timestamp": 0,
        })
        if self.on_followed:
            self.on_followed()

    def apply_live_event(self, event: dict) -> None:
        """把收发事件直接叠到界面；无需等待 session.db/message.db 完成落盘。"""
        contact_id = str(event.get("contact_id") or "")
        channel = str(event.get("channel") or self._channel_key or self._channel())
        if not contact_id:
            return
        key = (channel, contact_id)
        message = event_to_history_message(event)
        if message.get("text"):
            live = self._live_messages.setdefault(key, [])
            event_id = str(message.get("live_event_id") or "")
            if not any(str(item.get("live_event_id") or "") == event_id for item in live):
                live.append(message)
                del live[:-50]
        if channel != (self._channel_key or self._channel()):
            return

        existing = next((dict(row) for row in self._sessions if row.get("wxid") == contact_id), None)
        session = existing or {"wxid": contact_id, "summary": "", "ts": 0}
        if message.get("text"):
            session["summary"] = message["text"]
        session["ts"] = max(int(session.get("ts") or 0), int(message.get("ts") or 0))
        sessions = [session] + [row for row in self._sessions if row.get("wxid") != contact_id]
        selected = self._current_wxid
        self.refresh(sessions=sessions, names=self._names, select_first=False)
        if selected:
            idx = self._index_for_contact(selected)
            if idx is not None:
                self._list.blockSignals(True)
                self._list.setCurrentRow(idx)
                self._list.blockSignals(False)
        if not self._current_wxid:
            self._current_wxid = contact_id
            idx = self._index_for_contact(contact_id)
            if idx is not None:
                self._list.blockSignals(True)
                self._list.setCurrentRow(idx)
                self._list.blockSignals(False)
        if self._current_wxid == contact_id and message.get("text"):
            stored = self._conversation_cache.get(key, [])
            self._stick_bottom = True
            self._render_chat(contact_id, merge_live_messages(stored, [message]))
            self._send_status.setText("已实时更新；后台正在同步本机聊天记录")

    def follow_active(self) -> None:
        """跟随微信当前会话：仅当微信里活跃会话**发生变化**时才切过去——
        这样用户在挂件里手动点开的会话不会被每次轮询强行拽走。"""
        if self.adapter is None:
            return
        try:
            wxid = self.adapter.active_session()
        except Exception:
            return
        if wxid and wxid != self._last_active:
            self._last_active = wxid
            self.select_contact(wxid)
            if self.on_followed:                 # 通知外层把聊天记录页切到前台，否则跟随了也看不见
                self.on_followed()

    def activate(self) -> None:
        """返回会话中心时保留当前快照，只在后台检查新会话和新消息。"""
        self.live_tick()

    def memory_snapshot(self) -> dict:
        """给记忆对话页复用已加载数据；返回副本，避免两个页面互相修改状态。"""
        channel = self._channel_key or self._channel()
        contact_id = self._current_wxid
        messages = self._conversation_cache.get((channel, contact_id), []) if contact_id else []
        return {
            "channel": channel,
            "sessions": [dict(row) for row in self._sessions],
            "names": dict(self._names),
            "contact_id": contact_id,
            "messages": [dict(row) for row in messages],
        }

    @staticmethod
    def _saved_timestamp(value) -> int:
        if isinstance(value, (int, float)):
            return int(value)
        text = str(value or "").strip()
        if not text:
            return 0
        try:
            return int(dt.datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp())
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _saved_int(value) -> int:
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    def _load_backend_sessions(self, channel_key: str) -> list[dict] | None:
        """在个人微信 QueryDB 不可用时，读取本租户后端已保存的会话快照。"""
        try:
            rows = self.bridge.list_conversations(limit=100)
        except Exception:
            return None
        if not isinstance(rows, list):
            return None
        sessions: list[dict] = []
        seen: set[str] = set()
        for row in rows:
            if not isinstance(row, dict) or str(row.get("channel") or "") != channel_key:
                continue
            contact_id = str(row.get("contact_id") or "").strip()
            if not contact_id or contact_id in seen:
                continue
            seen.add(contact_id)
            sessions.append({
                "wxid": contact_id,
                "summary": str(row.get("last_text") or ""),
                "ts": self._saved_timestamp(row.get("last_at")),
                "backend_conversation_id": self._saved_int(row.get("id")),
            })
            if len(sessions) >= 40:
                break
        return sessions

    def _load_backend_messages(
        self,
        channel_key: str,
        contact_id: str,
        conversation_id: int = 0,
    ) -> list[dict] | None:
        """读取本租户后端消息并归一成聊天气泡格式；失败返回 None。"""
        conv_id = self._saved_int(conversation_id)
        if not conv_id:
            sessions = self._load_backend_sessions(channel_key)
            match = next(
                (row for row in sessions or [] if row.get("wxid") == contact_id),
                None,
            )
            conv_id = self._saved_int((match or {}).get("backend_conversation_id"))
        if not conv_id:
            return None
        try:
            detail = self.bridge.get_conversation(conv_id)
        except Exception:
            return None
        if not isinstance(detail, dict):
            return None
        if (
            str(detail.get("channel") or "") != channel_key
            or str(detail.get("contact_id") or "") != contact_id
        ):
            return None
        messages = detail.get("messages")
        if not isinstance(messages, list):
            return None
        normalized: list[dict] = []
        for message in messages:
            if not isinstance(message, dict):
                continue
            text = str(message.get("text") or "")
            if not text:
                continue
            direction = str(message.get("direction") or "").lower()
            is_self = direction in {"out", "outbound"}
            sender = str(message.get("sender") or "")
            provenance = str(message.get("provenance") or sender)
            delivery_status = str(message.get("delivery_status") or "")
            if (
                is_self
                and provenance in {"ai", "handoff"}
                and delivery_status != "delivered"
            ):
                continue
            normalized.append({
                "kind": "text",
                "local_id": self._saved_int(message.get("id")),
                "text": text,
                "sender_id": "self" if is_self else contact_id,
                "sender_name": "我" if is_self else contact_id,
                "is_self": is_self,
                "is_group": contact_id.endswith("@chatroom"),
                "ts": self._saved_timestamp(message.get("created_at")),
                "provenance": provenance if provenance in {"ai", "broadcast"} else "",
                "delivery_status": delivery_status,
            })
        return normalized

    def live_tick(self) -> None:
        """每 3s 的实时刷新入口。**所有可能阻塞的 hook/本地库读取都放到后台线程**，读完用
        `_tick_ready` 信号回到 GUI 线程渲染——否则个人微信 hook client 超时可长达数秒，三个串行
        同步读会把 GUI 卡死（客户反馈的『概率卡死、读不出聊天记录』根因）。

        `_tick_busy` 单飞：上一次后台读没回来就跳过本次，避免慢读堆积。"""
        self._refresh_delivery_hint()             # 状态页切换自动发送后，会话页无需重开即可看到
        if self.adapter is None or self._tick_busy:
            return
        adapter = self.adapter
        cur = self._current_wxid
        last_active = self._last_active
        cached_names = dict(self._names)
        channel_key = self._channel_key or self._channel()
        manual_refresh = self._manual_refresh_requested
        self._manual_refresh_requested = False
        self._tick_busy = True

        def worker() -> None:
            data: dict = {
                "adapter": adapter, "cur": cur, "final": True,
                "manual_refresh": manual_refresh,
            }
            try:
                sessions = adapter.list_sessions()
                if not isinstance(sessions, list):
                    sessions = None
                elif sessions:
                    sessions = [
                        session for session in sessions
                        if isinstance(session, dict) and session.get("wxid")
                    ]
                data["sessions"] = sessions
            except Exception as exc:
                data["sessions"] = None
                data["session_error"] = str(exc).strip()
                saved_sessions = self._load_backend_sessions(channel_key)
                if saved_sessions:
                    data["sessions"] = saved_sessions
                    data["backend_fallback"] = True

            if manual_refresh and data.get("sessions") == []:
                try:
                    confirmed = adapter.list_sessions()
                    if isinstance(confirmed, list):
                        data["sessions"] = confirmed
                        data["confirmed_empty"] = not confirmed
                except Exception as exc:
                    data["sessions"] = None
                    data["session_error"] = str(exc).strip()

            sessions = data.get("sessions")
            wxids = [str(s.get("wxid", "")) for s in sessions or [] if s.get("wxid")]
            quick_names = {wxid: cached_names.get(wxid, wxid) for wxid in wxids}
            data["names"] = quick_names
            # 首屏分阶段交付：SessionTable 一读完就先显示左侧列表，不再等待联系人名、
            # 活跃会话探测和完整聊天读取全部串行完成。
            if sessions is not None:
                self._tick_ready.emit({
                    **data,
                    "phase": "sessions",
                    "final": False,
                    "active": None,
                    "target": None,
                    "msgs": None,
                })

            # 首次进入优先读最近会话；返回页面优先刷新当前会话。活跃会话探测放到首屏
            # 消息之后，避免它的一次 QueryDB 超时阻塞用户真正要看的聊天内容。
            target = cur
            if not target and sessions:
                target = str(sessions[0].get("wxid", "") or "")
            data["target"] = target
            backend_id = next((
                self._saved_int(session.get("backend_conversation_id"))
                for session in sessions or []
                if str(session.get("wxid") or "") == target
            ), 0)
            if target and data.get("backend_fallback"):
                data["msgs"] = self._load_backend_messages(
                    channel_key, target, backend_id,
                )
                data["message_fallback"] = data["msgs"] is not None
            else:
                try:
                    data["msgs"] = adapter.read_conversation(target) if target else None
                except Exception:
                    data["msgs"] = None
                if target and not data.get("msgs"):
                    saved_messages = self._load_backend_messages(channel_key, target)
                    if saved_messages:
                        data["msgs"] = saved_messages
                        data["message_fallback"] = True
            if target:
                self._tick_ready.emit({
                    **data,
                    "phase": "messages",
                    "final": False,
                    "active": None,
                })

            data["names"] = self._resolve_names_via(adapter, wxids)
            try:
                data["active"] = adapter.active_session()
            except Exception:
                data["active"] = None
            active = data.get("active")
            if active and active != last_active and active != target:
                data["target"] = active
                try:
                    data["msgs"] = adapter.read_conversation(active)
                except Exception:
                    data["msgs"] = None
            data["phase"] = "complete"
            self._tick_ready.emit(data)

        threading.Thread(target=worker, daemon=True, name="history-live-tick").start()

    @staticmethod
    def _resolve_names_via(adapter, wxids: list[str]) -> dict:
        try:
            return adapter.display_names(wxids)
        except Exception:
            return {w: w for w in wxids}

    def _apply_tick(self, data: dict) -> None:
        """GUI 线程：用后台线程预取好的数据渲染。绝不在这里做阻塞读（跟随的新会话内容除外，
        那是偶发且已被短超时兜住）。"""
        final = bool(data.get("final", True))
        if final:
            self._tick_busy = False
        if data.get("adapter") is not self.adapter:
            return                              # 后台读期间用户切了渠道 → 丢弃过期结果
        # ① 左侧会话列表成员变化 → 只用后台预取数据刷新。禁止 currentRowChanged 在 GUI
        # 线程回调 read_conversation；目标聊天也已由同一 worker 预取，下面再统一渲染。
        sessions = data.get("sessions")
        if isinstance(sessions, list):
            session_by_id = {
                str(session.get("wxid") or ""): dict(session)
                for session in sessions if isinstance(session, dict) and session.get("wxid")
            }
            channel = self._channel_key or self._channel()
            for (event_channel, contact_id), live in self._live_messages.items():
                if event_channel != channel or not live or contact_id in session_by_id:
                    continue
                latest = live[-1]
                session_by_id[contact_id] = {
                    "wxid": contact_id,
                    "summary": latest.get("text", ""),
                    "ts": int(latest.get("ts") or 0),
                }
            sessions = list(session_by_id.values())
            sessions.sort(key=lambda row: int(row.get("ts") or 0), reverse=True)
        if sessions:
            self._empty_tick_count = 0
        elif final and sessions == [] and not self._sessions:
            self._empty_tick_count += 1
        # hook 在收消息/自动回复的极短窗口里，归属闸或 session.db 可能给出一次成功但为空的结果。
        # 后台轮询无法区分它和“账号确实没有会话”，因此已有列表时保留最后一次非空快照；首次加载、
        # 手动刷新和渠道切换仍走 refresh()，真实空账号仍会正确显示“暂无会话”。
        current_ids = [s["wxid"] for s in self._sessions]
        next_ids = [s["wxid"] for s in sessions] if sessions is not None else current_ids
        next_names = data.get("names") if isinstance(data.get("names"), dict) else self._names
        names_changed = bool(sessions) and any(
            self._names.get(wxid) != next_names.get(wxid) for wxid in next_ids
        )
        sessions_changed = bool(sessions) and sessions != self._sessions
        if sessions and (sessions_changed or names_changed):
            selected_contact = self._current_wxid
            self.refresh(sessions=sessions, names=next_names, select_first=False)
            selected_idx = self._index_for_contact(selected_contact)
            if selected_idx is not None:
                self._list.blockSignals(True)
                self._list.setCurrentRow(selected_idx)
                self._list.blockSignals(False)
                self._title.setText(f"与 {self._display_name(selected_contact)} 的聊天")
        elif final and data.get("confirmed_empty") and not sessions:
            self.refresh(sessions=[], names={}, select_first=False)
        elif final and sessions == [] and not self._sessions and self._empty_tick_count >= 2:
            self.refresh(sessions=[], names={}, select_first=False)
        elif final and sessions is None and not self._sessions:
            detail = str(data.get("session_error", "") or "").strip()
            self._list.clear()
            self._list.addItem(
                "（会话读取未就绪）" + (f"\n{detail[:160]}" if detail else "")
            )

        # ② 跟随微信活跃会话，或刷新当前/首个会话。这里只渲染 worker 带回的 msgs，绝不
        # 从 GUI 线程重新访问 hook。用户在 worker 期间手动换了联系人时，旧 cur 结果会被丢弃。
        active = data.get("active")
        active_changed = bool(active and active != self._last_active)
        if active_changed:
            self._last_active = active
        cur = data.get("cur")
        target = data.get("target")
        msgs = data.get("msgs")
        initial_target = not cur and not self._current_wxid and bool(target)
        current_target = bool(cur and cur == self._current_wxid and target == cur)
        followed_target = bool(active_changed and target == active)
        should_render = msgs is not None and (initial_target or followed_target)
        if current_target and msgs:
            cached = self._conversation_cache.get(self._conversation_cache_key(target), [])
            if cached:
                msgs = merge_history_snapshots(msgs, cached)
            should_render = [message_signature(m) for m in msgs] != self._last_msgs
        if should_render:
            idx = self._index_for_contact(target)
            if idx is not None:
                self._list.blockSignals(True)
                self._list.setCurrentRow(idx)
                self._list.blockSignals(False)
            self._render_chat(target, msgs=msgs)
        if data.get("backend_fallback") or data.get("message_fallback"):
            self._send_status.setText("微信本机历史暂未就绪，已显示客服系统记录")
        elif final and sessions is not None and not data.get("session_error"):
            self._send_status.setText("")
        if active_changed and self.on_followed:
            self.on_followed()

    def _open_profile(self, item) -> None:
        idx = self._list.row(item)
        if not (0 <= idx < len(self._sessions)):
            return
        wxid = self._sessions[idx]["wxid"]
        try:
            prof = self.bridge.get_customer_profile(self._channel(), wxid)
        except BridgeError:
            QMessageBox.warning(self, "客户资料", "读取失败，请稍后再试")
            return
        if self._profile_dialog is not None:        # 关掉上一个，避免连续双击叠出多个窗
            self._profile_dialog.close()
        self._profile_dialog = CustomerProfileDialog(prof, self._display_name(wxid), self)
        self._profile_dialog.show()

    def _open_kb_admin(self) -> None:
        """Open the tenant-aware backend UI for deliberate knowledge management."""
        base_url = str(getattr(self.bridge.cfg, "backend_base_url", "") or "").strip()
        if not base_url:
            QMessageBox.warning(self, "知识库管理", "未配置后台服务地址")
            return
        admin_url = QUrl(f"{base_url.rstrip('/')}/admin/kb")
        if not QDesktopServices.openUrl(admin_url):
            QMessageBox.warning(
                self,
                "知识库管理",
                f"无法打开浏览器，请手动访问：{admin_url.toString()}",
            )

    def closeEvent(self, event) -> None:
        self._chat_generation += 1
        self._chat_executor.shutdown(wait=False, cancel_futures=True)
        super().closeEvent(event)


def _bubble_row(message: dict, sender: str, is_self: bool, avatar_provider=None) -> QWidget:
    source_label, color = _SOURCE_TAG.get(sender, (sender, "#888888"))
    sender_id = str(message.get("sender_id", "") or "")
    sender_name = str(message.get("sender_name", "") or ("我" if is_self else sender_id or "客户"))
    badge_text = f"我 · {source_label}" if is_self else sender_name
    tag = QLabel(badge_text[:32])
    tag.setToolTip(badge_text)
    tag.setStyleSheet(f"color: white; background: {color}; border-radius: 6px; "
                      f"padding: 1px 6px; font-size: 11px;")
    bubble = message_content_widget(message)
    content = QWidget(); content_lay = QVBoxLayout(content)
    content_lay.setContentsMargins(0, 0, 0, 0); content_lay.setSpacing(3)
    content_lay.addWidget(tag, 0, Qt.AlignmentFlag.AlignRight if is_self else Qt.AlignmentFlag.AlignLeft)
    content_lay.addWidget(bubble)

    avatar = QLabel(); avatar.setFixedSize(36, 36)
    avatar.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter)
    show_avatar = avatar_provider is not None and bool(sender_id)
    if show_avatar:
        avatar.setPixmap(avatar_provider.get_pixmap(sender_id, sender_name))

    row = QWidget(); rl = QHBoxLayout(row); rl.setContentsMargins(0, 0, 0, 0); rl.setSpacing(6)
    if not is_self:                              # 对方：头像、具体姓名、消息从左到右
        if show_avatar:
            rl.addWidget(avatar, 0, Qt.AlignmentFlag.AlignTop)
        rl.addWidget(content); rl.addStretch(1)
    else:                                        # 我方：来源标记与头像均在右侧
        rl.addStretch(1); rl.addWidget(content)
        if show_avatar:
            rl.addWidget(avatar, 0, Qt.AlignmentFlag.AlignTop)
    row._avatar_label = avatar if show_avatar else None
    row._avatar_wxid = sender_id
    row._avatar_name = sender_name
    return row
