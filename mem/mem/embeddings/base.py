# 中文导读：所有 embedding provider 的最小协议。主流程只依赖 embed/embed_batch，
# 因而可以在不修改 Memory 的情况下切换 OpenAI、DashScope 兼容接口或本地模型。
from abc import ABC, abstractmethod
from typing import Literal, Optional

from mem.configs.embeddings.base import BaseEmbedderConfig


class EmbeddingBase(ABC):
    """Initialized a base embedding class

    :param config: Embedding configuration option class, defaults to None
    :type config: Optional[BaseEmbedderConfig], optional
    """

    def __init__(self, config: Optional[BaseEmbedderConfig] = None):
        if config is None:
            self.config = BaseEmbedderConfig()
        else:
            self.config = config

    @abstractmethod
    def embed(self, text, memory_action: Optional[Literal["add", "search", "update"]]):
        """
        Get the embedding for the given text.

        Args:
            text (str): The text to embed.
            memory_action (optional): The type of embedding to use. Must be one of "add", "search", or "update". Defaults to None.
        Returns:
            list: The embedding vector.
        """
        pass

    def embed_batch(self, texts, memory_action="add"):
        """Embed multiple texts. Override in subclasses for native batch support.

        Default implementation calls embed() sequentially for each text.
        Subclasses with native batch APIs (e.g., OpenAI) should override
        this for better performance.

        Args:
            texts: List of text strings to embed.
            memory_action: The action context ("add", "search", "update").

        Returns:
            List of embedding vectors (list of floats), one per input text.
        """
        # 默认实现保证正确性；支持原生批接口的子类覆写它以减少网络往返。
        return [self.embed(text, memory_action) for text in texts]
