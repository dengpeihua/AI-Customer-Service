# widget/wechat/manager.py
"""WeChatManager —— 个人微信多实例编排：每个 Weixin pid → 端口 → 账号 → 注册 hub。

M4。形状照抄 M2 的 WeComManager（widget/wecom/manager.py），差别只在端口来源：
企微读 native 写的 `status-<pid>.txt`，微信直接问操作系统「这个 pid 在听哪个端口」。

安全铁律（与企微一致）：**没在 instances.yaml 里配置的号一律 park** —— 不 patch、
不建 adapter、不注册 hub。绝不把陌生账号的消息误绑到任意已配置的租户上。

顺序很重要：先认人（/GetSelfProfile 在 g_IsLogin patch 之前即可用），确认这个号是我们
自己的，**再**往它进程里写内存。

整分支审查（Critical）后的三条加固：
1. 匹配用**全集**：`instances` 传进来的必须是全部已配置微信号。只给一个候选（重连最容易犯）
   等于把任何上报的 wxid 硬套上去 —— 乙号的微信会被认领成甲号。要「只接管某几个号」用
   `targets=`，那只限制**接管**，不限制匹配。
2. 身份佐证 **fail closed**：没有正面 OS 佐证就 park（见 `_identity_reject`）。
3. `reserved_ports=` 里的端口（别的实例正在用的那台微信）一律不碰。
"""
from __future__ import annotations

from typing import Callable, Optional

from widget.instances import InstanceConfig

# 微信 4.x 的数据目录：`<任意根>\xwechat_files\<wxid>_<随机后缀>\...`。
_DATA_DIR_MARK = "xwechat_files"


def _normalize_paths(paths) -> str:
    """把一堆文件路径拼成可比对的一条串：全小写 + 正斜杠统一成反斜杠。"""
    return " ".join(str(p) for p in (paths or [])).lower().replace("/", "\\")


def open_file_paths(pid: int) -> list:
    """该 pid 自己打开的文件路径 —— **独立于 hook** 的账号信号（唯一实现，别抄第二份）。

    微信 4.x 的数据目录是 `...\\xwechat_files\\<wxid>_<随机>\\`，进程打开的 db 文件路径里就带着
    它**此刻登录**的那个 wxid。取不到（权限/时机/进程没了）返回空表 = 无信号，不是矛盾。

    调用方两处，取的是同一个信号：
      · 认领链的 OS 佐证（`WeChatManager._identity_reject`）；
      · adapter 每次收发前的身份闸（`WeChatHookAdapter.owns_its_account`，本轮 H5）。
    """
    try:
        import psutil
        return [f.path for f in psutil.Process(int(pid)).open_files()]
    except Exception:                                # noqa: BLE001 —— 佐证探测绝不能抛
        return []


def process_owns_account(pid: int, wxid: str, *, paths_fn=None) -> bool:
    """这个 pid **此刻**还开着 `wxid` 的数据目录吗。**fail closed**：无信号 = False。

    ★H5★ 认领链只在「认领那一刻」问过这个问题，而账号是会在**同一个进程里**换掉的
    （退出登录 → 扫另一个号的码：pid 不变、StartPort 不变、hook 继续在同一个端口上服务，
    `pid_still_owns_port` 恒为真）。所以 adapter 每次收发前也要问一次 —— 复用
    `owns_data_dir` 这条已经被认领链信任的判据（目录段精确匹配，`zhangsan888` 骗不过
    `zhangsan`），绝不另起一套。
    """
    pid, wxid = int(pid or 0), str(wxid or "")
    if not pid or not wxid:
        return False
    try:
        paths = _normalize_paths((paths_fn or open_file_paths)(pid))
    except Exception:                                # noqa: BLE001 —— 探不出来 = 无法自证
        return False
    return owns_data_dir(paths, wxid)


def owns_data_dir(paths_blob: str, wxid: str) -> bool:
    """`paths_blob`（须先过 `_normalize_paths`）里有没有**属于这个 wxid 的数据目录段**。

    ★为什么不能用裸子串 `wxid in paths`（本轮 Critical，H1）★：老号可以自选微信号，前缀相关
    极常见。本店同时配了 `zhangsan` 和 `zhangsan888` 时，乙号进程的数据目录
    `...\\xwechat_files\\zhangsan888_1a2b\\...` **包含**子串 "zhangsan" —— 于是一次乱报的
    /GetSelfProfile（widget/config.py:35 自己承认它会乱报）就骗过了唯一那道 OS 佐证闸：
    乙的进程被认领成甲、挨一发跨进程写内存，此后乙的客户消息进甲的租户、甲的回复从乙的
    微信发出。所以只认 `\\xwechat_files\\<wxid>_` 这个**目录段**（前面那个分隔符也要，
    免得 `notxwechat_files` 之类蒙混），大小写不敏感、/ 与 \\ 都认。
    """
    if not wxid:
        return False
    return f"\\{_DATA_DIR_MARK}\\{wxid.lower()}_" in paths_blob


class WeChatManager:
    def __init__(self, instances, cfg, bridges, state, hub, *,
                 targets: Optional[set] = None,
                 reserved_ports: Optional[dict] = None,
                 pids_fn: Optional[Callable[[], list]] = None,
                 port_fn: Optional[Callable[[int], Optional[int]]] = None,
                 profile_fn: Optional[Callable[[int], Optional[dict]]] = None,
                 patch_fn: Optional[Callable[..., str]] = None,
                 paths_fn: Optional[Callable[[int], list]] = None,
                 adapter_factory: Optional[Callable] = None,
                 logger: Optional[Callable[[str], None]] = None):
        # `instances` 是**匹配全集**：每个发现出来的 wxid 都要拿全部已配置账号去比对。
        # 只给一个候选（重连时最容易犯）等于把任何上报的 wxid 硬套到那个候选上 —— 乙号的
        # 微信进程会被认领成甲号，然后被 patch、被注册进甲号的租户。
        self._insts = [i for i in instances if i.platform == "wechat" and i.enabled]
        # `targets`：这一轮**允许接管**的 channel_key（None = 全部）。重连只想拿回自己那一个号，
        # 但匹配仍须用全集：认出别人的进程只记 park，绝不注册（会顶掉别人正在跑的 adapter）。
        self._targets = set(targets) if targets is not None else None
        # `reserved_ports`：{port: channel_key} —— 已被别的实例认领的端口，本轮一律不碰。
        self._reserved_ports = {int(p): ck for p, ck in (reserved_ports or {}).items()}
        self._cfg = cfg
        self._bridges = bridges
        self._state = state
        self._hub = hub
        self._pids_fn = pids_fn or self._default_pids
        self._port_fn = port_fn or self._default_port
        self._profile_fn = profile_fn or self._default_profile
        self._patch_fn = patch_fn or self._default_patch
        self._paths_fn = paths_fn or self._default_paths
        self._adapter_factory = adapter_factory
        self._log = logger or (lambda m: None)
        self.adapters: dict[str, object] = {}
        self.pids: dict[str, int] = {}
        self.ports: dict[str, int] = {}
        self.parked: dict[int, str] = {}
        self.unclaimed: list[InstanceConfig] = []

    # ---- 默认实现（真机路径）----

    @staticmethod
    def _default_pids() -> list:
        from widget.wechat.ports import wechat_pids
        return wechat_pids()

    @staticmethod
    def _default_port(pid: int) -> Optional[int]:
        from widget.wechat.ports import listening_port_for_pid
        return listening_port_for_pid(pid)

    @staticmethod
    def _default_profile(port: int) -> Optional[dict]:
        from widget.hook_patch import _get_self_profile
        return _get_self_profile(port)

    @staticmethod
    def _default_patch(pid: int, port: int) -> str:
        from widget.hook_patch import ensure_login_patched
        return ensure_login_patched(pid=pid, port=port)

    @staticmethod
    def _default_paths(pid: int) -> list:
        """该 pid 自己打开的文件路径 —— **独立于 hook** 的账号佐证信号（模块级唯一实现）。"""
        return open_file_paths(pid)

    # ---- 认领 ----

    def _match(self, wxid: str) -> Optional[InstanceConfig]:
        for i in self._insts:
            if i.self_wxid and i.self_wxid == wxid:
                return i
        return None

    def _match_by_datadir(self, pid: int) -> Optional[InstanceConfig]:
        """权威身份反查：这个进程打开了**哪个配置账号**的数据目录段（xwechat_files\\<wxid>_）。

        为什么需要它（2026-08-03 真机 SP-D 实测）：`/GetSelfProfile` 会**摆动**——同一个已登录
        进程，隔几秒 wxid 报出不同值（甚至把聊天内容塞进 nickname 字段）。若匹配只信它，配置
        正确的号会被间歇性误 park（可用性缺陷）。open_files 的数据目录段是权威、稳定、独立于
        hook 的信号（`owns_data_dir` 已被认领链信任为佐证），拿它反查即可救回。

        只在 `/GetSelfProfile` 没给出可匹配身份时作为**回退**调用；找不到就返回 None（下面照常
        park，fail closed 不变）。不覆盖 `/GetSelfProfile` 已成功匹配的路径——那条路径行为零变化。
        """
        try:
            paths = _normalize_paths(self._paths_fn(pid))
        except Exception:                                # noqa: BLE001 —— 探测失败 = 无佐证
            return None
        if not paths:
            return None
        for i in self._insts:
            if i.self_wxid and owns_data_dir(paths, i.self_wxid):
                return i
        return None

    def _identity_reject(self, pid: int, port: int, wxid: str) -> str:
        """OS 侧交叉验证 profile 报出来的身份。**fail closed**：没拿到正面佐证就返回 park 理由。

        为什么非做不可：`/GetSelfProfile` 是本项目里出了名不可靠的接口
        （widget/config.py:35「hook 的 GetSelfProfile 会乱报，须手动指定」）。它一旦报出一个
        **合法但错**的 wxid，认领链上每一道闸都会放行 —— 匹配到别人的 InstanceConfig、
        往这个进程写 g_IsLogin、把它注册到别人的 channel_key/租户：甲号客户的消息记进乙号租户、
        乙号的回复从甲号发出。所以身份不能只有 hook 一个信源。

        为什么从「只在有正面矛盾时 park」改成「没有正面佐证就 park」（整分支审查 Critical）：
        写内存是不可逆的破坏性动作（写错进程 = 写崩客户的微信 + 弹崩溃修复框）。「无信号」和
        「已核对」绝不能同权 —— 只凭一个自己都不信的接口就往别人进程里写，正是 M4 要杜绝的。
        代价可接受：park 不是终态，`InstanceSupervisor` 会按退避重跑认领；微信一旦打开自己的
        数据库文件（冷启动几秒内），open_files 就给得出 `xwechat_files\\<wxid>_…` 这条佐证。
        legacy 单实例（无 instances.yaml）根本不走 manager，现网老客户不受影响。
        """
        try:
            paths = _normalize_paths(self._paths_fn(pid))
        except Exception as e:                           # noqa: BLE001 —— 探测失败 = 无佐证
            return (f"身份无法佐证：读不到 pid {pid} 的文件路径（{e}），端口 {port} 只有"
                    f" GetSelfProfile 一个信源（本项目已知会乱报）→ 不接管，等下轮重试")
        if not paths:
            return (f"身份无法佐证：pid {pid} 没给出任何文件路径，端口 {port} 只有"
                    f" GetSelfProfile 一个信源（本项目已知会乱报）→ 不接管，等下轮重试")
        if owns_data_dir(paths, wxid):
            return ""                                    # 正面佐证：数据目录就是这个号
        other = next((i.self_wxid for i in self._insts
                      if i.self_wxid and i.self_wxid.lower() != wxid.lower()
                      and owns_data_dir(paths, i.self_wxid)), "")
        if other:
            return (f"身份存疑：端口 {port} 上报 {wxid}，但 pid {pid} 的数据目录属于 {other}"
                    f" → 不接管，绝不把消息绑错租户")
        return (f"身份未获佐证：端口 {port} 上报 {wxid}，但 pid {pid} 的文件路径里找不到它的"
                f"数据目录 → 不接管，等下轮重试")

    def start(self) -> dict:
        from widget.instance_runtime import build_instance
        result: dict = {}
        claimed: set[str] = set()
        for pid in self._pids_fn():
            try:
                port = self._port_fn(pid)
                if not port:
                    # 两个微信抢同一端口时第二个 hook 起不来 → 没端口。park，绝不误认成别的号。
                    self.parked[pid] = "未接管：无 hook 端口（未注入或端口被占）"
                    self._log(f"[wechat-manager] pid {pid} 无 hook 端口 → park")
                    continue
                owner = self._reserved_ports.get(int(port))
                if owner and (self._targets is None or owner not in self._targets):
                    # 这个端口已经是别的实例正在用的那台微信：绝不重复认领（重复认领 = 两条
                    # 渠道指向同一个微信：重复回复、消息记错租户，而且会往它再写一次内存）。
                    reason = f"端口 {port} 已由 {owner} 认领 → 不重复接管"
                    self.parked[pid] = reason
                    self._log(f"[wechat-manager] pid {pid} {reason}")
                    continue
                profile = self._profile_fn(port)
                wxid = str((profile or {}).get("wxid", "")).strip()
                inst = self._match(wxid) if wxid else None
                if inst is None:
                    # /GetSelfProfile 空/乱报到未配置 wxid（真机 SP-D 实测会发生）→ 回退到权威
                    # 数据目录反查。反查到就用它的真实身份继续；反查不到则照常 park（fail closed）。
                    inst = self._match_by_datadir(pid)
                    if inst is not None:
                        wxid = inst.self_wxid
                if inst is None:
                    if not wxid:
                        self.parked[pid] = f"未就绪：{port} 上取不到账号身份（未登录？）"
                        self._log(f"[wechat-manager] pid {pid} 端口 {port} 未就绪 → park")
                    else:
                        self.parked[pid] = f"未认领的号 {wxid}"
                        self._log(f"[wechat-manager] pid {pid} wxid={wxid} 未配置账号 → park")
                    continue
                if self._targets is not None and inst.channel_key not in self._targets:
                    # 匹配用全集（否则任何 wxid 都会被硬套到唯一候选上），但只接管本轮目标：
                    # 认出别人的号只记 park —— 注册它会顶掉那条渠道正在跑的 adapter。
                    reason = f"{wxid} 属于 {inst.channel_key}，非本轮认领目标 → 不接管"
                    self.parked[pid] = reason
                    self._log(f"[wechat-manager] pid {pid} {reason}")
                    continue
                if inst.channel_key in claimed:
                    # 两个进程都自称同一个号（profile 乱报/端口错位）：后来者一律 park，
                    # 绝不覆盖已认领的 pid/端口绑定 —— 覆盖等于把消息改道到另一个微信。
                    reason = (f"身份重复：{wxid} 已由 pid {self.pids.get(inst.channel_key)} 认领"
                              f" → 不接管")
                    self.parked[pid] = reason
                    self._log(f"[wechat-manager] pid {pid} {reason}")
                    continue
                reject = self._identity_reject(pid, port, wxid)
                if reject:
                    self.parked[pid] = reject
                    self._log(f"[wechat-manager] pid {pid} {reject}")
                    continue
                # 先认人、再写内存：确认是自己的号之后才 patch g_IsLogin。
                self._log(f"[wechat-manager] pid {pid} 端口 {port} → {self._patch_fn(pid, port)}")
                # 实际发现的端口是权威：配置里可能写着陈旧的 hook_base_url/hook_port。
                inst.hook_base_url = ""
                inst.hook_port = port
                bridge = self._bridges.get(inst.channel_key)
                # `verified_pid`：adapter 自愈重补时唯一被允许写的那个进程 —— 就是刚刚这条
                # 认领链认出来的 pid。没有它，adapter 只会按端口盲写（M4 红线）。
                adapter, pipe = build_instance(inst, self._cfg, bridge, self._state,
                                               adapter_factory=self._adapter_factory,
                                               verified_pid=pid)
                self._hub.register(inst.channel_key, adapter, pipe)
                self.adapters[inst.channel_key] = adapter
                self.pids[inst.channel_key] = pid
                self.ports[inst.channel_key] = port
                claimed.add(inst.channel_key)
                result[inst.channel_key] = ""
                self._log(f"[wechat-manager] pid {pid} → {inst.channel_key}"
                          f"（tenant {inst.tenant_id}，端口 {port}）")
            except Exception as e:                       # noqa: BLE001 —— 单 pid 失败不拖垮其它
                self._log(f"[wechat-manager] pid {pid} 接入失败（忽略）：{e}")
                result[f"pid:{pid}"] = str(e)
        self.unclaimed = [i for i in self._insts if i.channel_key not in claimed
                          and (self._targets is None or i.channel_key in self._targets)]
        return result
