from __future__ import annotations
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QButtonGroup, QFrame, QHBoxLayout, QLabel, QPushButton,
                               QStackedWidget, QVBoxLayout, QWidget)
from PySide6.QtGui import QIcon
from widget.ui.app_icon import brand_mark_pixmap, load_app_icon
from widget.ui.pages.handoff_page import HandoffPage
from widget.ui.pages.history_page import HistoryPage
from widget.ui.pages.memory_page import MemoryPage
from widget.ui.pages.long_term_memory_page import LongTermMemoryPage
from widget.ui.pages.memory_conversation_page import MemoryConversationPage
from widget.ui.pages.memory_recall_page import MemoryRecallPage
from widget.ui.pages.operations_page import (
    DataBrowserPage, FunctionalTestsPage, MemoryRecordsPage, ModelGatewayPage,
    WorkerTasksPage,
)
from widget.ui.pages.status_page import StatusPage
from widget.ui.window_activation import show_front


class WorkbenchWindow(QWidget):
    def __init__(self, state, controller, bridge, broadcast_page=None, tasks_page=None,
                 avatar_provider=None, adapter=None, history_tasks_path="", history_source=None,
                  history_channels=None, history_adapter_for=None, memory_bridge_for=None,
                  history_default_channel="",
                  instances_page=None, version_page=None, labels=None,
                  app_icon: QIcon | None = None):
        super().__init__()
        self.setObjectName("Workbench")
        self.setWindowTitle("AI客服")
        if app_icon is not None and not app_icon.isNull():
            self.setWindowIcon(app_icon)
        self.resize(1380, 860)
        self.setMinimumSize(1120, 700)
        root = QHBoxLayout(self); root.setContentsMargins(0, 0, 0, 0); root.setSpacing(0)

        # 页面按固定顺序构建，索引动态分配（避免硬编码索引，加页不易错）。
        self.handoff_page = HandoffPage(controller, avatar_provider=avatar_provider,
                                        on_open_chat=self.show_history, adapter=adapter,
                                        labels=labels)
        # 「会话」= 主页面：真实聊天（读全量历史）+ 人工发消息 + 会话级 AI 托管开关 + 渠道切换。
        # controller 用于人工回复时按渠道路由并释放待人工（缺省则退化为 adapter 直发）；
        # history_channels/adapter_for 给了则渲染渠道下拉，可在个人微信/企微间切换。
        self.history_page = HistoryPage(bridge, adapter=adapter, avatar_provider=avatar_provider,
                                        tasks_path=history_tasks_path, source=history_source,
                                        on_followed=self._on_followed, controller=controller,
                                        channels=history_channels, adapter_for=history_adapter_for,
                                        default_channel=history_default_channel, state=state,
                                        defer_initial_load=True)
        self.status_page = StatusPage(state)
        self.memory_conversation_page = MemoryConversationPage(
            bridge, adapter=adapter, channels=history_channels, adapter_for=history_adapter_for,
            bridge_for=memory_bridge_for, default_channel=history_default_channel, labels=labels,
        )
        self.long_term_memory_page = LongTermMemoryPage(
            bridge, labels=labels, channels=history_channels, bridge_for=memory_bridge_for,
        )
        self.memory_page = MemoryRecallPage(
            bridge, labels=labels, channels=history_channels, bridge_for=memory_bridge_for,
        )
        self.memory_governance_page = MemoryRecordsPage(
            bridge, governance=True, channels=history_channels,
            bridge_for=memory_bridge_for, labels=labels,
        )
        self.memory_benchmark_page = MemoryPage(bridge, labels=labels)
        self.worker_tasks_page = WorkerTasksPage(bridge)
        self.data_browser_page = DataBrowserPage(bridge)
        self.functional_tests_page = FunctionalTestsPage(
            bridge, benchmark_page=self.memory_benchmark_page
        )
        self.model_gateway_page = ModelGatewayPage(bridge)
        self.broadcast_page = broadcast_page
        self.tasks_page = tasks_page
        self.instances_page = instances_page
        self.version_page = version_page

        spec = []                                   # (name, label, page)
        spec.append(("history", "会话中心", self.history_page))
        spec.append(("handoff", "待人工", self.handoff_page))
        spec.append(("memory_conversation", "记忆对话", self.memory_conversation_page))
        spec.append(("long_term_memory", "长期记忆", self.long_term_memory_page))
        spec.append(("memory_recall", "记忆召回", self.memory_page))
        spec.append(("memory_governance", "记忆治理", self.memory_governance_page))
        spec.append(("status", "状态设置", self.status_page))
        if instances_page is not None:
            spec.append(("instances", "实例", instances_page))
        if broadcast_page is not None:
            spec.append(("broadcast", "群发", broadcast_page))
        if tasks_page is not None:
            spec.append(("tasks", "群发任务", tasks_page))
        if version_page is not None:
            spec.append(("version", "版本", version_page))
        spec.append(("worker_tasks", "Worker 任务", self.worker_tasks_page))
        spec.append(("data_browser", "数据浏览", self.data_browser_page))
        spec.append(("functional_tests", "功能测试", self.functional_tests_page))
        spec.append(("model_gateway", "模型网关", self.model_gateway_page))

        side = QWidget(); side.setObjectName("Sidebar"); side.setFixedWidth(224)
        side_lay = QVBoxLayout(side); side_lay.setContentsMargins(14, 18, 14, 16)
        side_lay.setSpacing(4)
        brand = QHBoxLayout()
        mark = QLabel(); mark.setObjectName("BrandMark")
        mark.setFixedSize(38, 38); mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        brand_icon = app_icon if app_icon is not None and not app_icon.isNull() else load_app_icon()
        if not brand_icon.isNull():
            mark.setPixmap(brand_mark_pixmap(brand_icon, 38))
        else:
            mark.setText("AI")
        brand_copy = QVBoxLayout(); brand_copy.setSpacing(0)
        brand_name = QLabel("AI客服"); brand_name.setObjectName("BrandName")
        brand_sub = QLabel("AI SERVICE DESK"); brand_sub.setObjectName("BrandSub")
        brand_copy.addWidget(brand_name); brand_copy.addWidget(brand_sub)
        brand.addWidget(mark); brand.addSpacing(8); brand.addLayout(brand_copy); brand.addStretch(1)
        side_lay.addLayout(brand)
        side_lay.addSpacing(16)
        self._nav = QButtonGroup(self)
        self._stack = QStackedWidget()
        self._idx: dict[str, int] = {}
        self._btn: dict[str, QPushButton] = {}
        self._nav_labels: dict[str, str] = {}
        self._page_labels: dict[str, str] = {}
        group_for = {
            "history": "客户服务", "handoff": "客户服务",
            "memory_conversation": "记忆", "long_term_memory": "记忆",
            "memory_recall": "记忆", "memory_governance": "记忆",
            "broadcast": "营销触达", "tasks": "营销触达",
            "status": "运维", "instances": "运维", "version": "运维",
            "worker_tasks": "运维", "data_browser": "运维", "functional_tests": "运维",
            "model_gateway": "模型网关",
        }
        current_group = None
        for i, (name, label, page) in enumerate(spec):
            group = group_for.get(name, "工作台")
            if group != current_group:
                group_label = QLabel(group.upper()); group_label.setObjectName("NavGroup")
                side_lay.addWidget(group_label)
                current_group = group
            self._idx[name] = i
            nav_label = label
            b = QPushButton(nav_label); b.setObjectName("NavItem"); b.setCheckable(True)
            self._nav.addButton(b, i)
            side_lay.addWidget(b)
            self._btn[name] = b
            self._nav_labels[name] = nav_label
            self._page_labels[name] = label
            self._stack.addWidget(page)
        side_lay.addStretch(1)
        separator = QFrame(); separator.setFrameShape(QFrame.Shape.HLine)
        separator.setStyleSheet("background:#17314C; max-height:1px; border:none;")
        side_lay.addWidget(separator)
        foot = QLabel("本地记忆库 · 模型调用按当前网关"); foot.setObjectName("SidebarFoot")
        foot_strong = QLabel("●  服务防护已启用"); foot_strong.setObjectName("SidebarFootStrong")
        side_lay.addWidget(foot); side_lay.addWidget(foot_strong)
        root.addWidget(side)

        wrap = QWidget(); wrap.setObjectName("ContentShell")
        wrap_lay = QVBoxLayout(wrap); wrap_lay.setContentsMargins(0, 0, 0, 0); wrap_lay.setSpacing(0)
        header = QFrame(); header.setObjectName("TopHeader"); header.setFixedHeight(86)
        header_lay = QHBoxLayout(header); header_lay.setContentsMargins(22, 12, 0, 12)
        header_copy = QVBoxLayout(); header_copy.setSpacing(2)
        self._header_title = QLabel("会话中心"); self._header_title.setObjectName("HeaderTitle")
        self._header_sub = QLabel("实时接待、人工协同与客户上下文"); self._header_sub.setObjectName("HeaderSub")
        header_copy.addStretch(1); header_copy.addWidget(self._header_title)
        header_copy.addWidget(self._header_sub); header_copy.addStretch(1)
        header_lay.addLayout(header_copy, 2)
        self._header_values: dict[str, QLabel] = {}
        for key, label in (("pending", "待人工"), ("channels", "服务渠道"),
                           ("backend", "AI 服务"), ("mode", "回复模式")):
            card = QFrame(); card.setObjectName("HeaderStat"); card.setMinimumWidth(128)
            card_lay = QVBoxLayout(card); card_lay.setContentsMargins(18, 8, 18, 8); card_lay.setSpacing(2)
            caption = QLabel(label); caption.setObjectName("HeaderStatLabel")
            value = QLabel("—"); value.setObjectName("HeaderStatValue")
            card_lay.addWidget(caption); card_lay.addWidget(value)
            header_lay.addWidget(card, 1)
            self._header_values[key] = value
        wrap_lay.addWidget(header)
        page_host = QWidget()
        page_lay = QVBoxLayout(page_host); page_lay.setContentsMargins(18, 18, 18, 16)
        page_lay.addWidget(self._stack)
        wrap_lay.addWidget(page_host, 1)
        root.addWidget(wrap, 1)

        self._nav.idClicked.connect(self._switch)
        first = spec[0][0]                          # 默认显示「会话」页
        self._btn[first].setChecked(True)
        self._stack.setCurrentIndex(0)
        self._channel_count = max(1, len(history_channels or []))
        self._state = state
        self._update_header(first)
        # 人工回复可能发生在“待人工”页或“会话”页。统一刷新整个工作台，
        # 让左侧待人工数字与 controller 中已经清空的队列保持一致。
        if hasattr(controller, "set_on_changed"):
            controller.set_on_changed(self.refresh)

    def _switch(self, idx: int) -> None:
        self._stack.setCurrentIndex(idx)
        name = self.current_page_name()
        self._update_header(name)
        if idx == self._idx.get("history"):
            # 页面对象一直存在，返回时保留已显示的会话和聊天；同步 refresh() 会先清空快照，
            # hook 瞬时读空/超时时就表现为“切回来消息没了”。后台增量刷新只在新数据就绪后替换。
            self.history_page.activate()
        elif idx == self._idx.get("memory_conversation"):
            # 和会话中心相同：返回页面先保留已显示快照，只在后台刷新，避免 hook 瞬时
            # 读空或慢查询把左右两栏清成白屏。
            self.memory_conversation_page.seed_snapshot(self.history_page.memory_snapshot())
            self.memory_conversation_page.activate()
        elif idx == self._idx.get("memory_recall"):
            self.memory_page.refresh()
        elif idx == self._idx.get("long_term_memory"):
            self.long_term_memory_page.refresh()
        elif idx == self._idx.get("memory_governance"):
            self.memory_governance_page.refresh()
        elif idx == self._idx.get("worker_tasks"):
            self.worker_tasks_page.refresh()
        elif idx == self._idx.get("data_browser"):
            self.data_browser_page.refresh()
        elif idx == self._idx.get("functional_tests"):
            pass  # 只在用户点击“运行功能测试”时执行，避免切页产生外部探测
        elif idx == self._idx.get("model_gateway"):
            self.model_gateway_page.refresh()
        elif idx == self._idx.get("broadcast") and self.broadcast_page is not None:
            self.broadcast_page.refresh_friends()
        elif idx == self._idx.get("tasks") and self.tasks_page is not None:
            self.tasks_page.refresh()
        elif idx == self._idx.get("instances") and self.instances_page is not None:
            self.instances_page.refresh()
        elif idx == self._idx.get("version") and self.version_page is not None:
            # 每次切进来重读一次客户端版本：客户刚在别处装/降过版本，页面不该还是旧值。
            self.version_page.refresh()

    def current_page_name(self) -> str:
        idx = self._stack.currentIndex()
        for name, i in self._idx.items():
            if i == idx:
                return name
        return ""

    def _goto(self, name: str) -> None:
        if name not in self._idx:
            return
        self._btn[name].setChecked(True)
        self._stack.setCurrentIndex(self._idx[name])
        self._update_header(name)

    def _update_header(self, name: str) -> None:
        subtitles = {
            "history": "实时接待、人工协同与客户上下文",
            "memory_conversation": "从真实微信好友聊天中提取和更新记忆",
            "long_term_memory": "查看由长期记忆形成的用户画像",
            "memory_recall": "按好友隔离检索、解释分数与召回证据",
            "memory_governance": "审阅、置顶、修订与同步删除记忆",
            "handoff": "集中处理需要人工介入的会话",
            "broadcast": "安全触达客户并控制发送节奏",
            "tasks": "跟踪群发任务、失败项与重试进度",
            "status": "查看连接健康度并调整运行策略",
            "instances": "管理微信与企业微信运行实例",
            "version": "核对客户端版本与兼容状态",
            "worker_tasks": "跟踪后台任务状态、耗时与结果",
            "data_browser": "只读浏览当前租户的白名单数据",
            "functional_tests": "执行数据库、模型与记忆引擎检查",
            "model_gateway": "查看脱敏后的模型路由与运行模式",
        }
        self._header_title.setText(self._page_labels.get(name, "AI客服"))
        self._header_sub.setText(subtitles.get(name, "本地一体化智能客服控制台"))
        self._refresh_header_stats()

    def _refresh_header_stats(self) -> None:
        pending = self.handoff_page.pending_count()
        self._header_values["pending"].setText(str(pending))
        self._header_values["channels"].setText(str(self._channel_count))
        self._header_values["backend"].setText("在线" if getattr(self._state, "backend_ok", False) else "待连接")
        cfg = getattr(self._state, "cfg", None)
        self._header_values["mode"].setText("自动" if getattr(cfg, "auto_send", False) else "审阅")

    def show_handoff(self) -> None:
        self._goto("handoff"); show_front(self)

    def _on_followed(self) -> None:
        # 后台消息/微信活跃会话变化只更新内部视图，不抢占用户正在使用的窗口焦点。
        # 真正需要人工时由通知策略决定是否打开「待人工」页。
        self._goto("history")

    def show_history(self, contact_id: str = "") -> None:
        """切到聊天记录页；给了 contact_id 则打开和 ta 的会话（无会话则仅切页）。"""
        self._goto("history")
        if contact_id:
            self.history_page.select_contact(contact_id)
        else:
            self.history_page.activate()
        show_front(self)

    def show_history_for_channel(self, channel_key: str = "") -> None:
        """切到会话页并切到指定实例的渠道（实例控制台「看会话」用）。"""
        self._goto("history")
        if channel_key:
            self.history_page.switch_to_channel(channel_key)
        self.history_page.activate()
        show_front(self)

    def show_broadcast(self) -> None:
        if self.broadcast_page is None:
            return
        self._goto("broadcast"); show_front(self)

    def refresh(self) -> None:
        n = self.handoff_page.pending_count()
        base = self._nav_labels["handoff"]
        self._btn["handoff"].setText(f"{base} ({n})" if n else base)
        self.handoff_page.refresh()
        self.status_page.refresh()
        self._refresh_header_stats()
