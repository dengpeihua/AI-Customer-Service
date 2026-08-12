"""个人微信渠道适配器 —— WeChat-Hook(4.1.10.27) 版。

收：轮询 `POST /QueryDB/execute` 自动识别当前打开的 `message_N.db`。消息按会话分表 `Msg_[md5(会话user_name)]`，
    发言人 `real_sender_id` 是 `Name2Id` 表的 rowid（→ wxid）。文本消息 local_type=1，
    message_content 在 WCDB_CT_message_content=0 时是明文 UTF-8、非 0 时是 zstd（见 decode_content）。
发：`POST /SendTextMsg`（body 须 UTF-8）。
登录：hook 的 g_IsLogin 恒 0 会挡死 QueryDB，start() 调 ensure_login_patched() 运行时 patch（见 widget.hook_patch）。

真实库结构由 4.1.10.27 实测坐实；hook HTTP 契约见 WeChat-Hook README/postman。
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from collections import deque
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlsplit

import httpx

from widget.config import WidgetConfig
from widget.echo_ledger import EchoLedger
from widget.hook_io import lock_for_port
from widget.models import InboundMsg, SendResult
from widget.reply_quote import quoted_reply_text
from widget.wechat_media import WechatMediaResolver, parse_wechat_message
from widget.wechat_accounts import (
    is_group_account,
    is_official_or_system_account,
    is_private_conversation,
    is_supported_conversation,
    same_account,
)

MSG_DB = "message_0.db"  # 仅作兼容回退；4.x 会按账号当前打开文件自动识别 message_N.db
FTS_DB = "message_fts.db"
_LIST_TABLES_SQL = "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg%'"
_LEGACY_SUFFIX_RE = re.compile(r"^[0-9a-fA-F]{32}$")
_NAME2ID_SQL = "SELECT rowid AS rid, user_name FROM Name2Id"
_FTS_NAME2ID_SQL = "SELECT rowid AS rid, username FROM name2id"
_FTS_TABLES_SQL = "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'message_fts_v4_%'"
_FTS_TABLE_RE = re.compile(r"^message_fts_v4_(\d+)$")
_POLL_SQL = (
    "SELECT local_id, local_type, real_sender_id, create_time, "
    "message_content AS mc, WCDB_CT_message_content AS ct, HEX(message_content) AS mc_hex, "
    "source AS src, WCDB_CT_source AS src_ct, HEX(source) AS src_hex "
    "FROM Msg_{suffix} WHERE local_id > {cursor} ORDER BY local_id ASC LIMIT 200"
)
_TEXT_TYPE = 1
_UNDECODABLE = "[未能识别的消息，请在微信中查看]"   # 压缩且解不出的文本消息占位（不静默丢弃）
_HISTORY_SQL = (
    "SELECT local_id, local_type, real_sender_id, create_time, "
    "message_content AS mc, WCDB_CT_message_content AS ct, HEX(message_content) AS mc_hex "
    "FROM (SELECT * FROM Msg_{suffix} ORDER BY local_id DESC LIMIT {limit}) ORDER BY local_id ASC"
)
_POLL_BATCH_LIMIT = 1000
_ATLIST_RE = re.compile(r"<atuserlist>(.*?)</atuserlist>", re.S)


def parse_at_me(source_xml: str, self_wxid: str) -> bool:
    """从群消息 source(msgsource XML) 判断是否 @自己：atuserlist 里逗号分隔的 wxid 含 self 即 True。"""
    if not source_xml or not self_wxid:
        return False
    m = _ATLIST_RE.search(source_xml)
    if not m:
        return False
    inner = m.group(1).replace("<![CDATA[", "").replace("]]>", "")
    return self_wxid in {x.strip() for x in inner.split(",") if x.strip()}


def _port_from_base_url(base_url: str) -> int:
    """从 hook 地址解析端口（M4：每个微信实例一个端口）。解析不出返回 0 → 调用方退回缺省。"""
    try:
        return int(urlsplit(base_url).port or 0)
    except (ValueError, TypeError):
        return 0


class PortOwnershipLost(RuntimeError):
    """本实例的 hook 端口已经不归认领时那个微信进程所有 —— 任何收发都必须立刻停手。"""


class IdentityDrift(PortOwnershipLost):
    """端口还在、进程还在，但**这台微信登的已经不是认领时那个号** —— 同样必须立刻停手。

    继承 `PortOwnershipLost`：所有既有的 `except PortOwnershipLost`（`widget/guarded_read.py`
    的副表盘拒绝语义）不改一行就同样兜住身份漂移。
    """


def _default_owns_port(pid: int, port: int) -> bool:
    """惰性导入 + 模块级查找：测试替换 `wechat.ports.listening_port_for_pid` 一样生效。"""
    from widget.wechat.ports import pid_still_owns_port
    return pid_still_owns_port(pid, port)


def _default_owns_account(pid: int, wxid: str) -> bool:
    """惰性导入 + 模块级查找：与认领链的 OS 佐证同一份实现（`manager.process_owns_account`）。"""
    from widget.wechat.manager import process_owns_account
    return process_owns_account(pid, wxid)


def _md5(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest()


def _to_int(v, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def strip_group_sender_prefix(text: str, sender_wxid: str) -> str:
    """移除微信群消息正文里重复的 ``真实sender_id:\n`` 前缀。

    只接受与数据库 `real_sender_id` 精确相同的前缀；私聊或普通的“某人: 内容”不会被误删。
    """
    value = str(text or "")
    sender = str(sender_wxid or "")
    if not sender:
        return value
    for separator in (":\r\n", ":\n"):
        prefix = sender + separator
        if value.startswith(prefix):
            return value[len(prefix):]
    return value


def decode_content(text, ct, hex_val) -> str:
    """还原 message_content 文本。WCDB_CT_message_content=0/NULL 为明文；非 0 为 zstd 压缩。
    无法解压（缺 zstandard / 字典压缩 / 坏帧）时返回空串，交由 inbound 过滤丢弃，绝不返回乱码。"""
    if _to_int(ct, 0) == 0:
        return text or ""
    if not hex_val:
        return ""
    try:
        import zstandard  # 惰性导入：仅压缩消息才需要
        raw = bytes.fromhex(hex_val)
        return zstandard.ZstdDecompressor().decompress(raw).decode("utf-8", "replace")
    except Exception:
        return ""


class WeChatHookAdapter:
    channel = "wechat_personal"

    def __init__(self, cfg: WidgetConfig, client: httpx.Client | None = None,
                 self_wxid_override: str = "", patch_login: bool = True,
                 state_path: str | Path | None = None,
                 channel_key: str = "wechat_personal",
                 base_url: str = "",
                 verified_pid: int = 0, legacy_patch: bool | None = None,
                 owns_port_fn: Optional[Callable[[int, int], bool]] = None,
                 clock: Optional[Callable[[], float]] = None,
                 ownership_ttl: float = 0.25,
                 claimed_wxid: str = "",
                 owns_account_fn: Optional[Callable[[int, str], bool]] = None,
                 identity_ttl: float = 5.0):
        self.channel = channel_key
        self.cfg = cfg
        # M4：base_url 非空 = 该实例自己的 hook 端口（Weixin.exe StartPort=<port>）；
        # 留空 = 沿用全局配置（legacy 单实例，逐字节不变）。
        self.hook_base_url = base_url or cfg.hook_base_url
        # 超时收紧到 4s：hook 偶发慢响应时，用户点开会话/跟随这类 GUI 线程上的同步读最多卡 4s，
        # 不再是 10s（周期性 live_tick 已移到后台线程，见 history_page）。
        self._client = client or httpx.Client(
            base_url=self.hook_base_url, timeout=4.0, trust_env=False
        )
        # The native hook accepts parallel HTTP connections but its in-process
        # WCDB/SQLite bridge is not re-entrant. Polling, history refreshes and
        # readiness probes therefore share one lock per hook port.
        self._io_lock = lock_for_port(self.hook_port())
        self._self_wxid = self_wxid_override
        self._patch_login = patch_login
        # M4 红线：g_IsLogin 是**跨进程写内存**，写错进程 = 写崩客户的微信。只有 WeChatManager
        # 认领链（枚举进程→问端口→问身份→OS 佐证）认出来的 pid 才配被写。
        self._verified_pid = int(verified_pid or 0)
        # legacy（无 instances.yaml 的现网单实例）：那台微信就是全局 hook_base_url 上的那台，
        # 沿用旧的「按端口打」行为，功能不能被掐掉（规则 4）。
        # 缺省判据 = 「没给 base_url」：给了 per-instance 端口就说明这是多实例路径，端口来自
        # 配置（未经验证），除非同时给了已验证 pid，否则一个字节都不写。
        self._legacy_patch = (not base_url) if legacy_patch is None else bool(legacy_patch)
        # 端口归属闸（M4 H2）：认领链验的是 **pid**，可 adapter 手里只有**端口**。
        # 探针可注入（测试）、答案带 TTL 缓存（poll 循环每秒都在跑，别把 psutil 打爆）。
        self._owns_port_fn = owns_port_fn or _default_owns_port
        self._clock = clock or time.monotonic
        self._ownership_ttl = float(ownership_ttl)
        self._ownership_ok = False
        self._ownership_until: float | None = None
        # 身份闸（M4 H5）：端口闸证明的是 pid↔端口，**从来没证明过「登的还是认领时那个号」**。
        # `claimed_wxid` = 认领这条渠道时匹配上的那个账号（instances.yaml 的 self_wxid）。
        self._claimed_wxid = str(claimed_wxid or self_wxid_override or "")
        self._owns_account_fn = owns_account_fn or _default_owns_account
        # `identity_ttl` 现在只有**一个**用途：supervisor 后台线程周期确认的最小间隔
        # （见 refresh_identity）。读路径一律不再按 TTL 现探（R2：那是 130~280ms 持 GIL 的活）。
        self._identity_ttl = float(identity_ttl)
        # 身份结论：**建这个 adapter 的前一刻**，认领链刚用同一条 OS 佐证确认过
        # （`WeChatManager._identity_reject` → `owns_data_dir`），所以带 verified_pid 出生即「已确认」；
        # 没有 claimed_wxid 就无从核对 → fail closed。此后只由三处推进（见 _probe_identity）。
        self._identity_ok = bool(self._claimed_wxid)
        self._identity_at: float | None = None      # 上次**昂贵**确认的时刻
        self._identity_probe_lock = threading.Lock()
        self._identity_refresh_guard = threading.Lock()
        self._identity_refresh_thread: threading.Thread | None = None
        # 出生那一刻 = 认领链刚用同一条 OS 佐证核对完身份的那一刻，算作一次货真价实的权威确认
        # （只用于「结论还新不新鲜」这个纯内存判断，不影响 refresh_identity 的探测时间表）。
        self._identity_born_at = self._clock()
        self._identity_suspect = False              # 便宜触发器亮了、还没被昂贵确认裁决
        self._profile_seen: str | None = None       # 便宜触发器：hook 说此刻登的是谁
        self._profile_at: float | None = None
        # GetSelfProfile 在部分微信版本里会稳定返回另一个、已经过期的 wxid。只有当
        # OS 打开文件证据刚刚证明当前 pid 仍属于 claimed_wxid 时，才把这个具体值
        # 记为“已证伪”；其他新的不同值依然立即触发 fail-closed。
        self._disproved_profile_wxid: str | None = None
        self._state_path = Path(state_path) if state_path else None
        self._cursors: dict[str, int] = {}        # 会话表 md5 后缀 -> 已消费的 max(local_id)
        # 每次 start 都重新划定“本轮只处理此刻之后的新消息”。持久化游标只作诊断/运行中续写，
        # 绝不能让软件关闭期间积累、且客服可能已经手工回复过的消息在重启后重新进 AI。
        self._startup_cutoff = 0
        self._baseline_ready = False
        self._id2name: dict[int, str] = {}        # Name2Id.rowid -> user_name(wxid/群id)
        self._md52name: dict[str, str] = {}       # md5(user_name) -> user_name（反查表名对应会话）
        # None=尚未探测；set=上次真实 sqlite_master 查询得到的旧式 Msg_<md5> 表。
        # 不能用 _md52name 代替：Name2Id 会包含仅存于新版 FTS 的联系人。
        self._legacy_suffixes: set[str] | None = None
        self._fts_id2name: dict[int, str] = {}
        self._fts_name2id: dict[str, int] = {}
        self._fts_tables: list[str] | None = None
        # 微信会滚动创建 message_N.db，不能把 message_0.db 写死。首次读取时从已认领进程
        # 的打开文件中识别；没有 pid 的 legacy 路径则只读探测有限候选。附件根目录也由同一
        # 打开文件推导，避免跨账号读取另一个微信实例的文件。
        self._msg_db_name: str = ""
        self._account_root: Path | None = None
        self._media_pid: int = 0
        self._media = WechatMediaResolver(self._query_db)
        # /GetSelfProfile 在部分 4.1.10.27 真机上返回的 ID 与消息库 sender_id 对应的
        # username 不同。私聊中 session=对方，sender!=session 的一侧必然是本人；据此学习
        # 本地消息库里的本人别名，供私聊方向、群聊本人消息过滤和 @我 判断共同使用。
        self._self_wxid_aliases: set[str] = set()
        if self._self_wxid:
            self._self_wxid_aliases.add(self._self_wxid)
        self._on_message: Callable[[InboundMsg], None] | None = None
        self._last_patch = ""
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_poll_error = ""
        # 新版微信可能把同一条消息同时写入 Msg_* 与 FTS。两套表的 msg_id 不同，
        # 普通 msg_id 去重识别不了；这里用内容+时间只消除“跨存储副本”。
        self._cross_store_seen: dict[tuple, str] = {}
        self._cross_store_order: deque[tuple] = deque()
        # 发件账本：回声抑制（收到自己刚发的原样消息即丢，防自问自答；即使发信人 wxid 解析出错、
        # "过滤自己发的"漏判，也能靠内容挡住）+ 溯源打标（provenance_for）共用一份真相。有界+TTL。
        self._echo = EchoLedger()

    # ---- 端口归属闸（每次收发前）----

    def hook_port(self) -> int:
        """本实例说话的那个端口。解析不出返回 0 —— 归属闸据此 fail closed。"""
        return _port_from_base_url(self.hook_base_url)

    def owns_its_port(self, fresh: bool = False) -> bool:
        """这个端口此刻**还归认领时那个 pid** 所有吗？（答案缓存 `ownership_ttl` 秒）

        ★为什么每次操作都要问（本轮 Critical，H2）★：认领链验的是 pid，adapter 手里却只有
        端口，端口易主它一无所知。客户关掉甲的微信、按文档 `pythonw start_wechat.py`
        （不带 --port → hook 绑缺省 30001）重开并扫成乙，pid 200 立刻占住 30001：
        · 出站：人工客服在甲的待人工页点「回复」，这句话从**乙的微信**发给了乙的联系人；
        · 入站：甲那条还在跑的轮询线程读 30001，会话表 md5 全不认识 → cursor=0 →
          乙的**全部聊天历史**当新消息灌进甲的租户。
        supervisor 的 park 只在 tick 粒度（≤5s）上兜底，而且完全管不到发送路径。

        `verified_pid` 为 0 = legacy 单实例（无 instances.yaml 的合成行）：它本来就没有
        经过验证的 pid，那台微信就是全局 hook_base_url 上的那台 —— 整条闸跳过、探针一次都不调，
        行为与 5cea8aa 逐字节相同（规则 4）。

        `fresh=True` 跳过缓存现探一次：**出站**走这条（发消息是人点出来的/群发有节流，一次
        psutil 查询完全付得起），免得缓存里那条旧答案把一句话发进别人的微信。

        ★H4★ 缓存窗口原来是 1.0s —— 用户在 GUI 上连点（点会话→列历史→取名字→取头像）
        这一串同步读全落在同一个窗口里，端口在中间易主也照旧读出陌生人的会话。收紧到
        0.25s：一轮 `poll_once`（十几条 SQL，本机毫秒级）仍然只问一次 psutil
        （由 `test_one_poll_cycle_costs_exactly_one_ownership_probe` 量住），
        而人手的连点跨不过去。
        """
        if not self._verified_pid:
            return True
        now = self._clock()
        if not fresh and self._ownership_until is not None and now < self._ownership_until:
            return self._ownership_ok
        try:
            ok = bool(self._owns_port_fn(self._verified_pid, self.hook_port()))
        except Exception:                    # noqa: BLE001 —— 探不出来 = 无法自证 = 视为易主
            ok = False
        self._ownership_ok = ok
        self._ownership_until = now + self._ownership_ttl
        return ok

    def _probe_identity(self) -> bool:
        """★昂贵★ 真去问一次 OS：这个 pid 此刻还开着认领那个号的数据目录吗。

        `psutil.Process(pid).open_files()` **持 GIL**，本机实测微信量级的进程 130~280ms ——
        一次调用就是一次**全进程冻结**（Qt GUI 线程一起冻）。所以只允许三个地方调它：
          · `refresh_identity()`：InstanceSupervisor 自己那条 5s 的后台线程（规则 3）；
          · 出站前的 `fresh=True`（人点出来的动作，正确性压倒 200ms，规则 4）；
          · 轮询遇到**陌生会话表**时的那一次确认（规则 1 的「昂贵确认」）。
        任何 GUI 线程上的读都**不许**走到这里。

        结论是权威的：跑完就把「存疑」清掉 —— 便宜触发器只负责举手，裁决权在这里。
        """
        with self._identity_probe_lock:
            ok = False
            if self._claimed_wxid:      # 没有「认领的是哪个号」就无从核对 → fail closed
                try:
                    ok = bool(self._owns_account_fn(
                        self._verified_pid, self._claimed_wxid,
                    ))
                except Exception:       # noqa: BLE001 —— 探不出来 = 无法自证 = 视为已换号
                    ok = False
            self._identity_ok = ok
            self._identity_at = self._clock()
            self._identity_suspect = False
            if ok and self._profile_seen and self._profile_seen != self._claimed_wxid:
                self._disproved_profile_wxid = self._profile_seen
            return ok

    def _refresh_identity_async(self) -> None:
        """Resolve an unavailable cheap profile probe without waiting for the 5s supervisor tick."""
        if not self._verified_pid or not self._claimed_wxid:
            return
        with self._identity_refresh_guard:
            if self._identity_refresh_thread is not None \
                    and self._identity_refresh_thread.is_alive():
                return

            def worker() -> None:
                self.refresh_identity(force=True)

            self._identity_refresh_thread = threading.Thread(
                target=worker,
                daemon=True,
                name=f"wechat-identity-{self.channel}",
            )
            self._identity_refresh_thread.start()

    def refresh_identity(self, force: bool = False) -> bool:
        """周期性重新确认身份 —— **只有 InstanceSupervisor 的后台线程该调它**（规则 3）。

        读路径（轮询稳态 / 会话页 / 待人工页 / 名单 / 头像）一律只看结论、绝不自己探；
        「过了多久该再确认一次」这件事集中到这里，由那条本来就每 5s 醒一次的线程付钱。
        `identity_ttl` 内重复调用直接给缓存结论：supervisor 一个 tick 一次，绝不因为多问
        一遍健康就把 130~280ms 的开销翻倍。
        """
        if not self._verified_pid:
            return True                 # legacy：整条闸跳过，探针一次都不调（规则 4）
        if force or self._identity_at is None or \
                (self._clock() - self._identity_at) >= self._identity_ttl:
            return self._probe_identity()
        return self._identity_ok

    def _identity_conclusion_is_fresh(self) -> bool:
        """权威结论还在自己的有效期（`identity_ttl`）内吗。**纯内存，零 syscall、零 HTTP**。

        起点 = 上一次昂贵确认；从没确认过就用**出生那一刻** —— 认领链在建这个 adapter 的
        前一刻刚跑完 `owns_data_dir`，那就是一次权威确认（没有它，冷启动的 `baseline()` 会在
        便宜探针坏掉时被拒 → 游标留空 → 之后反而把自己的整份历史当新消息重放）。

        只有「便宜探针问不出答案」这一个分支用它：结论新鲜时它自己站得住、不需要便宜信号背书；
        过期了就必须有人背书，没人背书就停手。
        """
        at = self._identity_at if self._identity_at is not None else self._identity_born_at
        return (self._clock() - at) < self._identity_ttl

    def identity_suspect(self) -> bool:
        """便宜信号已经举了手、还没被昂贵确认裁决。**纯读，不探 OS**。

        supervisor 据此**插队**做一次确认：存疑期间收发全拒（fail closed），所以它必须尽快被
        裁决 —— 要么恢复（hook 乱报一次），要么定性为漂移（真换号）。
        """
        return bool(self._verified_pid) and self._identity_suspect

    def note_identity_suspect(self) -> None:
        """便宜信号说「这台微信好像换号了」：立刻停手（读写全拒），等一次昂贵确认来裁决。

        只制造怀疑、不下结论 —— 便宜信号（hook 自报的 wxid）本项目已知会乱报，凭它把一条
        活渠道判死会让客户静默失去客服；凭它放行则等于没有闸。所以：怀疑期 fail closed，
        由 supervisor 的周期确认（≤5s）或轮询遇陌生表时的确认给出权威结论。
        """
        self._identity_suspect = True

    def _account_changed_cheaply(self) -> bool:
        """★便宜触发器★ 问 hook「你此刻登的是谁」——本机 HTTP，亚毫秒、不持 GIL。

        与认领时那个号对不上就立刻停手并标记存疑。它**只被允许制造怀疑**：
        答案对得上不代表放行（乱报/被换号后仍报旧号都可能），放行与否仍由缓存下来的权威结论决定。
        答案缓存 `ownership_ttl`（0.25s）：一串 GUI 连点只问一次。

        ★问不出来 = 身份未确认 = 拒绝，不是放行★（本轮修复）
        上一版把「问不出来」当成「没信号」直接 `return False` —— 也就是**放行**。方向是反的：
        这个接口本项目实测**会坏、会乱报**（`widget/config.py:35`；真机运维笔记「GetSelfProfile坏」），
        而它坏掉的那一刻恰恰是攻击者最想要的 —— 只要让它回 HTTP 500，再原地换号，两次昂贵确认
        之间（六开轮转 ≈30s）就再没有东西拦得住：游标为空的渠道连 `_strangers_confirmed` 那道
        「陌生会话表先验身份」都免检（`not self._cursors` → 直接放行），陌生账号的整份历史就
        直通 Pipeline → Bridge → 本店后端 / acs.db，传上去的撤不回来。

        但拒绝必须**可恢复**，否则这个已知会坏的接口一抖就等于永久掐掉客户的客服：
        权威结论还新鲜（`_identity_conclusion_is_fresh`）时它自己站得住 → 放行；过期了才停手
        并**举手**（`note_identity_suspect`），由 supervisor 插队做一次昂贵确认来裁决
        （`refresh_identity` / `app._wechat_healthy`）。代价上限 = 一个 tick 的静默轮询。
        注意两者的判据是同一条时间线：只有在 `refresh_identity` 确实会去探的时候才举手，
        所以举起来的手一定有人放得下（不会卡在「存疑但确认被 TTL 挡回」的死角）。
        """
        if not self._verified_pid or not self._claimed_wxid:
            return False                # legacy / 无从核对：一个包都不多发（规则 4）
        now = self._clock()
        if self._profile_at is None or (now - self._profile_at) >= self._ownership_ttl:
            seen: str | None = None
            try:
                with self._io_lock:
                    # 这是每轮读路径的便宜触发器，不是业务请求。hook 异常时绝不能吃掉默认
                    # 4 秒 HTTP 超时；超时后由后台 OS 身份确认裁决。
                    r = self._client.post("/GetSelfProfile", json={}, timeout=0.25)
                    r.raise_for_status()
                    seen = str(r.json().get("wxid", "") or "") or None
            except Exception:           # noqa: BLE001 —— 问不出来 = 身份未确认（见上）
                seen = None
            self._profile_seen, self._profile_at = seen, now
        if self._profile_seen is None:
            if self._identity_conclusion_is_fresh():
                return False           # 权威结论还没过期：它自己站得住，不需要便宜信号背书
            self.note_identity_suspect()
            # /GetSelfProfile 在部分真机上长期无响应。仍先 fail closed，但立即在后台做与
            # supervisor 相同的 OS 权威确认，不让收信固定等待到下一个 5 秒 tick。
            self._refresh_identity_async()
            return True                # 结论过期 + 没人背书 → 停手，等 supervisor 裁决
        if self._profile_seen != self._claimed_wxid:
            if self._profile_seen == self._disproved_profile_wxid:
                # 这个特定自报值刚被 OS 权威证据否定，不能让它在每次 GUI/轮询读取时
                # 重新制造“存疑 -> supervisor 清除 -> 再存疑”的永久空会话循环。
                # 真正换号仍由 supervisor 的周期 OS 核验裁决；出站则始终 fresh 核验。
                if self._identity_conclusion_is_fresh():
                    return False
                self.note_identity_suspect()
                self._refresh_identity_async()
                return True
            self.note_identity_suspect()
            return True
        return False

    def owns_its_account(self, fresh: bool = False) -> bool:
        """这台微信**此刻登的还是认领时那个号**吗？（`fresh=False` = **纯读结论，绝不探 OS**）

        ★为什么端口闸不够（本轮 Critical，H5）★：`owns_its_port` 证明的是 pid↔端口，它对
        「同一个进程换了个号」完全免疫 —— 客户在**同一个** Weixin.exe 里点「退出登录」、扫另一个
        号的码：进程不重启，pid 不变、StartPort 不变、注入的 hook 继续在同一个端口上服务，
        `pid_still_owns_port` 恒为真，每一道既有闸全部放行。于是甲号渠道会把**乙的整份历史**
        当新消息灌进甲的租户（游标 key 全不认识 → cursor=0）、把乙的私聊端上甲的会话页、
        把甲的人工回复和群发**从乙的微信**发出去。而且这是**永久**状态：进程活着，supervisor
        看这行还 online，认领链再也不会重跑。Windows pid 复用是同一个洞的无人为错误版本。

        判据复用认领链自己在用的那条 OS 佐证：`manager.owns_data_dir` —— 进程打开的
        `\\xwechat_files\\<wxid>_` 目录段。它**跟着登录的账号变**，正是我们要的信号。
        **fail closed**：拿不到正面信号（权限抖动 / 进程刚起来还没开库 / 探针抛异常）= 拒绝。

        ★成本（本轮 Critical，R2）★：`psutil.Process(pid).open_files()` **持 GIL**，微信量级的
        进程本机实测 130~280ms。上一版让**每条读路径**在 TTL 到期时就地现探，实测后果：
        一条后台轮询线程就能把主循环 5s 内的 tick 从 ~3280 打到 81；而且 history_page.refresh /
        点开会话 / handoff_page.refresh / 群发名单这**四个入口跑在 Qt GUI 线程上**。
        所以读路径**不再自己探**：
          · `fresh=False` = 纯读结论（`_identity_ok`）+ 一个「存疑」标志，零 syscall；
          · 结论由后台推进：supervisor 的 `refresh_identity()`（规则 3）、轮询遇陌生会话表时的
            确认（规则 1）、出站的现探；
          · 便宜触发器（`_account_changed_cheaply`，问 hook 此刻登的是谁，亚毫秒）负责把
            「可能换号了」在 0.25s 内变成拒绝，不必等那 5s 的周期确认。
        出站（send_message / 群发）一律 `fresh=True` 现探：人点出来的动作、群发条间还有
        3~5s 间隔，一次几十~几百毫秒完全付得起，绝不拿缓存赌一条发到陌生人手里的消息。

        `verified_pid` 为 0 = legacy 单实例：整条闸跳过，探针一次都不调（规则 4）。
        """
        if not self._verified_pid:
            return True
        if fresh:
            return self._probe_identity()
        return self._identity_ok and not self._identity_suspect

    def identity_drifted(self) -> bool:
        """当前结论是不是「已经不是认领时那个号」。**纯读缓存，不探 OS**。

        给 supervisor/自愈路径用（`app.park_offline_channel` / health 检查）：漂移必须让这行
        落回可重认领的状态，否则进程一直活着 = 一直 online = 认领链永远不再跑。
        「存疑」（便宜信号举了手、还没被昂贵确认裁决）不算漂移：判死一条渠道要有权威结论，
        而收发在存疑期间本来就已经被 `owns_its_account` 拦住了。
        """
        return bool(self._verified_pid) and not self._identity_ok

    def guard_ok(self, fresh: bool = False) -> bool:
        """收发/读之前的完整闸：端口还归自己 **且** 账号还是认领那个。

        三层，按代价从低到高：端口归属（psutil net_connections，0.19ms）→ 便宜触发器
        （问 hook 此刻登的是谁，亚毫秒）→ 已确认的身份结论（纯内存）。
        `fresh=True`（出站）时跳过便宜触发器，直接付昂贵确认那一份钱。
        """
        if not self.owns_its_port(fresh=fresh):
            return False
        if not fresh and self._account_changed_cheaply():
            return False
        return self.owns_its_account(fresh=fresh)

    def _require_port_ownership(self, fresh: bool = False) -> None:
        if not self.owns_its_port(fresh=fresh):
            raise PortOwnershipLost(
                f"端口 {self.hook_port()} 已不属于认领时的微信进程（pid={self._verified_pid}）："
                f"拒绝本次收发 —— 绝不从别人的微信读消息/发消息")
        if not fresh and self._account_changed_cheaply():
            if self._profile_seen is None:
                raise IdentityDrift(
                    f"端口 {self.hook_port()} 上的微信问不出此刻登的是谁"
                    f"（/GetSelfProfile 无应答/乱报），而上次权威确认已过期："
                    f"身份未确认 → 拒绝本次收发，等后台昂贵确认裁决")
            raise IdentityDrift(
                f"端口 {self.hook_port()} 上的微信自报登的是 {self._profile_seen}，"
                f"不是认领时那个号（{self._claimed_wxid}）：拒绝本次收发，等后台确认")
        if not self.owns_its_account(fresh=fresh):
            raise IdentityDrift(
                f"pid={self._verified_pid} 上的微信已经不是认领时那个号（{self._claimed_wxid}）："
                f"拒绝本次收发 —— 绝不从别人的账号读消息/发消息")

    # ---- hook HTTP ----

    def self_wxid(self) -> str:
        if self._self_wxid:
            return self._self_wxid
        self._require_port_ownership()   # 别把新主人的账号当成自己的身份记下来
        with self._io_lock:
            r = self._client.post("/GetSelfProfile", json={})
            r.raise_for_status()
            self._self_wxid = str(r.json().get("wxid", ""))
        if self._self_wxid:
            self._self_wxid_aliases.add(self._self_wxid)
        return self._self_wxid

    def send_message(self, contact_id: str, text: str, provenance: str = "human") -> SendResult:
        try:
            # 端口易主 = 这条会从别人的微信发出去：出站一律现探，不吃缓存。
            self._require_port_ownership(fresh=True)
        except PortOwnershipLost as e:
            return SendResult(ok=False, error=str(e))
        try:
            with self._io_lock:
                r = self._client.post("/SendTextMsg", json={"wxidorgid": contact_id, "msg": text})
                r.raise_for_status()
                body = r.json()
        except (httpx.HTTPError, ValueError) as e:
            return SendResult(ok=False, error=str(e))
        ok = body.get("ret") == 0
        if ok:
            self._remember_sent(text, provenance)   # 只登记已被 hook 确认接受的出站消息
        return SendResult(ok=ok, error=body.get("retmsg", ""))

    def send_reply(self, contact_id: str, text: str, reply_to: InboundMsg,
                   provenance: str = "human") -> SendResult:
        # 当前 hook 只暴露 SendTextMsg，没有原生引用发送端点；用微信内可见的引用块保留一一对应关系。
        return self.send_message(
            contact_id, quoted_reply_text(text, reply_to.get("text")), provenance=provenance
        )

    def _remember_sent(self, text: str, provenance: str = "human") -> None:
        self._echo.remember(text, provenance)

    def _is_own_echo(self, text: str) -> bool:
        return self._echo.is_own(text)

    def provenance_for(self, text: str) -> str | None:
        """这条出向文本的来源（ai/human/broadcast），未知返回 None。供聊天页按字段判定来源。"""
        return self._echo.source_of(text)

    def query_db(self, db_name: str, sql: str) -> list[dict]:
        """对外的只读查询出口（群发名单 / 头像这些副表盘用）—— 与收发共用同一道归属闸。

        ★H3★ 副表盘绝不许自己开一个钉死端口的 HTTP 客户端：端口易主后那就是在读陌生人的
        微信库。它们统一从这里进来；闸不放行时抛 `PortOwnershipLost`，调用方一律失败关闭
        （空名单 / 占位头像），绝不退回「读到什么算什么」。
        """
        return self._query_db(db_name, sql)

    def _query(self, sql: str) -> list[dict]:
        return self._query_db(self._ensure_message_db(), sql)

    def _query_db(self, db_name: str, sql: str) -> list[dict]:
        # 所有读路径（轮询/基线/会话列表/历史/名字）的唯一出口 —— 归属闸钉在这里一次到位。
        self._require_port_ownership()
        with self._io_lock:
            r = self._client.post("/QueryDB/execute", json={"optDbName": db_name, "SQL": sql})
            r.raise_for_status()
            body = r.json()
            status = body.get("status")
            desc = str(body.get("desc") or body.get("message") or "")
            if status not in (None, "", 0, "0") and "get database handle" in desc.lower():
                # 冷启动时 g_IsLogin 虽已补成 1，native hook 仍可能尚未执行数据库扫描。
                # 此时直接 QueryDB/execute 只会反复返回“get database handle failed”，会话页
                # 就一直停在“读取会话失败”。GetAllDBName 会触发 searchDatabases；必须与
                # 原查询放在同一个端口锁里，避免和轮询/历史读取并发进入非线程安全的 WCDB。
                # 扫描失败时保留原响应；扫描成功则只重试一次，避免永久重试掩盖真实故障。
                try:
                    probe = self._client.post("/QueryDB/GetAllDBName", json={})
                    probe.raise_for_status()
                    probe.json()
                except (httpx.HTTPError, ValueError):
                    pass
                else:
                    r = self._client.post(
                        "/QueryDB/execute", json={"optDbName": db_name, "SQL": sql}
                    )
                    r.raise_for_status()
                    body = r.json()
        status = body.get("status")
        if status not in (None, "", 0, "0"):
            desc = body.get("desc") or body.get("message") or "QueryDB failed"
            raise RuntimeError(f"{db_name}: {desc}")
        return body.get("data", []) or []

    def _message_db_from_process(self) -> tuple[str, Path | None]:
        """从当前被认领的微信进程找活跃消息库；只读取 OS 打开文件表。"""
        pid = self._verified_pid
        if not pid:
            try:
                from widget.hook_patch import _hook_pid
                pid = int(_hook_pid(self.hook_port()) or 0)
            except Exception:
                pid = 0
        if not pid:
            return "", None
        self._media_pid = pid
        try:
            import psutil
            candidates = []
            for opened in psutil.Process(pid).open_files():
                path = Path(opened.path)
                match = re.fullmatch(r"message_(\d+)\.db", path.name, re.I)
                if match:
                    try:
                        modified = path.stat().st_mtime
                    except OSError:
                        modified = 0.0
                    candidates.append((modified, int(match.group(1)), path))
        except Exception:
            return "", None
        if not candidates:
            return "", None
        path = max(candidates, key=lambda item: (item[0], item[1]))[2]
        # 4.1 当前布局为 <账号>/db_storage/message/message_N.db，而附件在 <账号>/msg；
        # 旧布局也可能把库直接放进 msg。只在祖先目录确实带 msg 子目录时认定，避免猜错账号根。
        account_root = None
        for ancestor in list(path.parents)[:5]:
            if (ancestor / "msg").is_dir():
                account_root = ancestor
                break
        return path.name, account_root

    def _ensure_message_db(self) -> str:
        if self._msg_db_name:
            return self._msg_db_name
        db_name, account_root = self._message_db_from_process()
        probe_sql = (
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name LIKE 'Msg_%' LIMIT 1"
        )

        def queryable(candidate: str) -> bool:
            try:
                self._query_db(candidate, probe_sql)
                return True
            except Exception:
                return False

        # OS 打开文件表只是候选：hook 刚 patch 时可能暂未注册该 handle，双微信进程下也可能
        # 出现“进程打开 message_0，但当前 QueryDB 实际可读 message_1”。必须先经 QueryDB 验证，
        # 不能一看到文件名就永久缓存，否则首次失败后本进程永远卡死在错误库名。
        if db_name and queryable(db_name):
            self._msg_db_name = db_name
            self._account_root = account_root
            self._media.set_account_root(account_root)
            return db_name
        if db_name and self._verified_pid:
            # 已验证进程明确打开了这个库，但 hook 暂时还没注册 handle：只重试本进程候选，
            # 绝不为了“能读”而探到另一微信进程的分库，避免多账号串历史。
            return db_name
        # legacy 单实例的 OS 文件名和 hook 逻辑句柄偶尔不一致（例如进程打开 message_0.db，
        # /QueryDB 却只认 message_1.db）。这个端口只绑定当前一个微信进程，因此可以有限探测
        # 同一 hook 暴露的句柄。查询成功但没有 Msg_* 也算有效：新版微信可能只把消息写进 FTS。
        for candidate in [f"message_{index}.db" for index in range(10)]:
            if candidate != db_name and queryable(candidate):
                self._msg_db_name = candidate
                self._account_root = account_root
                self._media.set_account_root(account_root)
                return candidate
        # legacy hook 尚未就绪时只返回兼容名，不缓存。下一轮基线会重新做 OS 发现+QueryDB 探测；
        # 与 baseline_ready 闸配合，期间不读取消息，更不会从 0 重放历史。
        return MSG_DB

    def _remember_self_alias(self, sender_wxid: str) -> None:
        if sender_wxid and not is_group_account(sender_wxid) \
                and not is_official_or_system_account(sender_wxid):
            self._self_wxid_aliases.add(sender_wxid)

    def _is_self_sender(self, sender_wxid: str, session: str) -> bool:
        """识别消息库里的本人方向，不依赖不可靠的 GetSelfProfile 单一 ID。

        私聊表/FTS 行的 session 永远是会话对方：sender==session 是对方来信，
        sender!=session 是本人从微信客户端或工作台发出的消息。后者同时沉淀成本地库别名，
        让群聊中同一个 sender 能被正确识别为本人。
        """
        if not sender_wxid:
            return False
        profile = self.self_wxid()
        if same_account(sender_wxid, profile) or any(
                same_account(sender_wxid, alias) for alias in self._self_wxid_aliases):
            self._remember_self_alias(sender_wxid)
            return True
        if is_private_conversation(session) and not same_account(sender_wxid, session):
            self._remember_self_alias(sender_wxid)
            return True
        return False

    def _is_at_me(self, source_xml: str) -> bool:
        candidates = {self.self_wxid(), *self._self_wxid_aliases}
        return any(parse_at_me(source_xml, wxid) for wxid in candidates if wxid)

    def list_sessions(self, limit: int = 40) -> list[dict]:
        """微信最近好友/群聊会话（供聊天页左侧列表），排除公众号和系统会话。"""
        # 安全闸拒绝与数据库真实为空是两种状态。拒绝时抛出明确异常，让 UI 保留旧快照
        # 并显示“读取未就绪”，不能伪装成“暂无会话”。
        self._require_port_ownership()
        visible_limit = max(0, int(limit))
        if visible_limit == 0:
            return []
        # SessionTable 里公众号聚合会话很多。若只查 limit 行再过滤，它们会挤掉真正的好友/群聊；
        # 适度预取后再按“可见会话数”截断，仍保持时间顺序且不给 GUI 无界数据。
        query_limit = max(visible_limit, visible_limit * 5)
        sql = ("SELECT username, summary, sort_timestamp FROM SessionTable "
               f"WHERE is_hidden=0 ORDER BY sort_timestamp DESC LIMIT {query_limit}")
        out = []
        for r in self._query_db("session.db", sql):
            u = str(r.get("username", "") or "")
            if not is_supported_conversation(u):
                continue
            out.append({"wxid": u, "summary": str(r.get("summary", "") or ""),
                        "ts": _to_int(r.get("sort_timestamp"))})
            if len(out) >= visible_limit:
                break
        return out

    def display_names(self, wxids: list[str]) -> dict[str, str]:
        """把一批 wxid 解析成人看得懂的名字：备注优先→昵称→查不到才退回 wxid。"""
        names = {w: w for w in wxids}      # 默认退回 wxid，保证每个都有值
        wanted = [w for w in wxids if w]
        if not wanted or not self.guard_ok():
            return names
        in_list = ",".join("'" + w.replace("'", "''") + "'" for w in wanted)
        try:
            rows = self._query_db("contact.db",
                f"SELECT username, remark, nick_name FROM contact WHERE username IN ({in_list})")
        except Exception:
            return names
        for r in rows:
            u = str(r.get("username", "") or "")
            name = str(r.get("remark", "") or "") or str(r.get("nick_name", "") or "")
            if u and name:
                names[u] = name
        return names

    def active_session(self) -> str | None:
        """当前/最近活跃的好友或群聊（跟随用）：正在打字优先，否则最近清未读。"""
        if not self.guard_ok():
            return None                    # 端口易主：别跟着别人的微信走
        rows = self._query_db("session.db",
            "SELECT username, draft, last_clear_unread_timestamp FROM SessionTable")
        best, best_ts = None, -1
        for r in rows:
            u = str(r.get("username", "") or "")
            if not is_supported_conversation(u):
                continue
            if str(r.get("draft", "") or ""):     # 有草稿=正在此会话打字，最强信号
                return u
            ts = _to_int(r.get("last_clear_unread_timestamp"))
            if ts > best_ts:
                best, best_ts = u, ts
        return best

    # ---- 映射 / 表 ----

    def _refresh_mapping(self) -> None:
        rows = []
        for db_name in (self._ensure_message_db(), "session.db"):
            try:
                rows = self._query_db(db_name, _NAME2ID_SQL)
            except Exception:
                rows = []
            if rows:
                break
        id2name, md52name = {}, {}
        for row in rows:
            name = str(row.get("user_name", ""))
            if not name:
                continue
            id2name[_to_int(row.get("rid"))] = name
            md52name[_md5(name)] = name
        self._id2name, self._md52name = id2name, md52name

    def _list_suffixes(self) -> list[str]:
        try:
            return self._list_suffixes_strict()
        except Exception:
            return []

    def _list_suffixes_strict(self) -> list[str]:
        """列出旧式消息表；与宽容版分开，使启动基线能区分“确实为空”和“读取失败”。"""
        rows = self._query(_LIST_TABLES_SQL)
        suffixes = []
        for row in rows:
            name = str(row.get("name", "") or "")
            suffix = name[4:] if name.startswith("Msg_") else ""
            if _LEGACY_SUFFIX_RE.fullmatch(suffix):
                suffixes.append(suffix)
        self._legacy_suffixes = set(suffixes)
        return suffixes

    def _refresh_fts_mapping(self) -> None:
        try:
            rows = self._query_db(FTS_DB, _FTS_NAME2ID_SQL)
        except Exception:
            rows = []
        id2name, name2id = {}, {}
        for row in rows:
            name = str(row.get("username", "") or row.get("user_name", "") or "")
            rid = _to_int(row.get("rid") or row.get("rowid"))
            if not name or rid <= 0:
                continue
            id2name[rid] = name
            name2id[name] = rid
        self._fts_id2name, self._fts_name2id = id2name, name2id
        self._discover_fts_self_alias()

    def _discover_fts_self_alias(self) -> None:
        """从近期私聊的出向行学习消息库本人 ID，解决 profile ID 与 FTS ID 不一致。"""
        for table in self._list_fts_tables():
            try:
                rows = self._query_db(
                    FTS_DB,
                    f"SELECT session_id, sender_id FROM {table} "
                    "WHERE sender_id != session_id ORDER BY rowid DESC LIMIT 200",
                )
            except Exception:
                continue
            for row in rows:
                sender = self._fts_id2name.get(_to_int(row.get("sender_id")), "")
                session = self._fts_id2name.get(_to_int(row.get("session_id")), "")
                if sender and is_private_conversation(session) \
                        and not same_account(sender, session):
                    self._remember_self_alias(sender)
                    return

    def _list_fts_tables(self, *, refresh: bool = True) -> list[str]:
        # 轮询/启动基线会持续刷新表清单；用户点开历史时复用它，避免再次查询 sqlite_master。
        if not refresh and self._fts_tables is not None:
            return list(self._fts_tables)
        try:
            rows = self._query_db(FTS_DB, _FTS_TABLES_SQL)
        except Exception:
            return []
        tables = []
        for row in rows:
            name = str(row.get("name", "") or "")
            m = _FTS_TABLE_RE.match(name)
            if m:
                tables.append((int(m.group(1)), name))
        result = [name for _, name in sorted(tables)]
        self._fts_tables = result
        return list(result)

    def _before_start_clause(self) -> str:
        return f" WHERE create_time < {self._startup_cutoff}" if self._startup_cutoff else ""

    def _baseline_legacy_cursor(self, suffix: str) -> int:
        rows = self._query(
            f"SELECT MAX(local_id) AS mx FROM Msg_{suffix}{self._before_start_clause()}"
        )
        return _to_int(rows[0].get("mx") if rows else None, 0)

    def _baseline_fts_cursor(self, table: str) -> int:
        rows = self._query_db(
            FTS_DB,
            f"SELECT MAX(rowid) AS mx FROM {table}{self._before_start_clause()}",
        )
        return _to_int(rows[0].get("mx") if rows else None, 0)

    def baseline(self) -> bool:
        """建立本次启动基线，只允许启动时刻之后的消息进入 AI。

        旧实现会保留小于当前 MAX 的持久化游标，等同于把软件关闭期间的消息当作待续传任务；
        客服若已在微信里手工回复，重启后就会再次全部回复。现在每次启动都以
        ``create_time < startup_cutoff`` 的末行重新划线。消息表读取失败时返回未就绪，轮询
        必须继续等待，不能从 cursor=0 冒险读取历史。
        """
        if not self.guard_ok():
            self._baseline_ready = False
            return False                    # 端口易主：绝不拿别人库里的 local_id 当自己的基线
        suffixes = self._list_suffixes_strict()  # 失败必须抛出，不能与“空账号”混为一谈
        if suffixes and not self._md52name:
            self._refresh_mapping()
        snapshot: dict[str, int] = {}
        for suffix in suffixes:
            snapshot[suffix] = self._baseline_legacy_cursor(suffix)
        # 新版微信可能同时保留旧式 Msg_* 表和 FTS 表，而且某些会话只写入 FTS。
        # 两套游标都必须在冷启动时打到当前末尾，否则启用混合轮询后会重放历史消息。
        for table in self._list_fts_tables():
            snapshot[f"fts:{table}"] = self._baseline_fts_cursor(table)
        self._cursors = snapshot                 # 丢弃上次进程/旧消息库留下的游标
        self._baseline_ready = True
        self._save_state()
        return True

    # ---- 轮询 ----

    def _build_msg(self, suffix: str, row: dict) -> InboundMsg | None:
        if self._startup_cutoff and _to_int(row.get("create_time")) < self._startup_cutoff:
            return None                           # 游标异常的第二道保险：启动前历史永不进 Pipeline
        if _to_int(row.get("local_type")) != _TEXT_TYPE:
            return None
        sid = _to_int(row.get("real_sender_id"))
        sender_wxid = self._id2name.get(sid, "")
        session = self._md52name.get(suffix, "")
        if not sender_wxid or not session:            # 未知 id/会话 → 刷新一次映射再试
            self._refresh_mapping()
            sender_wxid = self._id2name.get(sid, sender_wxid)
            session = self._md52name.get(suffix, session)
        if is_official_or_system_account(sender_wxid) or \
                is_official_or_system_account(session):
            return None
        if self._is_self_sender(sender_wxid, session):
            return None                               # 自己发的，不接
        text = decode_content(row.get("mc"), row.get("ct"), row.get("mc_hex"))
        is_group = is_group_account(session or sender_wxid)
        if is_group:
            text = strip_group_sender_prefix(text, sender_wxid)
        if not text.strip():
            if _to_int(row.get("ct")) != 0:
                text = _UNDECODABLE          # 压缩但解不出（如字典压缩）→ 不丢，占位交人工
            else:
                return None                  # 真空白文本 → 丢
        if self._is_own_echo(text):          # 自己刚发出去的回声 → 丢，防自问自答死循环
            return None
        contact_id = session or sender_wxid           # 会话名反查不到时退化为发言人（按私聊处理）
        is_group = is_group_account(contact_id)
        at_me = False
        if is_group:                                  # 群消息：解压 source 判断是否 @我
            src_xml = decode_content(row.get("src"), row.get("src_ct"), row.get("src_hex"))
            at_me = self._is_at_me(src_xml)
        return InboundMsg(
            channel=self.channel,
            msg_id=f"{suffix}:{row.get('local_id')}",
            contact_id=contact_id,
            sender_id=sender_wxid or contact_id,
            text=text,
            is_group=is_group,
            at_me=at_me,
            timestamp=_to_int(row.get("create_time")),
        )

    def _build_fts_msg(self, table: str, row: dict) -> InboundMsg | None:
        if self._startup_cutoff and _to_int(row.get("create_time")) < self._startup_cutoff:
            return None                           # FTS 同样拒绝启动前历史
        if _to_int(row.get("local_type")) != _TEXT_TYPE:
            return None
        if not self._fts_id2name:
            self._refresh_fts_mapping()
        sender_wxid = self._fts_id2name.get(_to_int(row.get("sender_id")), "")
        session = self._fts_id2name.get(_to_int(row.get("session_id")), "")
        if not sender_wxid or not session:
            self._refresh_fts_mapping()
            sender_wxid = self._fts_id2name.get(_to_int(row.get("sender_id")), sender_wxid)
            session = self._fts_id2name.get(_to_int(row.get("session_id")), session)
        if is_official_or_system_account(sender_wxid) or \
                is_official_or_system_account(session):
            return None
        if self._is_self_sender(sender_wxid, session):
            return None
        text = str(row.get("acontent", "") or "").strip()
        is_group = is_group_account(session or sender_wxid)
        if is_group:
            text = strip_group_sender_prefix(text, sender_wxid).strip()
        if not text:
            return None
        if self._is_own_echo(text):          # 自己刚发出去的回声 → 丢，防自问自答死循环
            return None
        contact_id = session or sender_wxid
        is_group = is_group_account(contact_id)
        msg_local_id = row.get("message_local_id") or row.get("rid") or row.get("rowid")
        return InboundMsg(
            channel=self.channel,
            msg_id=f"{table}:{msg_local_id}",
            contact_id=contact_id,
            sender_id=sender_wxid or contact_id,
            text=text,
            is_group=is_group,
            at_me=False,
            timestamp=_to_int(row.get("create_time")),
        )

    def _read_conversation_legacy(self, contact_wxid: str, limit: int) -> list[dict]:
        if not self._id2name:
            self._refresh_mapping()
        suffix = _md5(contact_wxid)
        if suffix not in self._list_suffixes():
            return []
        rows = self._query(_HISTORY_SQL.format(suffix=suffix, limit=int(limit)))
        out: list[dict] = []
        for row in rows:
            sid = _to_int(row.get("real_sender_id"))
            sender_wxid = self._id2name.get(sid, "")
            is_self = self._is_self_sender(sender_wxid, contact_wxid)
            content = decode_content(row.get("mc"), row.get("ct"), row.get("mc_hex"))
            if is_group_account(contact_wxid):
                content = strip_group_sender_prefix(content, sender_wxid)
            raw_type = _to_int(row.get("local_type"))
            if not content.strip() and raw_type == _TEXT_TYPE:
                content = _UNDECODABLE if _to_int(row.get("ct")) != 0 else ""
                if not content:
                    continue
            message = parse_wechat_message(raw_type, content)
            message.update({"is_self": is_self, "ts": _to_int(row.get("create_time")),
                            "local_id": _to_int(row.get("local_id")),
                            "preview_pid": self._media_pid,
                            "sender_id": sender_wxid or (
                                self.self_wxid() if is_self else contact_wxid)})
            out.append(self._media.enrich(message))
        return self._decorate_history_senders(contact_wxid, out)

    def _read_conversation_fts(self, contact_wxid: str, limit: int) -> list[dict]:
        if not self._fts_name2id:
            self._refresh_fts_mapping()
        session_id = self._fts_name2id.get(contact_wxid)
        if not session_id:
            return []
        tables = self._list_fts_tables(refresh=False)
        if not tables:
            return []
        selects = [
            "SELECT '{table}' AS table_name, rowid AS rid, message_local_id, "
            "local_type, session_id, sender_id, create_time, acontent "
            "FROM {table} WHERE session_id={session_id}".format(table=table, session_id=session_id)
            for table in tables
        ]
        sql = (
            "SELECT * FROM (" + " UNION ALL ".join(selects) + ") "
            f"ORDER BY create_time DESC, rid DESC LIMIT {int(limit)}"
        )
        rows = self._query_db(FTS_DB, sql)
        rows.reverse()
        out: list[dict] = []
        for row in rows:
            content = str(row.get("acontent", "") or "").strip()
            raw_type = _to_int(row.get("local_type"))
            if not content and raw_type == _TEXT_TYPE:
                continue
            sender_wxid = self._fts_id2name.get(_to_int(row.get("sender_id")), "")
            if is_group_account(contact_wxid):
                content = strip_group_sender_prefix(content, sender_wxid).strip()
            message = parse_wechat_message(raw_type, content)
            message.update({
                "is_self": self._is_self_sender(sender_wxid, contact_wxid),
                "ts": _to_int(row.get("create_time")),
                "local_id": _to_int(row.get("message_local_id") or row.get("rid")),
                "sender_id": sender_wxid,
            })
            out.append(message)
        return self._decorate_history_senders(contact_wxid, out)

    def _poll_legacy_rows(self, suffixes: list[str]) -> list[dict]:
        """Read all known legacy conversations with one QueryDB request when possible."""
        if not suffixes:
            return []
        if len(suffixes) == 1:
            suffix = suffixes[0]
            rows = self._query(_POLL_SQL.format(
                suffix=suffix, cursor=self._cursors.get(suffix, 0),
            ))
            return [dict(row, table_suffix=suffix) for row in rows]
        selects = []
        for suffix in suffixes:
            escaped = suffix.replace("'", "''")
            selects.append(
                "SELECT '{suffix}' AS table_suffix, local_id, local_type, "
                "real_sender_id, create_time, message_content AS mc, "
                "WCDB_CT_message_content AS ct, HEX(message_content) AS mc_hex, "
                "source AS src, WCDB_CT_source AS src_ct, HEX(source) AS src_hex "
                "FROM Msg_{table} WHERE local_id > {cursor}".format(
                    suffix=escaped,
                    table=suffix,
                    cursor=self._cursors.get(suffix, 0),
                )
            )
        selects = [f"SELECT * FROM ({select} ORDER BY local_id ASC LIMIT 200)"
                   for select in selects]
        sql = (
            "SELECT * FROM (" + " UNION ALL ".join(selects) + ") "
            "ORDER BY create_time ASC, local_id ASC"
        )
        try:
            return self._query(sql)
        except Exception:
            # Older/modified hook SQLite builds may reject a large compound query. Preserve
            # compatibility, but use the fast single-request path on normal WeChat databases.
            rows: list[dict] = []
            for suffix in suffixes:
                current = self._query(_POLL_SQL.format(
                    suffix=suffix, cursor=self._cursors.get(suffix, 0),
                ))
                rows.extend(dict(row, table_suffix=suffix) for row in current)
            return rows

    def _poll_fts_rows(self, tables: list[str]) -> list[dict]:
        """Read all FTS shards in one QueryDB request instead of one HTTP call per shard."""
        if not tables:
            return []
        selects = []
        for table in tables:
            cursor = self._cursors.get(f"fts:{table}", 0)
            escaped = table.replace("'", "''")
            selects.append(
                "SELECT '{table_name}' AS table_name, rowid AS rid, message_local_id, "
                "local_type, session_id, sender_id, create_time, acontent "
                "FROM {table} WHERE rowid > {cursor}".format(
                    table_name=escaped, table=table, cursor=cursor,
                )
            )
        selects = [f"SELECT * FROM ({select} ORDER BY rowid ASC LIMIT 200)"
                   for select in selects]
        sql = (
            "SELECT * FROM (" + " UNION ALL ".join(selects) + ") "
            "ORDER BY create_time ASC, rid ASC"
        )
        try:
            return self._query_db(FTS_DB, sql)
        except Exception:
            rows: list[dict] = []
            for table in tables:
                cursor = self._cursors.get(f"fts:{table}", 0)
                rows.extend(self._query_db(
                    FTS_DB,
                    f"SELECT '{table}' AS table_name, rowid AS rid, message_local_id, "
                    f"local_type, session_id, sender_id, create_time, acontent "
                    f"FROM {table} WHERE rowid > {cursor} ORDER BY rowid ASC LIMIT 200",
                ))
            return rows

    def _decorate_history_senders(self, contact_wxid: str, messages: list[dict]) -> list[dict]:
        """补齐历史消息的真实发言人昵称；群聊必须按成员区分，不能统一显示成“客户”。"""
        sender_ids = sorted({str(message.get("sender_id", "") or "")
                             for message in messages if message.get("sender_id")})
        try:
            names = self.display_names(sender_ids)
        except Exception:
            names = {sender_id: sender_id for sender_id in sender_ids}
        is_group = is_group_account(contact_wxid)
        for message in messages:
            sender_id = str(message.get("sender_id", "") or "")
            is_self = bool(message.get("is_self"))
            message["is_group"] = is_group
            message["sender_name"] = "我" if is_self else (
                names.get(sender_id) or sender_id or ("群成员" if is_group else "客户"))
        return messages

    def read_conversation(self, contact_wxid: str, limit: int = 80) -> list[dict]:
        """读与某联系人的真实微信聊天（最近 limit 条，按时间正序）。
        返回结构化消息；图片、表情、文件、链接/应用卡片、视频和语音均保留类型及元数据。"""
        if not self.guard_ok():
            return []                      # 端口易主：绝不把别人的聊天记录显示成本号的
        # 首次读取已识别出该联系人只存在于新版 FTS 存储后，后续点击直接走 FTS，省掉一次
        # `sqlite_master` 旧表扫描。QueryDB 单次超时可达数秒，这个无效探测会直接放大白屏等待。
        suffix = _md5(contact_wxid)
        legacy_known = (
            suffix in self._legacy_suffixes
            if self._legacy_suffixes is not None else None
        )
        fts_known = contact_wxid in self._fts_name2id if self._fts_name2id else False
        if fts_known and legacy_known is False:
            try:
                return self._read_conversation_fts(contact_wxid, int(limit))
            except Exception:
                return []
        try:
            legacy = self._read_conversation_legacy(contact_wxid, int(limit))
        except Exception:
            legacy = []
        if legacy:
            return legacy
        try:
            return self._read_conversation_fts(contact_wxid, int(limit))
        except Exception:
            return []

    def _poll_once_fts(
        self, excluded_session_suffixes: set[str] | None = None
    ) -> list[InboundMsg]:
        excluded = excluded_session_suffixes or set()
        if not self.guard_ok():
            return []                      # 端口易主：一条都不发出去，游标一格都不推进
        tables = self._list_fts_tables()
        unknown = [t for t in tables if f"fts:{t}" not in self._cursors]
        if not self._strangers_confirmed(unknown):
            return []                  # 与 poll_once 同一条判据（陌生表 = 触发点）
        for table in unknown:
            # 表可能在 hook 就绪后才变得可见；只基线启动前的行，启动后的第一条仍会被处理。
            self._cursors[f"fts:{table}"] = self._baseline_fts_cursor(table)
        if not self._fts_id2name:
            self._refresh_fts_mapping()
        out: list[InboundMsg] = []
        pending: list[tuple[str, dict]] = []
        changed = False
        rows_by_table: dict[str, list[dict]] = {table: [] for table in tables}
        for row in self._poll_fts_rows(tables):
            table = str(row.get("table_name") or "")
            if table in rows_by_table:
                rows_by_table[table].append(row)
        for table in tables:
            key = f"fts:{table}"
            cursor = self._cursors.get(key, 0)
            new_cursor = cursor
            for row in rows_by_table[table]:
                new_cursor = max(new_cursor, _to_int(row.get("rid") or row.get("rowid"), cursor))
                pending.append((table, row))
            if new_cursor != cursor or key not in self._cursors:
                self._cursors[key] = new_cursor
                changed = True
        pending.sort(key=lambda item: (
            _to_int(item[1].get("create_time")),
            _to_int(item[1].get("rid") or item[1].get("rowid")),
        ))
        for table, row in pending:
            msg = self._build_fts_msg(table, row)
            # 同一会话若有旧式 Msg_<md5> 表，就由旧式轮询负责；这里只补齐
            # “仅存在于 FTS”的会话，避免两套存储同时产出造成重复回复。
            if msg is not None and _md5(msg["contact_id"]) not in excluded:
                out.append(msg)
        if changed:
            self._save_state()
        return out

    def _strangers_confirmed(self, keys: list[str]) -> bool:
        """★便宜触发 + 昂贵确认（本轮 Critical，R1）★ 出现了本渠道**从没见过**的会话表 —— 放行吗？

        身份漂移在**它造成伤害的那一点**上有一个精确且免费的信号：轮询发现一张陌生会话表，
        而自己手里**已经有基线**。那正是「整份陌生历史被当成新消息」的入口：`_cursors` 里没有
        这个 key → cursor=0 → 每个会话最多 200 条历史全被吐进 Pipeline → Bridge 上传到本店
        后端/LLM、永久写进 acs.db、摆上待人工页。发送那一半堵住了，**已经传上去的数据撤不回来**。

        所以：稳态（只碰认识的表）**零探测**；一旦冒出陌生表，先付**一次**昂贵确认，
        没过就一条不吐、游标一格不动。真·新客户会偶尔触发一次确认 —— 那是这个设计愿意付的价钱。

        两种情况不设这道确认（都不存在「见过/没见过」的可比基准）：
          · legacy 单实例（无已验证 pid）—— 规则 4，探针一次都不许调；
          · 冷启动还没有任何游标 —— 这个 adapter 是认领链刚刚验完身份才造出来的，
            而且 `start()` 会先 `baseline()` 把当前末尾记下来，不会重放历史。
        """
        if not keys:
            return True
        if not self._verified_pid or not self._cursors:
            return True
        return self._probe_identity()

    def poll_once(self) -> list[InboundMsg]:
        if not self._baseline_ready:
            if not self.baseline():
                return []
            return []                           # 建好基线这一轮也不读，下一轮再只取新消息
        if not self.guard_ok():
            # ★H2★ 端口易主：新主人的会话表 md5 本号一个都不认识 → cursor=0 → 整份历史
            # 会被当成新消息灌进本号的租户。所以这里空手而归，且**绝不推进任何游标**。
            return []
        suffixes = self._list_suffixes()
        if not suffixes:
            return self._poll_once_fts()
        unknown = [s for s in suffixes if s not in self._cursors]
        if not self._strangers_confirmed(unknown):
            return []                  # 陌生会话 + 身份确认没过：一条不吐，游标一格不动
        for suffix in unknown:
            self._cursors[suffix] = self._baseline_legacy_cursor(suffix)
        if not self._md52name:
            self._refresh_mapping()
        out: list[InboundMsg] = []
        changed = False
        rows_by_suffix: dict[str, list[dict]] = {suffix: [] for suffix in suffixes}
        for row in self._poll_legacy_rows(suffixes):
            suffix = str(row.get("table_suffix") or "")
            if suffix in rows_by_suffix:
                rows_by_suffix[suffix].append(row)
        for suffix in suffixes:
            cursor = self._cursors.get(suffix, 0)
            new_cursor = cursor
            for row in rows_by_suffix[suffix]:
                new_cursor = max(new_cursor, _to_int(row.get("local_id"), cursor))
                msg = self._build_msg(suffix, row)
                if msg is not None:
                    out.append(msg)
            if new_cursor != cursor or suffix not in self._cursors:
                self._cursors[suffix] = new_cursor
                changed = True
        if changed:
            self._save_state()
        out.extend(self._poll_once_fts(excluded_session_suffixes=set(suffixes)))
        out.sort(key=lambda msg: (msg.get("timestamp", 0), msg.get("msg_id", "")))
        return self._dedupe_cross_store(out)

    def _dedupe_cross_store(self, messages: list[InboundMsg]) -> list[InboundMsg]:
        """只消除 legacy/FTS 间的同一副本，不吞掉客户真的连续发了两次。"""
        unique: list[InboundMsg] = []
        for msg in messages:
            source = "fts" if str(msg.get("msg_id", "")).startswith("fts:") else "legacy"
            key = (
                msg.get("channel", ""),
                msg.get("contact_id", ""),
                msg.get("sender_id", ""),
                msg.get("text", "").strip(),
                int(msg.get("timestamp", 0) or 0),
            )
            previous_source = self._cross_store_seen.get(key)
            if previous_source is not None and previous_source != source:
                continue
            unique.append(msg)
            if previous_source is None:
                self._cross_store_seen[key] = source
                self._cross_store_order.append(key)
                if len(self._cross_store_order) > 4000:
                    expired = self._cross_store_order.popleft()
                    self._cross_store_seen.pop(expired, None)
        return unique

    # ---- 生命周期 ----

    def start(self, on_message: Callable[[InboundMsg], None]) -> None:
        self._on_message = on_message
        self._startup_cutoff = int(time.time())
        self._baseline_ready = False
        if self._patch_login:
            try:
                from widget import hook_patch
                # M4：patch 必须打在**本实例自己的端口**上（端口盲=恒 30001 会补错号），
                # 且只打在**身份已被认领链验证过**的那个 pid 上。
                #
                # 审查修复（整分支审查 Critical）：这里原来是 `ensure_login_patched(port=…)`
                # ——不带 pid。hook_patch 因此认为 pid 不是调用方显式给的，跳过 pid/端口配对闸，
                # 直接写「此刻谁在听这个端口」。而 adapter 手里的端口来自 **instances.yaml
                # 配置**（未经验证）：客户手动登了个没配置的号抢到 30001、或微信重启后端口易主，
                # 就会把 g_IsLogin 写进一个我们明确拒绝接管的陌生微信 → 写崩它、弹「上次异常
                # 退出，是否修复」框。所以：有已验证 pid 才写；没有就交给 manager，自己一个字节不碰。
                port = _port_from_base_url(self.hook_base_url)
                if self._verified_pid:
                    self._last_patch = hook_patch.ensure_login_patched(
                        pid=self._verified_pid, port=port or hook_patch.HOOK_PORT)
                elif self._legacy_patch:
                    self._last_patch = (hook_patch.ensure_login_patched(port=port) if port
                                        else hook_patch.ensure_login_patched())
                else:
                    self._last_patch = ("patch skipped: 无已验证 pid —— 本实例的 g_IsLogin "
                                        "只由 WeChatManager 认领后写，绝不按配置端口盲写")
            except Exception as e:                    # patch 失败不阻塞启动（收消息会降级）
                self._last_patch = f"patch skipped: {e}"
        self._load_state()
        self._stop.clear()
        # 基线会扫描消息表并逐表读取 MAX 游标，真机上可能持续数秒。这里不能在 GUI 启动线程
        # 同步执行，否则工作台虽已 show()，事件循环却迟迟起不来，会话页也无法先展示记录。
        # poll_once() 自带“基线未成功前不读取消息”的安全闸，交给轮询线程完成不会重放历史。
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name=f"wechat-poll-{self.channel}"
        )
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            baseline_was_ready = self._baseline_ready
            try:
                msgs = self.poll_once()
                if self._last_poll_error:
                    print(f"[hook] poll recovered channel={self.channel}", flush=True)
                    self._last_poll_error = ""
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                if error != self._last_poll_error:
                    print(
                        f"[hook] poll failed channel={self.channel}: {error}",
                        flush=True,
                    )
                    self._last_poll_error = error
                msgs = []                             # 轮询异常（网络/畸形响应）→ 本轮跳过，下轮重试
            for msg in msgs:
                if self._on_message:
                    try:
                        self._on_message(msg)
                    except Exception as exc:
                        print(
                            f"[hook] message handler failed channel={self.channel}: "
                            f"{type(exc).__name__}: {exc}",
                            flush=True,
                        )
                        pass                          # 单条处理失败不拖累同批其他消息
            # 首次基线刚建立时立刻进入一次增量读取，不额外空等 poll_interval；基线 SQL 已用
            # startup_cutoff 排除启动前历史，所以这一步既快又不会把离线积压当新消息。
            if not baseline_was_ready and self._baseline_ready:
                continue
            self._stop.wait(self.cfg.poll_interval_s)

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        identity_thread = self._identity_refresh_thread
        if identity_thread is not None and identity_thread is not threading.current_thread():
            identity_thread.join(timeout=0.5)

    # ---- 游标持久化（可选，避免重启后重放/漏消息） ----

    def _load_state(self) -> None:
        if not self._state_path or not self._state_path.exists():
            return
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
            self._cursors.update({str(k): _to_int(v) for k, v in data.items()})
        except Exception:
            pass

    def _save_state(self) -> None:
        if not self._state_path:
            return
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            self._state_path.write_text(json.dumps(self._cursors), encoding="utf-8")
        except Exception:
            pass
