import atexit
from threading import Lock

from app.config import settings
from app.llm.base import EMBED_DIM, LLM, Vector
from app.llm.fake import FakeLLM

__all__ = [
    "LLM",
    "Vector",
    "EMBED_DIM",
    "close_llm",
    "embedding_model_name",
    "get_llm",
    "runtime_summary",
]

_cache_lock = Lock()
_cached_real_llm: LLM | None = None
_cached_real_config: tuple[str, ...] | None = None


def embedding_model_name() -> str:
    return settings.llm_embed_model


def runtime_summary() -> str:
    chat_model = (
        settings.deepseek_model
        if settings.llm_provider == "deepseek"
        else settings.llm_chat_model
    )
    return (
        f"provider={settings.llm_provider} "
        f"chat_model={chat_model} "
        f"embed_model={embedding_model_name()} "
        f"embed_dim={EMBED_DIM}"
        + (
            f" thinking={'on' if settings.deepseek_thinking else 'off'}"
            if settings.llm_provider == "deepseek"
            else ""
        )
    )


def get_llm() -> LLM:
    if settings.llm_provider in {"dashscope", "deepseek"}:
        if settings.llm_provider == "dashscope":
            from app.llm.dashscope import DashScopeLLM
            factory = DashScopeLLM
        else:
            from app.llm.deepseek import DeepSeekLLM
            factory = DeepSeekLLM
        config = (
            settings.llm_provider,
            settings.dashscope_api_key,
            settings.dashscope_base_url,
            settings.llm_chat_model,
            settings.llm_embed_model,
            settings.deepseek_api_key,
            settings.deepseek_api_base,
            settings.deepseek_model,
            str(settings.deepseek_thinking),
        )
        global _cached_real_llm, _cached_real_config
        with _cache_lock:
            if _cached_real_llm is not None and _cached_real_config == config:
                return _cached_real_llm
            replacement = factory()
            previous = _cached_real_llm
            _cached_real_llm = replacement
            _cached_real_config = config
            if previous is not None:
                close = getattr(previous, "close", None)
                if close is not None:
                    close()
            return replacement
    if settings.llm_provider == "fake":
        close_llm()
        return FakeLLM()
    close_llm()
    raise ValueError(f"Unsupported LLM provider: {settings.llm_provider}")


def close_llm() -> None:
    global _cached_real_llm, _cached_real_config
    with _cache_lock:
        llm = _cached_real_llm
        _cached_real_llm = None
        _cached_real_config = None
        if llm is not None:
            close = getattr(llm, "close", None)
            if close is not None:
                close()


atexit.register(close_llm)
