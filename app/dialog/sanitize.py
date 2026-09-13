"""回复净化：模型生成完、发出去之前，把"一眼看出是AI"的东西删掉。

为什么需要它：提示词按不住。实测再三点名"严禁 emoji/换行/您"，模型仍会偶尔漏——
概率虽低，但客服人设是"永远以真人身份出现"，漏一次就穿帮。规则类硬约束交给代码，
提示词只负责"人味"。

只删表层痕迹，不改语义、不碰事实（grounding 不受影响）。话术库那条路不用净化——
那本来就是人写的。
"""
from __future__ import annotations

import re

# 表情符号 + 杂项符号 + 交通/旗帜 + 补充符号 + 变体选择符与零宽连接符
_EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001FAFF"      # 表情、符号、象形
    "\U00002600-\U000027BF"      # 杂项符号、装饰符
    "\U0001F1E6-\U0001F1FF"      # 区域旗帜
    "\U00002B00-\U00002BFF"
    "\U0000FE00-\U0000FE0F"      # 变体选择符
    "\U0000200D"                 # 零宽连接符
    "\U00002190-\U000021FF"      # 箭头
    "]+",
    flags=re.UNICODE,
)
_KAOMOJI_RE = re.compile(r"[（(][^（）()]{0,6}[·^><\-_~][^（）()]{0,6}[）)]")
_MARKDOWN_RE = re.compile(r"(\*{1,3}|`{1,3}|^#{1,6}\s*|^[-•·]\s+)", flags=re.MULTILINE)
_TILDE_RE = re.compile(r"[~～]+")
_SPACES_RE = re.compile(r"[ \t　]{2,}")


def humanize(text: str, *, polite_you: bool = False) -> str:
    """把模型输出改造成"像真人在抖音私信打字"的样子。

    polite_you=True（formal 档）保留「您」；其余档位统一成「你」，
    因为 casual/warm 的人设不会对客户说敬语，混用反而露馅。
    """
    if not text:
        return text
    out = _EMOJI_RE.sub("", text)
    out = _KAOMOJI_RE.sub("", out)
    out = _MARKDOWN_RE.sub("", out)
    out = _TILDE_RE.sub("", out)
    out = out.replace("\r", "\n")
    out = re.sub(r"\n+", " ", out)          # 换行 -> 空格：真人一条消息不换行
    if not polite_you:
        out = out.replace("您好", "你好").replace("您", "你")
    out = _SPACES_RE.sub(" ", out).strip()
    out = re.sub(r"[。\s]+$", "", out)        # 去句尾句号（抖音私信里真人很少打）
    return out.strip()
