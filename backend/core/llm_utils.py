"""LLM response helpers shared by Anthropic-compatible providers."""
from typing import Any, Iterable, List


# 推理型 Anthropic 兼容模型会先消耗 thinking token；256 容易只返回 thinking
# 而没有最终 JSON。上限提高不会强制消耗满额，只为结构化输出留出完成空间。
STRUCTURED_OUTPUT_MAX_TOKENS = 1024


def extract_text_content(content: Iterable[Any]) -> str:
    """Return text blocks from Anthropic-style response content."""
    texts: List[str] = []
    for block in content or []:
        if isinstance(block, str):
            texts.append(block)
            continue

        block_type = getattr(block, "type", None)
        text = getattr(block, "text", None)
        if isinstance(block, dict):
            block_type = block.get("type", block_type)
            text = block.get("text", text)

        if isinstance(text, str) and (block_type in (None, "text")):
            texts.append(text)

    return "\n".join(t for t in texts if t)
