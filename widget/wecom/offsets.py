from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class OffsetManifest:
    """版本→偏移清单。骨架期 signatures/symbols 为空。"""
    version: str = ""
    arch: str = ""
    module: str = ""
    module_sha256: str = ""              # 目标模块整文件 SHA-256（版本权威：防同版本号不同二进制）
    verified: bool = False
    signatures: dict = field(default_factory=dict)
    symbols: dict = field(default_factory=dict)

    @staticmethod
    def load(path: str | Path) -> "OffsetManifest":
        p = Path(path)
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        return OffsetManifest(
            version=str(raw.get("version", "")),
            arch=str(raw.get("arch", "")),
            module=str(raw.get("module", "")),
            module_sha256=str(raw.get("module_sha256", "")),
            verified=bool(raw.get("verified", False)),
            signatures=dict(raw.get("signatures") or {}),
            symbols=dict(raw.get("symbols") or {}),
        )

    def has_message_offsets(self) -> bool:
        """是否已具备接真实收发消息所需的偏移（骨架期恒 False）。"""
        return self.verified and bool(self.signatures or self.symbols)


def verify_version(required: str, actual: str, strict: bool) -> tuple[bool, str]:
    """版本闸。actual 为空=未知：strict 下拒绝、否则放行并给警告理由。"""
    if not actual:
        if strict:
            return False, f"无法读取企微版本，strict 模式拒绝启动（要求 {required}）"
        return True, f"警告：未能读取企微版本（要求 {required}），strict=False 放行"
    if actual == required:
        return True, f"版本匹配 {actual}"
    return False, f"企微版本 {actual} ≠ 要求 {required}；偏移强绑定，拒绝启动"


def sha256_of_file(path: str | Path) -> str:
    """整文件 SHA-256（十六进制小写）。用于校验目标模块二进制身份。"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_module_sha256(expected: str, actual: str) -> tuple[bool, str]:
    """模块二进制身份闸（比版本字符串更强：防同版本号、不同二进制/被替换）。
    expected 为空=清单未登记 → 放行并提示（仍以版本字符串为主闸）；大小写不敏感。"""
    if not expected:
        return True, "清单未登记 module_sha256，跳过二进制身份校验（依赖版本字符串闸）"
    if not actual:
        return False, "无法计算目标模块 SHA-256，拒绝（清单已登记 module_sha256）"
    if expected.strip().lower() == actual.strip().lower():
        return True, "模块 SHA-256 匹配（二进制身份权威通过）"
    return False, (f"模块 SHA-256 不匹配：期望 {expected.strip()[:16]}… 实得 {actual.strip()[:16]}…"
                   "（同版本号但二进制不同，拒绝）")
