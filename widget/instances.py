# widget/instances.py
from __future__ import annotations
import dataclasses
from dataclasses import dataclass
from pathlib import Path
import yaml

class InstanceConfigError(Exception):
    pass

@dataclass
class InstanceConfig:
    account_id: str
    platform: str            # "wechat" | "wecom"
    display_name: str
    tenant_id: int
    login: str
    password_enc: str
    enabled: bool = True
    channel_key: str = ""
    self_wxid: str = ""
    self_id: str = ""
    hook_base_url: str = ""      # 显式覆写（legacy 合成路径用）；留空则由 hook_port 派生
    hook_port: int = 0           # M4：per-instance hook 端口（Weixin.exe StartPort=<port>）
    data_dir: str = ""
    password: str = ""           # M5：明文登录密码（现网明文客户；per-instance 独立登录用）
                                 # ★放在有默认值字段区★——加在必填字段之后、enabled 之前会改变
                                 # 位置参数顺序、打乱既有 InstanceConfig(...) 调用点。
    # ★只由 `legacy_instances()` 置 True★ —— 「这条不是用户配的，是没有 instances.yaml 时我们
    # **合成**出来的」。`app.is_legacy_wechat` 只认这个标记：legacy 与否是**配置怎么加载出来的**
    # 属性，绝不是某一行上的 channel_key。（曾经按 `channel_key == "wechat_personal"` 判，而
    # docs/DEVLOG.md:24 恰恰处方老客户迁移时保留这个 channel_key，于是一份合法的手写单行
    # instances.yaml 就会走 legacy 盲补路径 → 往「此刻谁在听配置端口」的陌生微信写 g_IsLogin。）
    # `load_instances` 显式拒收 YAML 里的同名 key，见 `_YAML_FORBIDDEN_KEYS`。
    synthesized: bool = False

    def finalize(self) -> "InstanceConfig":
        if not self.channel_key:
            self.channel_key = f"{self.platform}#{self.account_id}"
        if not self.data_dir:
            self.data_dir = f"data/{self.account_id}"
        return self

    def resolve_hook_base_url(self, default: str = "") -> str:
        """本实例的 hook 服务地址。显式 hook_base_url > hook_port 派生 > 调用方给的默认值。

        **所有消费者只走这个方法**——端口的单一真相落在访问点上，避免出现第二个
        「端口写死在哪儿」的来源（M4 之前 hook_base_url / bridge_port_hint 两个字段都没人读）。
        """
        if self.hook_base_url:
            return self.hook_base_url
        if self.hook_port:
            return f"http://127.0.0.1:{self.hook_port}"
        return default

def _discriminator(ic: InstanceConfig) -> str:
    return ic.self_id if ic.platform == "wecom" else ic.self_wxid

def effective_hook_port(ic: InstanceConfig) -> int:
    """本实例**生效后**的 hook 端口（与 resolve_hook_base_url 同一优先级）；判定不出返回 0。"""
    if ic.hook_base_url:
        from urllib.parse import urlsplit
        try:
            return int(urlsplit(ic.hook_base_url).port or 0)
        except (ValueError, TypeError):
            return 0
    return int(ic.hook_port or 0)

def _validate(insts: list[InstanceConfig]) -> None:
    ids = [i.account_id for i in insts]
    if len(ids) != len(set(ids)):
        raise InstanceConfigError(f"account_id 重复：{ids}")
    by_platform: dict[str, list[InstanceConfig]] = {}
    for i in insts:
        by_platform.setdefault(i.platform, []).append(i)
    for platform, group in by_platform.items():
        if len(group) > 1:
            discs = [_discriminator(i) for i in group]
            if any(not d for d in discs):
                raise InstanceConfigError(
                    f"{platform} 多实例：每行须填判别符（wecom=self_id/wechat=self_wxid）")
            if len(discs) != len(set(discs)):
                raise InstanceConfigError(f"{platform} 多实例判别符重复：{discs}")
            # M5/D4：多实例=多租户 N-Bridge，每账号用自己的 tenant/login 登录独立 Bridge
            # （隔离靠 token）。凭据缺失过去会一路穿到 Bridge.login() 抛裸 error / 静默失败；
            # 这里前置成清晰的 InstanceConfigError（对所有 platform）。
            for i in group:
                if not i.tenant_id or i.tenant_id <= 0:
                    raise InstanceConfigError(
                        f"{platform} 多实例 {i.account_id}：tenant_id 必填且 > 0（多租户隔离靠它登录）")
                if not i.login:
                    raise InstanceConfigError(f"{platform} 多实例 {i.account_id}：login 必填")
                if not (i.password or i.password_enc):
                    raise InstanceConfigError(
                        f"{platform} 多实例 {i.account_id}：须填 password 或 password_enc")
            if platform == "wechat":
                # M4：端口→账号的绑定由**进程驱动认领**决定（枚举 Weixin.exe → 问操作系统
                # 这个 pid 在听哪个端口 → GET /GetSelfProfile 问身份 → 匹配 self_wxid，
                # 见 spec §3 A3）。所以 hook_port **不是必填**：不写就交给发现流程，这正是
                # M4 的设计。（曾经这里要求多实例逐行必填，把 M3 能加载的配置一刀拒了，
                # 而 run() 又没接异常 → pythonw 下挂件静默不启动。）
                # 仍然拦下的只有**真冲突**：两行写了同一个非 0 端口 —— 那是明确的人为错误，
                # 会让两个 adapter 轮询/发送同一台微信（客户收双份回复、甲号消息从乙号发出）。
                ports = [p for p in (effective_hook_port(i) for i in group) if p]
                if len(ports) != len(set(ports)):
                    raise InstanceConfigError(f"wechat 多实例 hook_port 重复：{ports}")

# 用户 YAML 里**绝不接受**的字段：它们是「配置怎么来的」这类元信息，只能由本模块自己置。
# load_instances 是按 dataclass 字段名白名单收 key 的，不在这里显式挡掉，用户（或一份被抄来的
# 示例配置）写一行 `synthesized: true` 就能把自己伪装成合成实例、重新打开 legacy 盲补那个洞。
_YAML_FORBIDDEN_KEYS = frozenset({"synthesized"})


def load_instances(instances_path: str = "instances.yaml",
                   widget_cfg_path: str = "widget_config.yaml",
                   wecom_cfg_path: str = "wecom_hook_config.yaml") -> list[InstanceConfig]:
    p = Path(instances_path)
    if p.exists():
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        rows = raw.get("instances", []) if isinstance(raw, dict) else []
        names = {f.name for f in dataclasses.fields(InstanceConfig)} - _YAML_FORBIDDEN_KEYS
        insts = [InstanceConfig(**{k: v for k, v in row.items() if k in names}).finalize()
                 for row in rows]
        _validate(insts)
        return insts
    return legacy_instances(widget_cfg_path, wecom_cfg_path)


def legacy_instances(widget_cfg_path: str = "widget_config.yaml",
                     wecom_cfg_path: str = "wecom_hook_config.yaml",
                     wc=None) -> list[InstanceConfig]:
    """无 instances.yaml 时合成的「旧版单实例」列表（channel_key 用旧常量，零迁移）。

    单独暴露出来是给 `app.load_instances_or_degrade` 用的：instances.yaml 坏了的时候，
    挂件必须还能按老样子起来，而不是异常穿出 `run()`、在 pythonw 下静默不启动。
    `wc` 传入已加载好的 widget_config 可跳过重新读盘（连 widget_config 都坏了的场景）。
    """
    if wc is None:
        from widget.config import load_config
        wc = load_config(widget_cfg_path)
    insts = [InstanceConfig(
        account_id="default", platform="wechat", display_name="个人微信",
        tenant_id=wc.tenant_id, login=wc.login, password_enc=wc.password_enc,
        password=wc.password,
        channel_key="wechat_personal", self_wxid=wc.self_wxid,
        hook_base_url=wc.hook_base_url, data_dir=".", synthesized=True).finalize()]
    # 注意 Path("") == Path(".")（存在！）：空串必须当成「不要企微」，否则降级路径会去
    # WeComHookConfig.load("") 再炸一次。
    if wecom_cfg_path and Path(wecom_cfg_path).exists():
        from widget.wecom.config import WeComHookConfig
        wx = WeComHookConfig.load(wecom_cfg_path)
        insts.append(InstanceConfig(
            account_id="default_wecom", platform="wecom", display_name="企微",
            tenant_id=wc.tenant_id, login=wc.login, password_enc=wc.password_enc,
            password=wc.password,
            channel_key="wecom_hook", self_id=wx.self_id, data_dir=".",
            synthesized=True).finalize())
    _validate(insts)
    return insts

def account_from_self_id(platform: str, self_id: str) -> tuple[str, str]:
    tail = self_id[-6:] if len(self_id) > 6 else self_id
    account_id = f"{platform}-{tail}"
    return account_id, f"{platform}#{account_id}"

def append_instance(row: dict, path: str = "instances.yaml") -> bool:
    import os
    p = Path(path)
    data = {"instances": []}
    if p.exists():
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        if isinstance(raw, dict) and isinstance(raw.get("instances"), list):
            data = raw
    disc_key = "self_id" if row.get("platform") == "wecom" else "self_wxid"
    disc = row.get(disc_key)
    if disc:
        for existing in data["instances"]:
            if existing.get(disc_key) == disc:
                return False                            # 幂等：判别符已存在
    data["instances"].append(row)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    os.replace(tmp, p)                                  # 原子替换
    return True
