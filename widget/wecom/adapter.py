from __future__ import annotations

import json
import threading
import time
from collections import deque
from pathlib import Path
from typing import Callable

from widget.echo_ledger import EchoLedger
from widget.models import InboundMsg, SendResult
from widget.reply_quote import quoted_reply_text
from widget.wecom.bridge_client import BridgeClient, BridgeError


def _to_int(v, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


class WeComHookAdapter:
    """企微渠道适配器，实现 ChannelAdapter 协议（对照 WeChatHookAdapter）。

    收：后台线程按 poll_interval_s 轮询桥 `GET /messages?since=<cursor>`，映射成 InboundMsg
        交 on_message；**自过滤**（sender==self_id 丢）+ **回声抑制**（自己刚发的原样回来即丢），
        与个人微信一脉相承，防自问自答死循环。游标可选持久化，防重启重放/漏消息。
    发：默认 **headless**（`POST /hsend`，不开窗/不聚焦、可多客户并发）——自动回复的生产通路；
        `send_mode="ui"` 则走 `POST /send`（SetText+合成回车，需聚焦，供人工/兜底）。发成功记回声。

    桥未实现对应端点（404）——此时 send 返回**诚实失败**、poll 本轮**无消息**，绝不假装成功。
    """
    channel = "wecom_hook"

    def __init__(self, bridge: BridgeClient, self_id: str = "",
                 poll_interval_s: float = 1.0, state_path: str | Path | None = None,
                 send_mode: str = "headless", msgdb_reader=None,
                 conv_state_path: str | Path | None = None, history_reader=None,
                 self_id_resolver: Callable[[], str] | None = None,
                 channel_key: str = "wecom_hook"):
        self.channel = channel_key
        self._bridge = bridge
        self._self_id = self_id
        # self_id 惰性解析：连接时若拿不到（冷启动库句柄未捕获），后续轮询/读会话时用它重试反推，
        # 拿到即回填自己 + history_reader。让 self_id 全程"系统自动识别"，无需用户填。
        self._self_id_resolver = self_id_resolver
        # 进程内自查读全量历史（路线 B step3；WeComHistoryReader）。有它时 read_conversation/
        # list_sessions 直接读企微本地库全量结构化历史（个人微信 QueryDB 式），不再只靠连接后的实时流。
        self._history_reader = history_reader
        # 可选：本地库(message.db)解密读取器（col_probe 被动收割 → widget.wecom.msgdb.MsgDbReader）。
        # 有它时，read hook 只逆到的「连接后实时流」可升级为反哺**连接前历史 + 人工手打**（详见
        # docs/wecom-local-msgdb-re-plan.md 的 RE 里程碑）。为 None 时行为与之前完全一致（不破坏基线）。
        self._msgdb = msgdb_reader
        self._poll_interval_s = poll_interval_s
        self._state_path = Path(state_path) if state_path else None
        self._send_mode = send_mode if send_mode in ("headless", "ui") else "headless"
        self._on_message: Callable[[InboundMsg], None] | None = None
        self._cursor = 0                              # 已消费的最大 seq（DLL 单调游标）
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # 发件账本（对齐个人微信）：回声抑制 + 溯源打标共用一份真相。
        self._echo = EchoLedger()
        # 会话视图：企微 recv hook 只逆到消息事件（无会话历史库），这里把 recv 流 + 自己发的
        # 按联系人累积成「自连接以来的实时会话」，供工作台聊天记录页展示（企微不再是 GUI 里的盲区）。
        self._history: dict[str, deque] = {}
        self._history_max = 200
        self._hist_lock = threading.Lock()
        self._last_ts: dict[str, int] = {}          # 每联系人最近活动时间，供会话列表排序
        # 会话视图持久化：把实时 recv/send 累积的会话落盘，重启后 GUI 仍见历史记录（不用重头等消息）。
        self._conv_state_path = Path(conv_state_path) if conv_state_path else None
        self._conv_dirty = False
        self._last_conv_save = 0.0

    # ---- ChannelAdapter 协议 ----

    def start(self, on_message: Callable[[InboundMsg], None]) -> None:
        self._on_message = on_message
        if not self._bridge.is_ready():
            raise BridgeError("企微 hook 桥未就绪，无法 start（先跑连接器完成注入）")
        self._load_conversations()                    # 恢复上次落盘的会话视图，GUI 立刻有历史
        self._ensure_self_id()                        # 启动即尝试自动识别 self_id（拿不到则轮询时再试）
        loaded = self._load_state()
        if not loaded:
            # 冷启动基线：把游标设到桥当前 max，只处理"启动后"的新消息，不重放环形缓冲里的历史
            # （对齐个人微信 baseline；否则首次连上会把 recv 环缓冲里已有的历史全当新消息回一遍）。
            self._set_baseline()
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _set_baseline(self) -> None:
        """冷启动基线 = 桥当前游标（recv_hook 的 g_seq）。best-effort，失败保持 0。"""
        try:
            body = self._bridge.fetch_messages(self._cursor)
        except BridgeError:
            return
        srv = body.get("cursor")
        if srv is not None:
            self._cursor = max(self._cursor, _to_int(srv))

    def send_message(self, contact_id: str, text: str, provenance: str = "human") -> SendResult:
        try:
            if self._send_mode == "ui":
                body = self._bridge.send(contact_id, text)
            else:
                body = self._bridge.hsend(contact_id, text, self_id=self._self_id)
        except BridgeError as e:
            # 桥未实现该端点或通信失败 → 诚实失败，绝不假装成功。
            return SendResult(ok=False, error=str(e))
        ok = bool(body.get("ok"))
        if ok:
            self._remember_sent(text, provenance)     # 发成功才记（回声抑制 + 溯源）
            self._record_history(contact_id, text, True, provenance)   # 入会话视图（我方气泡）
        return SendResult(ok=ok, error=str(body.get("error", "") or ""))

    def send_reply(self, contact_id: str, text: str, reply_to: InboundMsg,
                   provenance: str = "human") -> SendResult:
        return self.send_message(
            contact_id, quoted_reply_text(text, reply_to.get("text")), provenance=provenance
        )

    def self_wxid(self) -> str:
        self._ensure_self_id()
        return self._self_id

    def _ensure_self_id(self) -> None:
        """self_id 为空时用解析器反推一次；拿到即回填自己 + history_reader（下次不再解析）。"""
        if self._self_id or self._self_id_resolver is None:
            return
        try:
            sid = self._self_id_resolver()
        except Exception:
            sid = ""
        if sid:
            self._self_id = str(sid)
            if self._history_reader is not None:
                try:
                    self._history_reader.set_self_id(self._self_id)
                except Exception:
                    pass

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        self._save_conversations(force=True)          # 退出前落盘会话视图
        self._on_message = None

    # ---- 回声抑制 + 溯源（共用发件账本）----

    def _remember_sent(self, text: str, provenance: str = "human") -> None:
        self._echo.remember(text, provenance)

    def _is_own_echo(self, text: str) -> bool:
        return self._echo.is_own(text)

    def provenance_for(self, text: str) -> str | None:
        """这条出向文本的来源（ai/human/broadcast），未知返回 None。供聊天页按字段判定来源。"""
        return self._echo.source_of(text)

    # ---- 收：轮询 ----

    def _build_msg(self, raw: dict) -> InboundMsg | None:
        text = str(raw.get("text", "") or "")
        if not text.strip():
            return None                               # 空文本/非文本 → 丢
        sender = str(raw.get("sender_id", "") or "")
        if sender and self._self_id and sender == self._self_id:
            return None                               # 自己发的 → 不接
        if self._is_own_echo(text):                   # 自己刚发的回声 → 丢，防自问自答
            return None
        contact_id = str(raw.get("contact_id", "") or "") or sender
        if not contact_id:
            return None                               # 无回复目标 → 丢
        is_group = bool(raw.get("is_group", False))
        seq = raw.get("seq")
        return InboundMsg(
            channel=self.channel,
            msg_id=str(raw.get("msg_id", "") or (f"seq:{seq}" if seq is not None else "")),
            contact_id=contact_id,
            sender_id=sender or contact_id,
            text=text,
            is_group=is_group,
            at_me=bool(raw.get("at_me", False)),
            timestamp=_to_int(raw.get("timestamp")),
        )

    def poll_once(self) -> list[InboundMsg]:
        """拉一批入站消息并映射/过滤。桥未实现或通信失败 → 返回空（本轮跳过，不抛）。"""
        self._ensure_self_id()                        # 冷启动后自动补上 self_id（自过滤才准）
        try:
            body = self._bridge.fetch_messages(self._cursor)
        except BridgeError:
            return []
        out: list[InboundMsg] = []
        changed = False
        # 自愈：桥游标(g_seq) < 本地游标 → DLL 被重注入过、g_seq 归零。陈旧的高游标会让
        # fetch 永远取不到新消息（静默失聪）。夹回桥当前值，从新基线续（宁可漏注入间隙的极少数，
        # 不要永久失聪；对齐个人微信重注入自愈）。
        srv0 = body.get("cursor")
        if srv0 is not None and _to_int(srv0) < self._cursor:
            self._cursor = _to_int(srv0)
            changed = True
        for raw in (body.get("messages") or []):
            seq = _to_int(raw.get("seq"))
            if seq > self._cursor:
                self._cursor = seq
                changed = True
            msg = self._build_msg(raw)
            if msg is not None:
                out.append(msg)
                self._record_history(msg["contact_id"], msg["text"], False,
                                     "customer", msg.get("timestamp") or 0)   # 入会话视图
        srv_cursor = body.get("cursor")               # 桥直接回传的权威游标（比逐条 seq 更稳）
        if srv_cursor is not None and _to_int(srv_cursor) > self._cursor:
            self._cursor = _to_int(srv_cursor)
            changed = True
        if changed:
            self._save_state()
        return out

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                msgs = self.poll_once()
            except Exception:
                msgs = []                             # 轮询异常 → 本轮跳过，下轮重试
            for msg in msgs:
                if self._on_message:
                    try:
                        self._on_message(msg)
                    except Exception:
                        pass                          # 单条失败不拖累同批其他消息
            self._save_conversations()                # 节流落盘（有变化 + 距上次≥10s 才写）
            self._stop.wait(self._poll_interval_s)

    # ---- 骨架自检直通（供 connector / 冒烟用）----

    def health(self) -> dict:
        return self._bridge.health()

    def is_ready(self) -> bool:
        return self._bridge.is_ready()

    # ---- 会话视图（自连接以来的实时会话，来自 recv 流 + 自己发的）----
    # 注：企微 recv hook 只逆到消息事件，没逆会话历史库/联系人库，所以这里是"连上之后"的实时流，
    # 不是完整历史；文本类，无名字（企微 uid 原样显示）。够工作台看当前会话、回复。

    def _record_history(self, contact: str, text: str, is_self: bool,
                        provenance: str, ts: int = 0) -> None:
        if not contact or not (text or "").strip():
            return
        ts = ts or int(time.time())
        with self._hist_lock:
            dq = self._history.get(contact)
            if dq is None:
                dq = deque(maxlen=self._history_max)
                self._history[contact] = dq
            dq.append({"text": text, "is_self": is_self, "provenance": provenance, "ts": ts})
            self._last_ts[contact] = ts
            self._conv_dirty = True

    def read_conversation(self, contact_id: str, limit: int = 80) -> list[dict]:
        # 优先走进程内自查读全量历史（连接前 + 人工手打全都有）；不可用则回落连接后的实时视图。
        self._ensure_self_id()                        # 历史读取器需 self_id 拼 1:1 会话 id
        if self._history_reader is not None:
            try:
                full = self._history_reader.read_conversation(contact_id, limit=max(limit, 300))
                if full:
                    return full[-limit:] if limit else full
            except Exception:
                pass
        with self._hist_lock:
            dq = self._history.get(contact_id)
            return list(dq)[-limit:] if dq else []

    def harvest_history_texts(self) -> list[str]:
        """从本地库解密读取器拉一批**历史聊天正文**（连接前 + 人工手打），去重返回，供反哺 KB/话术库。

        无 msgdb_reader（未接 col_probe）时返回空——此时仍可用「连接后实时会话」反哺（原有能力不变）。
        只读、纯收割，绝不写企微库。异常自吞（reader 内部已降级），不拖累调用方。
        """
        if self._msgdb is None:
            return []
        try:
            return self._msgdb.harvest_texts()
        except Exception:
            return []

    def list_sessions(self) -> list[dict]:
        self._ensure_self_id()                        # 历史读取器需 self_id 才列得出全量会话
        merged: dict[str, dict] = {}
        # 全量历史里的所有会话（进程内自查）——GUI 一上来就看得到所有客户会话，不用等实时消息。
        if self._history_reader is not None:
            try:
                for s in self._history_reader.list_sessions():
                    merged[s["wxid"]] = dict(s)
            except Exception:
                pass
        # 叠加连接后的实时视图（补 last 文本 / 更新的 ts）。
        from widget.wecom.msgdb import is_app_contact
        with self._hist_lock:
            for c, dq in self._history.items():
                if is_app_contact(c):            # 应用/系统推送(10120…)不进会话列表
                    continue
                row = merged.get(c) or {"wxid": c, "name": c, "last": "", "ts": 0}
                row["last"] = dq[-1]["text"] if dq else row.get("last", "")
                row["ts"] = max(row.get("ts", 0), self._last_ts.get(c, 0))
                merged[c] = row
        rows = list(merged.values())
        rows.sort(key=lambda r: r["ts"], reverse=True)   # 最近活动的会话排前
        return rows

    def display_names(self, ids: list[str]) -> dict[str, str]:
        # 有全量历史读取器时，从企微联系人库(user_table/备注)解析真实名字；否则原样显示 uid。
        if self._history_reader is not None:
            try:
                return self._history_reader.resolve_names(ids)
            except Exception:
                pass
        return {i: i for i in ids}

    def active_session(self) -> str:
        return ""

    # ---- 游标持久化（可选，避免重启后重放/漏消息） ----

    def _load_state(self) -> bool:
        """从持久化载入游标。返回 True=载入成功（挂件重启续传，不做冷启动基线）。"""
        if not self._state_path or not self._state_path.exists():
            return False
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
            self._cursor = _to_int(data.get("cursor"), self._cursor)
            return True
        except Exception:
            return False

    def _save_state(self) -> None:
        if not self._state_path:
            return
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            self._state_path.write_text(json.dumps({"cursor": self._cursor}), encoding="utf-8")
        except Exception:
            pass

    # ---- 会话视图持久化（重启后 GUI 仍见历史记录） ----

    def set_conv_state_path(self, path) -> None:
        """在 start() 前重定位会话视图落盘路径（多实例：每账号一份，避免共用一个文件串账号）。"""
        from pathlib import Path
        self._conv_state_path = Path(path) if path else None

    def conv_state_path(self):
        return self._conv_state_path

    def _load_conversations(self) -> None:
        if not self._conv_state_path or not self._conv_state_path.exists():
            return
        try:
            data = json.loads(self._conv_state_path.read_text(encoding="utf-8"))
        except Exception:
            return
        convs = data.get("conversations") or {}
        last = data.get("last_ts") or {}
        with self._hist_lock:
            for contact, msgs in convs.items():
                if not isinstance(msgs, list):
                    continue
                dq = deque(maxlen=self._history_max)
                for m in msgs[-self._history_max:]:
                    if isinstance(m, dict) and (m.get("text") or "").strip():
                        dq.append({"text": m.get("text", ""), "is_self": bool(m.get("is_self")),
                                   "provenance": m.get("provenance", ""), "ts": _to_int(m.get("ts"))})
                if dq:
                    self._history[contact] = dq
                    self._last_ts[contact] = _to_int(last.get(contact))

    def _save_conversations(self, force: bool = False) -> None:
        """节流落盘：有变化且距上次≥10s（或 force）才写；原子替换避免半截文件。"""
        if not self._conv_state_path:
            return
        now = time.time()
        if not force and (not self._conv_dirty or now - self._last_conv_save < 10.0):
            return
        with self._hist_lock:
            if not self._conv_dirty and not force:
                return
            snapshot = {"conversations": {c: list(dq) for c, dq in self._history.items()},
                        "last_ts": dict(self._last_ts)}
            self._conv_dirty = False
        try:
            self._conv_state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._conv_state_path.with_suffix(self._conv_state_path.suffix + ".tmp")
            tmp.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self._conv_state_path)
            self._last_conv_save = now
        except Exception:
            self._conv_dirty = True                   # 写失败下轮重试
