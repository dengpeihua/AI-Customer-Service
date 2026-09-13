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
    "chat_model_name",
    "embedding_model_name",
    "get_llm",
    "runtime_summary",
]

_cache_lock = Lock()
_cached_real_llm: LLM | None = None
_cached_real_config: tuple[str, ...] | None = None


def embedding_model_name() -> str:
    if settings.llm_provider == "glm":
        return settings.glm_embed_model
    return settings.minimax_embed_model


def chat_model_name() -> str:
    if settings.llm_provider == "glm":
        return settings.glm_model
    return settings.minimax_model if settings.llm_provider == "minimax" else settings.deepseek_model


def runtime_summary() -> str:
    return (
        f"provider={settings.llm_provider} "
        f"chat_model={chat_model_name()} "
        f"embed_model={embedding_model_name()} "
        f"embed_dim={EMBED_DIM}"
        + (
            f" thinking={'on' if settings.deepseek_thinking else 'off'}"
            if settings.llm_provider == "deepseek"
            else ""
        )
    )


def get_llm() -> LLM:
    if settings.llm_provider in {"deepseek", "minimax", "glm"}:
        if settings.llm_provider == "glm":
            from app.llm.glm import GLMLLM
            factory = GLMLLM
        elif settings.llm_provider == "minimax":
            from app.llm.minimax_chat import MiniMaxLLM
            factory = MiniMaxLLM
        else:
            from app.llm.deepseek import DeepSeekLLM
            factory = DeepSeekLLM
        config = (
            settings.llm_provider,
            settings.minimax_api_key,
            settings.minimax_embedding_base_url,
            settings.minimax_embed_model,
            settings.minimax_api_base,
            settings.minimax_model,
            settings.glm_api_key,
            settings.glm_api_base,
            settings.glm_model,
            settings.glm_embed_model,
            str(settings.llm_embedding_dimension),
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
