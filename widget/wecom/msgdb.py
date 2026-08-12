"""企微本地消息库(message.db) 解密读取 —— 解析层。

背景（RE 里程碑，2026-07-28 打通，详见 docs/wecom-local-msgdb-re-plan.md）：
企微把 message.db 用 WCDB 自研 cipher 加密（非标准 SQLCipher，密钥不驻留明文，外部解密走不通）。
但企微进程内**已解密打开**该库。探针 DLL `native/wecom_hook/col_probe_dll.c` inline hook 了企微内部
`sqlite3_column_text`（Ghidra 坐实 = FUN_00624740，RVA 0x224740），在企微读消息历史时**被动收割**
每列解密后的明文，进环形缓冲，`GET /collog?since=<seq>` 拉出。**只读、不写企微库、无锁（避 HangMonitor）。**

本模块是纯解析层（无网络、可离线单测）：把 `/collog` 的**原始 HTTP body 字节**还原成一列列解密值，
再分类成聊天正文 / 消息 envelope(protobuf: clientmsgid+svrid+时间) / 菜单项 / 卡片 / 二进制，
供 `WeComHookAdapter` 把「连接前的历史 + 人工手打」反哺进知识库/话术库。

⚠️ 为什么要按字节解析而不是 json.loads：DLL 的 collog_json 用 jesc 把 **>0x20 的原始字节直接塞进
JSON 串**（含 >127 的 UTF-8 尾字节），这不是合法 UTF-8 JSON。json.loads(errors='replace') 会把高字节
毁成替换符、protobuf 长度漂移。所以这里在**字节层**手工反转义（与 col_probe 的 jesc 一一对应）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# col_probe 每条值前缀 "[f=<hex> e=<dec> n=<dec>]"，见 col_probe_dll.c::on_col
_HEADER = re.compile(rb"^\[f=([0-9a-fA-F]+) e=(-?\d+) n=(\d+)\]")
_CLIENTMSGID = re.compile(rb"(CAEQ[A-Za-z0-9+/]{12,}={0,2})")
_SVRID = re.compile(rb"^\s*(\d{15,20})\s*$")
# 卡片/菜单噪声特征（非聊天正文）
_CARD_MARKS = (b"http", b"wwcdn", b"qpic.cn", b"qlogo.cn", b"node/wework",
               b'{"', b"crm_daily_report", b"_report_", b"foldericon")


@dataclass
class HarvestedRow:
    """一条被收割的解密列值。"""
    flags: int
    enc: int
    nbytes: int
    raw: bytes                       # 去掉前缀头之后的值字节
    kind: str                        # text|envelope|menu|card|binary
    text: str | None = None          # kind==text 时的明文
    clientmsgid: str | None = None   # kind==envelope 时的客户端消息 id(base64)
    svrid: int | None = None         # kind==envelope 时的服务端消息 id(int64)


@dataclass
class HarvestResult:
    rows: list[HarvestedRow] = field(default_factory=list)

    @property
    def texts(self) -> list[str]:
        """去重后的聊天正文（保序）。"""
        out, seen = [], set()
        for r in self.rows:
            if r.kind == "text" and r.text and r.text not in seen:
                seen.add(r.text)
                out.append(r.text)
        return out

    @property
    def clientmsgids(self) -> list[str]:
        out, seen = [], set()
        for r in self.rows:
            if r.clientmsgid and r.clientmsgid not in seen:
                seen.add(r.clientmsgid)
                out.append(r.clientmsgid)
        return out

    @property
    def svrids(self) -> list[int]:
        out, seen = [], set()
        for r in self.rows:
            if r.svrid is not None and r.svrid not in seen:
                seen.add(r.svrid)
                out.append(r.svrid)
        return out


def unescape_collog(body: bytes) -> list[bytes]:
    """从 `/collog` 原始响应体里字节级抽出 `"txt":[...]` 的每个元素，反 jesc 成原始字节。

    jesc 规则（col_probe_dll.c::jesc，需一一对应反转）：
        `"`→`\\"`  `\\`→`\\\\`  `\\n`→`\\n`  `\\r`→丢弃  c<0x20→`\\u00xx`  其余字节原样。
    因此反转义时：`\\uXXXX`→取低字节；`\\n`→0x0a；`\\"`/`\\\\`→字面；其余原样。
    """
    key = body.find(b'"txt":[')
    if key < 0:
        return []
    i = key + len(b'"txt":[')
    out: list[bytes] = []
    cur = bytearray()
    instr = False
    n = len(body)
    while i < n:
        c = body[i]
        if not instr:
            if c == 0x22:            # opening quote
                instr = True
                cur = bytearray()
                i += 1
                continue
            if c == 0x5D:            # ] end of array
                break
            i += 1
            continue
        # inside a string
        if c == 0x5C:                # backslash escape
            if i + 1 >= n:
                break
            nx = body[i + 1]
            if nx == 0x75:           # \uXXXX -> low byte
                if i + 5 < n:
                    try:
                        cur.append(int(body[i + 2:i + 6], 16) & 0xFF)
                    except ValueError:
                        pass
                    i += 6
                    continue
                break
            if nx == 0x6E:           # \n
                cur.append(0x0A)
            else:                    # \" \\ or any other escaped literal
                cur.append(nx)
            i += 2
            continue
        if c == 0x22:                # closing quote
            instr = False
            out.append(bytes(cur))
            i += 1
            continue
        cur.append(c)
        i += 1
    return out


def _split_header(value: bytes) -> tuple[int, int, int, bytes]:
    """拆出 col_probe 前缀头，返回 (flags, enc, nbytes, payload)。无头则 flags=enc=nbytes=0。"""
    m = _HEADER.match(value)
    if not m:
        return 0, 0, 0, value
    flags = int(m.group(1), 16)
    enc = int(m.group(2))
    nbytes = int(m.group(3))
    return flags, enc, nbytes, value[m.end():]


_MENU_EN = re.compile(rb"[A-Za-z]{4,}")

def _looks_like_menu(payload: bytes) -> bool:
    """企微「会话工具栏」菜单项 protobuf：`\\x08<id>\\x12\\x0c<中文名>...:\\x15<English>@\\x01p`。

    特征：首字节是 protobuf tag(<0x20)，`:` 后含（可能带 varint 长度前缀的）ASCII 英文名
    （Company Business Card / Rapid reply / Product Album / LIVE / Customer Info…）。
    这类是 UI 注册表，不是聊天正文，判为噪声。
    """
    if not payload or payload[0] >= 0x20:
        return False
    j = payload.find(b":")
    if j < 0:
        return False
    return bool(_MENU_EN.search(payload[j + 1:]))


def classify(value: bytes) -> HarvestedRow:
    """把一条带头的收割值分类成 HarvestedRow。"""
    flags, enc, nbytes, payload = _split_header(value)

    # 1) envelope：含 clientmsgid(CAEQ…) 或纯 int64 svrid
    cid = _CLIENTMSGID.search(payload)
    sm = _SVRID.match(payload)
    if cid or sm:
        svr = int(sm.group(1)) if sm else None
        # envelope 里也可能带 svrid（少见），尽量抽
        if svr is None:
            sm2 = re.search(rb"\b(\d{15,20})\b", payload)
            if sm2:
                svr = int(sm2.group(1))
        return HarvestedRow(flags, enc, nbytes, payload, "envelope",
                            clientmsgid=cid.group(1).decode() if cid else None,
                            svrid=svr)

    # 2) card / 菜单：噪声
    if any(mk in payload for mk in _CARD_MARKS):
        return HarvestedRow(flags, enc, nbytes, payload, "card")
    if _looks_like_menu(payload):
        return HarvestedRow(flags, enc, nbytes, payload, "menu")

    # 3) text：明文聊天正文（首字节可打印/CJK 前导，整体 UTF-8 可解、可读比例高）
    if payload and (payload[0] >= 0x20):
        try:
            t = payload.decode("utf-8")
        except UnicodeDecodeError:
            t = None
        if t is not None:
            printable = sum(1 for ch in t if ch >= " " or ch == "\n")
            s = t.strip()
            has_cjk = any("一" <= ch <= "鿿" for ch in s)
            # 可读比例≥0.8，且不是单个 ASCII 字符（截断 protobuf 字段的噪声，如孤零零的 "3"）
            if s and printable / max(len(t), 1) >= 0.8 and (len(s) >= 2 or has_cjk):
                return HarvestedRow(flags, enc, nbytes, payload, "text", text=t)

    # 4) 其余二进制
    return HarvestedRow(flags, enc, nbytes, payload, "binary")


_CURSOR = re.compile(rb'"cursor"\s*:\s*(\d+)')


def parse_cursor(body: bytes) -> int:
    """从 `/collog` 响应尾部抽 `"cursor":N`（DLL 单调 seq），失败返回 0。"""
    m = _CURSOR.search(body)
    return int(m.group(1)) if m else 0


def harvest(body: bytes) -> HarvestResult:
    """`/collog` 原始响应体 → 结构化收割结果。纯函数、可离线单测。"""
    res = HarvestResult()
    for value in unescape_collog(body):
        res.rows.append(classify(value))
    return res


def _to_int(v, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _varint(b: bytes, i: int) -> tuple[int, int]:
    v = 0
    s = 0
    while i < len(b):
        x = b[i]
        i += 1
        v |= (x & 0x7f) << s
        s += 7
        if not (x & 0x80):
            break
    return v, i


def parse_text_content(content: bytes) -> str:
    """企微 `message_table.content`（content_type=2 文本消息）的 protobuf → 文本。

    实测格式（真机 hex 坐实）：`0A<n> 08 00 12<m> 0A<k> <UTF-8 文本>`
    —— 外层字段1(0A)是子消息，内含字段2(12)子消息，其字段1(0A)才是正文串。
    不匹配（其它消息类型/结构）返回 ''，绝不抛。
    """
    b = content
    if not b or b[0] != 0x0A:
        return ""
    i = 1
    n, i = _varint(b, i)
    sub = b[i:i + n]
    j = 0
    while j < len(sub):
        tag = sub[j]
        j += 1
        wt = tag & 7
        if wt == 0:                       # varint 字段（如 08 00）→ 跳过
            _, j = _varint(sub, j)
        elif wt == 2:                     # 长度分隔字段
            m, j = _varint(sub, j)
            seg = sub[j:j + m]
            j += m
            if (tag >> 3) == 2 and seg and seg[0] == 0x0A:   # 字段2 的子消息里字段1=正文
                k, jj = _varint(seg, 1)
                try:
                    return seg[jj:jj + k].decode("utf-8")
                except UnicodeDecodeError:
                    return ""
        else:
            break
    return ""


def _sql_lit(s: str) -> str:
    """把标识符/值转成安全的 SQL 单引号字面量（转义单引号防注入；只读查询更多是防意外）。"""
    return "'" + str(s).replace("'", "''") + "'"


def is_app_contact(uid) -> bool:
    """企微应用/系统账号（短数字 id，如 10120/10067/10212）——非真人联系人，会话列表里滤掉。
    真人 uid 是 15-19 位长数字（如 7881303268160321），应用 id 一般 ≤6 位。"""
    s = str(uid or "")
    return s.isdigit() and len(s) < 11


class WeComHistoryReader:
    """经 col_hook 进程内自查（/wecom_query）读企微本地 `message.db` 的【全量结构化历史】。

    这是路线 B step3 的 Python 侧：注入 `list_dbs_fn()->list[str]` 和 `query_fn(db,sql)->dict`
    （来自 BridgeClient），主动 SELECT message_table，得到所有会话、所有消息的 {发件人,时间,类型,正文}，
    无需 UI 交互 = 个人微信 QueryDB 式"开箱读全库"的企微对等实现。

    1:1 会话 conversation_id = `S:<self_id>_<对方uid>`；本类按此和 self_id 做联系人↔会话映射。
    """

    def __init__(self, list_dbs_fn, query_fn, self_id: str = ""):
        self._list_dbs = list_dbs_fn
        self._query = query_fn
        self._self_id = self_id
        self._db = None                    # 缓存 message.db 句柄（pool 句柄可能失效，用前校验）
        self._cdb = None                   # 缓存联系人库(user_table)句柄，供名字解析
        self._cav = None                   # 缓存含 conversation_avatar_table 的库（冷启动即被捕获，供 self_id 反推）

    def _msgdb(self):
        """定位并缓存含 message_table 的连接句柄；失效则重找。查询失败/未接返回 None。"""
        if self._db:
            try:
                r = self._query(self._db, "SELECT 1")
                if r.get("ok"):
                    return self._db
            except Exception:
                pass
            self._db = None
        try:
            for db in (self._list_dbs() or []):
                r = self._query(db, "SELECT name FROM sqlite_master WHERE name='message_table'")
                if r.get("ok") and r.get("nrows"):
                    self._db = db
                    return db
        except Exception:
            return None
        return None

    def available(self) -> bool:
        return self._msgdb() is not None

    def _conv_where(self, contact_id: str) -> str:
        """联系人 → 会话匹配 WHERE 子句。1:1 会话 id 是 S:<A>_<B>，**self 可能在前或后**，两序都匹配。
        已是 S:/R:/Y:/B: 前缀（直接传会话 id）则精确匹配。"""
        c = str(contact_id or "")
        if c[:2] in ("S:", "R:", "Y:", "B:"):
            return "conversation_id=%s" % _sql_lit(c)
        return "conversation_id IN (%s,%s)" % (
            _sql_lit(f"S:{self._self_id}_{c}"), _sql_lit(f"S:{c}_{self._self_id}"))

    def _contact_of(self, conv: str) -> str:
        """S:<A>_<B> → 非 self 的那个 uid（供会话列表显示联系人）。非 S: 原样。"""
        if conv.startswith("S:"):
            parts = conv[2:].split("_", 1)
            if len(parts) == 2:
                a, b = parts
                return b if a == self._self_id else a
        return conv

    def read_conversation(self, contact_id: str, limit: int = 300) -> list[dict]:
        """某联系人/会话的全量历史（时间正序），映射成会话视图用的 {text,is_self,provenance,ts,type}。"""
        db = self._msgdb()
        if not db:
            return []
        sql = ("SELECT sender_id,send_time,content_type,hex(content) FROM message_table "
               "WHERE %s ORDER BY message_id DESC LIMIT %d" % (self._conv_where(contact_id), int(limit)))
        try:
            r = self._query(db, sql)
        except Exception:
            return []
        out = []
        for row in (r.get("rows") or []):
            sender, ts, ctype, hexc = (list(row) + ["", "", "", ""])[:4]
            text = ""
            if str(ctype) == "2":
                try:
                    text = parse_text_content(bytes.fromhex(hexc))
                except ValueError:
                    text = ""
            if not text:
                continue                   # 非文本（图片/卡片/系统）暂不进气泡
            out.append({"text": text, "is_self": str(sender) == str(self._self_id),
                        "provenance": "history", "ts": _to_int(ts), "type": _to_int(ctype)})
        out.reverse()                      # 时间正序
        return out

    def list_sessions(self) -> list[dict]:
        """所有 1:1 会话（有文本消息的），映射成 {wxid,name,last,ts} 供会话列表（name=真实名/备注）。"""
        db = self._msgdb()
        if not db:
            return []
        sql = ("SELECT conversation_id,max(send_time),count(*) FROM message_table "
               "WHERE conversation_id LIKE 'S:%' GROUP BY conversation_id ORDER BY max(send_time) DESC LIMIT 200")
        try:
            r = self._query(db, sql)
        except Exception:
            return []
        rows = []
        for row in (r.get("rows") or []):
            conv = str((list(row) + [""])[0])
            contact = self._contact_of(conv)
            if is_app_contact(contact):        # 应用/系统会话(10120…)不进联系人列表
                continue
            rows.append({"wxid": contact, "name": contact, "last": "",
                         "ts": _to_int((list(row) + ["", ""])[1])})
        names = self.resolve_names([r["wxid"] for r in rows])   # uid → 真实名/备注
        for r in rows:
            r["name"] = names.get(r["wxid"], r["wxid"])
        return rows

    def _contactdb(self):
        """定位并缓存含 user_table 的联系人库连接（供名字解析）。失效则重找。"""
        if self._cdb:
            try:
                if self._query(self._cdb, "SELECT 1").get("ok"):
                    return self._cdb
            except Exception:
                pass
            self._cdb = None
        try:
            for db in (self._list_dbs() or []):
                r = self._query(db, "SELECT name FROM sqlite_master WHERE name='user_table'")
                if r.get("ok") and r.get("nrows"):
                    self._cdb = db
                    return db
        except Exception:
            return None
        return None

    def resolve_names(self, uids) -> dict:
        """企微 uid → 真实名字：优先 external_user_relation_v3 备注，回落 user_table.name，再回落 uid 本身。
        供聊天记录/会话列表/群发页把 uid 显示成人能认的名字。查不到安全回 uid。"""
        out = {str(u): str(u) for u in (uids or [])}
        db = self._contactdb()
        ids = [str(u) for u in (uids or []) if str(u).isdigit()]
        if not db or not ids:
            return out
        inlist = ",".join(ids)
        try:
            r = self._query(db, "SELECT id,name FROM user_table WHERE id IN (%s)" % inlist)
            for row in (r.get("rows") or []):
                rid, name = (list(row) + ["", ""])[:2]
                if name and str(name).strip():
                    out[str(rid)] = str(name)
        except Exception:
            pass
        try:  # 备注优先（客服更认备注）
            r = self._query(db, "SELECT user_id,remarks,real_remarks FROM external_user_relation_v3 "
                                "WHERE user_id IN (%s)" % inlist)
            for row in (r.get("rows") or []):
                uid, rem, real = (list(row) + ["", "", ""])[:3]
                pick = (str(real).strip() or str(rem).strip())
                if pick:
                    out[str(uid)] = pick
        except Exception:
            pass
        return out

    def set_self_id(self, sid) -> None:
        """动态识别出 self_id 后回填（供 read_conversation/list_sessions 拼 1:1 会话 id）。"""
        self._self_id = str(sid or "")

    def _cavatar_db(self):
        """定位并缓存含 conversation_avatar_table 的库（会话头像表）。企微一登录/切会话就读它，
        **冷启动即被 col_hook 捕获**（比 message.db 早，不用等读消息表）——self_id 反推的首选源。"""
        if self._cav:
            try:
                if self._query(self._cav, "SELECT 1").get("ok"):
                    return self._cav
            except Exception:
                pass
            self._cav = None
        try:
            for db in (self._list_dbs() or []):
                r = self._query(db, "SELECT name FROM sqlite_master WHERE name='conversation_avatar_table'")
                if r.get("ok") and r.get("nrows"):
                    self._cav = db
                    return db
        except Exception:
            return None
        return None

    def _self_conversations(self) -> list:
        """所有 1:1 会话 id（`S:...`）。**优先 conversation_avatar_table**（冷启动即被捕获），
        回落 message_table。拿不到库/无会话 → []。"""
        for db, table in ((self._cavatar_db(), "conversation_avatar_table"),
                          (self._msgdb(), "message_table")):
            if not db:
                continue
            try:
                r = self._query(db, "SELECT conversation_id FROM %s "
                                    "WHERE conversation_id LIKE 'S:%%' GROUP BY conversation_id" % table)
            except Exception:
                continue
            convs = [c for c in (str((list(row) + [""])[0]) for row in (r.get("rows") or []))
                     if c.startswith("S:")]
            if convs:
                return convs
        return []

    def derive_self_id(self) -> str:
        """从本地库 1:1 会话 id 反推本企微账号 uid —— **无需用户填写**。

        1:1 会话 id 形如 `S:<A>_<B>`，本账号出现在**所有** 1:1 会话里（客户各只出现在自己那条），
        故"出现在最多会话里的 uid" = 本账号。会话源优先 conversation_avatar_table（冷启动即捕获），
        回落 message_table。拿不到库/无 1:1 会话 → 返回 ""（调用方稍后重试）。并列时用联系人库
        排歧（客户在 external_user_relation_v3，self 不在）。真机验证：本机账号反推=1688856447584453 ✓。"""
        convs = self._self_conversations()
        if not convs:
            return ""
        from collections import Counter
        cnt: Counter = Counter()
        for conv in convs:
            parts = conv[2:].split("_", 1)
            if len(parts) != 2:
                continue
            a, b = parts
            if a:
                cnt[a] += 1
            if b:
                cnt[b] += 1
        if not cnt:
            return ""
        ranked = cnt.most_common()
        top_n = ranked[0][1]
        ties = [uid for uid, n in ranked if n == top_n]
        if len(ties) == 1:
            return ties[0]
        # 并列（如只有 1 条会话，两 uid 各 1 次）：客户在外部联系人库、self 不在 → 取不在库里的那个
        cdb = self._contactdb()
        digits = [u for u in ties if u.isdigit()]
        if cdb and digits:
            try:
                ext = self._query(cdb, "SELECT user_id FROM external_user_relation_v3 "
                                       "WHERE user_id IN (%s)" % ",".join(digits))
                externals = {str((list(row) + [""])[0]) for row in (ext.get("rows") or [])}
                non_ext = [u for u in ties if u not in externals]
                if len(non_ext) == 1:
                    return non_ext[0]
            except Exception:
                pass
        return ties[0]


class MsgDbReader:
    """把 col_probe 的 `/collog`（企微解密后被动收割的列值）读成结构化聊天正文，供反哺。

    设计成**注入一个 `fetch(since:int)->bytes` 回调**（返回 `/collog` 原始字节），
    这样离线可用 fixture 单测、线上传入 `BridgeClient.fetch_collog`。维护单调游标做增量拉取；
    游标可后退（DLL 重注入 g_seq 归零）时自愈夹回，避免永久失聪（对齐 adapter 的收方自愈）。
    """

    def __init__(self, fetch, since: int = 0):
        self._fetch = fetch
        self._cursor = int(since)

    @property
    def cursor(self) -> int:
        return self._cursor

    def poll(self) -> HarvestResult:
        """拉一批增量收割并推进游标。fetch 抛异常 → 返回空结果（本轮跳过，不抛）。"""
        try:
            body = self._fetch(self._cursor)
        except Exception:
            return HarvestResult()
        if not body:
            return HarvestResult()
        res = harvest(body)
        srv = parse_cursor(body)
        if srv and srv < self._cursor:      # DLL 重注入 → g_seq 归零，夹回自愈
            self._cursor = srv
        elif srv:
            self._cursor = max(self._cursor, srv)
        return res

    def harvest_texts(self) -> list[str]:
        """便捷：拉一批并只回去重后的聊天正文（反哺 KB/话术库直接用）。"""
        return self.poll().texts
