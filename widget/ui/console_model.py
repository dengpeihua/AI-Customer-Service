"""统一会话控制台的视图模型（无 Qt 依赖，纯逻辑，可单测）。

照 WeiClaw 三栏客服台的信息结构，但补上它没有的**渠道维度**：把个人微信 + 企微两条渠道的
会话合并成一个列表（每条带渠道徽章），待人工优先置顶。右详情面板的「AI 托管开关」= 会话级
转人工/恢复 AI（复用后端 F4 的 ai_muted 标签，引擎已认）。

依赖全部注入，便于测试：
  hub        —— ChannelHub（多渠道 adapter/pipeline 路由）
  state      —— RuntimeState（待人工队列 / 计数）
  backend    —— 鸭子类型，需要 get_ai_mute(ch,contact)->bool / set_ai_mute(ch,contact,bool)
                / list_customer_tags(ch,contact)->list[dict]（通常是 widget.bridge.Bridge）
  controller —— HandoffController（人工回复按渠道路由 + 释放 + 移出待办）
"""
from __future__ import annotations


class ConversationConsole:
    # legacy 兜底（单实例/未接注册表时的旧文案）；per-instance 场景由注入的 labels 覆盖。
    CHANNEL_LABELS = {"wechat_personal": "个人微信", "wecom_hook": "企微"}

    def __init__(self, hub, state, backend, controller, history_syncer=None, labels=None):
        self.hub = hub
        self.state = state
        self.backend = backend
        self.controller = controller
        # 企微历史【自动】反哺后台同步器（可选）。有它时 GUI 显示自动同步状态，客户无需点按钮。
        self.history_syncer = history_syncer
        # per-instance 标签注册表：{channel_key: display_name}（app 从 instances 注册表建，注入）。
        # 缺省 {} → 回落到 legacy 两平台常量，再回落 raw key（M0 前/单实例行为不变）。
        self.labels: dict = dict(labels or {})

    def channel_label(self, channel: str) -> str:
        return self.labels.get(channel, self.CHANNEL_LABELS.get(channel, channel))

    def _pending(self) -> list[dict]:
        snapshot = getattr(self.state, "pending_snapshot", None)
        return snapshot() if callable(snapshot) else list(self.state.pending)

    def _pending_keys(self) -> set:
        return {(p.get("channel", ""), p.get("contact")) for p in self._pending()}

    def conversations(self) -> list[dict]:
        """双渠道合并会话列表；待人工优先、其次按最近活动。每条带 channel/channel_label。"""
        pend = self._pending_keys()
        rows: list[dict] = []
        seen: set = set()
        for ch in self.hub.channels():
            adapter = self.hub.adapter(ch)
            try:
                sessions = adapter.list_sessions() if adapter else []
            except Exception:
                sessions = []
            for s in sessions:
                contact = s.get("wxid") or s.get("contact") or ""
                if not contact:
                    continue
                key = (ch, contact)
                seen.add(key)
                rows.append({
                    "channel": ch, "channel_label": self.channel_label(ch),
                    "contact": contact, "name": s.get("name") or contact,
                    "last": s.get("last", ""), "ts": s.get("ts", 0),
                    "pending": key in pend,
                })
        # 待人工里可能有还没进会话列表的联系人（个人微信从 DB 列会话、企微从 feed），补进来置顶
        for p in self._pending():
            key = (p.get("channel", ""), p.get("contact"))
            if key[1] and key not in seen:
                seen.add(key)
                rows.append({
                    "channel": key[0], "channel_label": self.channel_label(key[0]),
                    "contact": key[1], "name": key[1], "last": p.get("text", ""),
                    "ts": 0, "pending": True,
                })
        rows.sort(key=lambda r: (1 if r["pending"] else 0, r["ts"]), reverse=True)
        return rows

    def messages(self, channel: str, contact: str) -> list[dict]:
        adapter = self.hub.adapter(channel)
        try:
            return adapter.read_conversation(contact) if adapter else []
        except Exception:
            return []

    def detail(self, channel: str, contact: str) -> dict:
        """右详情面板数据：AI 托管开关状态 + 客户标签。后端不可达时安全降级。"""
        try:
            ai_muted = bool(self.backend.get_ai_mute(channel, contact))
        except Exception:
            ai_muted = False
        try:
            tags = self.backend.list_customer_tags(channel, contact)
        except Exception:
            tags = []
        return {"ai_enabled": not ai_muted, "tags": tags}

    def set_ai_enabled(self, channel: str, contact: str, enabled: bool) -> bool:
        """会话级 AI 托管开关：enabled=False → 挂 AI静音标签（转人工）；True → 取下（恢复AI）。"""
        self.backend.set_ai_mute(channel, contact, not enabled)
        return enabled

    def reply(self, channel: str, contact: str, text: str) -> bool:
        """人工在控制台回复：按渠道路由到正确的桥（经 HandoffController）。"""
        return self.controller.send(contact, text, channel)

    def summarize(self, channel: str, contact: str) -> dict:
        """把该会话总结成 FAQ 反哺知识库（双渠道通用；企微新对话已在后端落库即可反哺）。
        走后端 /v1/kb/summarize；后端会拒纯寒暄/无实质内容的会话。返回 {title,...} 或抛异常。"""
        return self.backend.summarize_to_kb(channel, contact)

    def history_sync_status(self, channel: str = "wecom_hook") -> dict | None:
        """企微历史【自动】反哺的实时状态，供 GUI 显示（客户无需点击）。
        返回 {'running','synced','batches',...}；没接自动同步器则 None（GUI 隐藏该条）。"""
        if channel != "wecom_hook" or self.history_syncer is None:
            return None
        try:
            return self.history_syncer.status()
        except Exception:
            return None

    def harvest_history(self, channel: str) -> list[str]:
        """拉该渠道本地库(message.db)解密后的历史聊天正文（连接前历史 + 人工手打）。
        渠道适配器无此能力（个人微信 / 未接 col_hook）时安全返回 []，绝不抛。
        注：正常运行走后台自动同步（WeComHistorySyncer），这方法保留供「立即同步」等兜底。"""
        adapter = self.hub.adapter(channel)
        fn = getattr(adapter, "harvest_history_texts", None)
        if not callable(fn):
            return []
        try:
            return fn()
        except Exception:
            return []

    def harvest_history_to_kb(self, channel: str, title: str = "企微本地历史反哺") -> dict:
        """拉本地历史 → 后端 LLM 蒸馏成 FAQ 反哺知识库。
        返回 {'count': n, 'title': ...}；无历史时 count=0、不落库、不打后端。"""
        texts = self.harvest_history(channel)
        if not texts:
            return {"count": 0, "title": ""}
        out = self.backend.ingest_history_texts(texts, title)
        out.setdefault("count", len(texts))
        return out

    def pending_count(self) -> int:
        return len(self._pending())
