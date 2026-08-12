# 中文导读：工厂层把配置中的短名称（如 deepseek、qdrant）解析成具体实现。
# 使用字符串类路径和延迟 import，可以让未选中的 provider 不必安装其可选依赖。
import importlib
import inspect
from typing import Dict, Optional, Union

from mem.configs.embeddings.base import BaseEmbedderConfig
from mem.configs.llms.anthropic import AnthropicConfig
from mem.configs.llms.aws_bedrock import AWSBedrockConfig
from mem.configs.llms.azure import AzureOpenAIConfig
from mem.configs.llms.base import BaseLlmConfig
from mem.configs.llms.deepseek import DeepSeekConfig
from mem.configs.llms.gemini import GeminiConfig
from mem.configs.llms.lmstudio import LMStudioConfig
from mem.configs.llms.ollama import OllamaConfig
from mem.configs.llms.openai import OpenAIConfig
from mem.configs.llms.vllm import VllmConfig
from mem.configs.llms.xai import XAIConfig
from mem.configs.rerankers.base import BaseRerankerConfig
from mem.configs.rerankers.cohere import CohereRerankerConfig
from mem.configs.rerankers.huggingface import HuggingFaceRerankerConfig
from mem.configs.rerankers.llm import LLMRerankerConfig
from mem.configs.rerankers.sentence_transformer import (
    SentenceTransformerRerankerConfig,
)
from mem.configs.rerankers.zero_entropy import ZeroEntropyRerankerConfig
from mem.embeddings.mock import MockEmbeddings


def load_class(class_type):
    # 例："mem.llms.deepseek.DeepSeekLLM" 被拆成模块路径和类名后动态导入。
    module_path, class_name = class_type.rsplit(".", 1)
    module = importlib.import_module(module_path)
    return getattr(module, class_name)


class LlmFactory:
    """
    Factory for creating LLM instances with appropriate configurations.
    Supports both old-style BaseLlmConfig and new provider-specific configs.
    """

    # 每个 LLM 同时绑定实现类与 provider 专属 Pydantic 配置类。
    provider_to_class = {
        "ollama": ("mem.llms.ollama.OllamaLLM", OllamaConfig),
        "openai": ("mem.llms.openai.OpenAILLM", OpenAIConfig),
        "groq": ("mem.llms.groq.GroqLLM", BaseLlmConfig),
        "together": ("mem.llms.together.TogetherLLM", BaseLlmConfig),
        "aws_bedrock": ("mem.llms.aws_bedrock.AWSBedrockLLM", AWSBedrockConfig),
        "litellm": ("mem.llms.litellm.LiteLLM", BaseLlmConfig),
        "azure_openai": ("mem.llms.azure_openai.AzureOpenAILLM", AzureOpenAIConfig),
        "openai_structured": ("mem.llms.openai_structured.OpenAIStructuredLLM", OpenAIConfig),
        "anthropic": ("mem.llms.anthropic.AnthropicLLM", AnthropicConfig),
        "azure_openai_structured": ("mem.llms.azure_openai_structured.AzureOpenAIStructuredLLM", AzureOpenAIConfig),
        "gemini": ("mem.llms.gemini.GeminiLLM", GeminiConfig),
        "deepseek": ("mem.llms.deepseek.DeepSeekLLM", DeepSeekConfig),
        "xai": ("mem.llms.xai.XAILLM", XAIConfig),
        "sarvam": ("mem.llms.sarvam.SarvamLLM", BaseLlmConfig),
        "lmstudio": ("mem.llms.lmstudio.LMStudioLLM", LMStudioConfig),
        "vllm": ("mem.llms.vllm.VllmLLM", VllmConfig),
        "langchain": ("mem.llms.langchain.LangchainLLM", BaseLlmConfig),
    }

    @classmethod
    def create(cls, provider_name: str, config: Optional[Union[BaseLlmConfig, Dict]] = None, **kwargs):
        """
        Create an LLM instance with the appropriate configuration.

        Args:
            provider_name (str): The provider name (e.g., 'openai', 'anthropic')
            config: Configuration object or dict. If None, will create default config
            **kwargs: Additional configuration parameters

        Returns:
            Configured LLM instance

        Raises:
            ValueError: If provider is not supported
        """
        if provider_name not in cls.provider_to_class:
            raise ValueError(f"Unsupported Llm provider: {provider_name}")

        class_type, config_class = cls.provider_to_class[provider_name]
        llm_class = load_class(class_type)

        # 兼容三种输入：无配置、普通 dict、旧版 BaseLlmConfig。最后统一得到
        # provider 能验证的配置对象，再交给具体 LLM 类。
        if config is None:
            # Create default config with kwargs
            config = config_class(**kwargs)
        elif isinstance(config, dict):
            # Merge dict config with kwargs
            config = {**config, **kwargs}
            config = config_class(**config)
        elif isinstance(config, BaseLlmConfig):
            # Convert base config to provider-specific config if needed
            if config_class != BaseLlmConfig:
                # Convert to provider-specific config
                config_dict = {
                    "model": config.model,
                    "temperature": config.temperature,
                    "api_key": config.api_key,
                    "max_tokens": config.max_tokens,
                    "top_p": config.top_p,
                    "top_k": config.top_k,
                    "enable_vision": config.enable_vision,
                    "vision_details": config.vision_details,
                    "http_client_proxies": config.http_client_proxies,
                }
                # Only forward reasoning fields to provider configs that accept them
                # (explicitly or via **kwargs); others would raise on unexpected kwargs.
                params = inspect.signature(config_class).parameters
                accepts_kwargs = any(p.kind == p.VAR_KEYWORD for p in params.values())
                if accepts_kwargs or "reasoning_effort" in params:
                    config_dict["reasoning_effort"] = config.reasoning_effort
                if accepts_kwargs or "is_reasoning_model" in params:
                    config_dict["is_reasoning_model"] = config.is_reasoning_model
                config_dict.update(kwargs)
                config = config_class(**config_dict)
            else:
                # Use base config as-is
                pass
        else:
            # Assume it's already the correct config type
            pass

        return llm_class(config)

    @classmethod
    def register_provider(cls, name: str, class_path: str, config_class=None):
        """
        Register a new provider.

        Args:
            name (str): Provider name
            class_path (str): Full path to LLM class
            config_class: Configuration class for the provider (defaults to BaseLlmConfig)
        """
        if config_class is None:
            config_class = BaseLlmConfig
        cls.provider_to_class[name] = (class_path, config_class)

    @classmethod
    def get_supported_providers(cls) -> list:
        """
        Get list of supported providers.

        Returns:
            list: List of supported provider names
        """
        return list(cls.provider_to_class.keys())


class EmbedderFactory:
    # Embedder 的配置结构较统一，因此映射只需保存实现类路径。
    provider_to_class = {
        "openai": "mem.embeddings.openai.OpenAIEmbedding",
        "ollama": "mem.embeddings.ollama.OllamaEmbedding",
        "huggingface": "mem.embeddings.huggingface.HuggingFaceEmbedding",
        "azure_openai": "mem.embeddings.azure_openai.AzureOpenAIEmbedding",
        "gemini": "mem.embeddings.gemini.GoogleGenAIEmbedding",
        "vertexai": "mem.embeddings.vertexai.VertexAIEmbedding",
        "together": "mem.embeddings.together.TogetherEmbedding",
        "lmstudio": "mem.embeddings.lmstudio.LMStudioEmbedding",
        "langchain": "mem.embeddings.langchain.LangchainEmbedding",
        "aws_bedrock": "mem.embeddings.aws_bedrock.AWSBedrockEmbedding",
        "fastembed": "mem.embeddings.fastembed.FastEmbedEmbedding",
    }

    @classmethod
    def create(cls, provider_name, config, vector_config: Optional[dict]):
        # Upstash 可由服务端生成 embedding；此时返回占位实现，避免本地重复计算。
        if provider_name == "upstash_vector" and vector_config and vector_config.enable_embeddings:
            return MockEmbeddings()
        class_type = cls.provider_to_class.get(provider_name)
        if class_type:
            embedder_instance = load_class(class_type)
            base_config = BaseEmbedderConfig(**config)
            return embedder_instance(base_config)
        else:
            raise ValueError(f"Unsupported Embedder provider: {provider_name}")


class VectorStoreFactory:
    # 所有 provider 都实现 VectorStoreBase 的增删改查约定。读主流程时只需先看
    # qdrant.py；其余文件主要是相同协议到不同数据库 SDK 的翻译层。
    provider_to_class = {
        "qdrant": "mem.vector_stores.qdrant.Qdrant",
        "chroma": "mem.vector_stores.chroma.ChromaDB",
        "pgvector": "mem.vector_stores.pgvector.PGVector",
        "milvus": "mem.vector_stores.milvus.MilvusDB",
        "upstash_vector": "mem.vector_stores.upstash_vector.UpstashVector",
        "azure_ai_search": "mem.vector_stores.azure_ai_search.AzureAISearch",
        "azure_mysql": "mem.vector_stores.azure_mysql.AzureMySQL",
        "pinecone": "mem.vector_stores.pinecone.PineconeDB",
        "mongodb": "mem.vector_stores.mongodb.MongoDB",
        "redis": "mem.vector_stores.redis.RedisDB",
        "valkey": "mem.vector_stores.valkey.ValkeyDB",
        "databricks": "mem.vector_stores.databricks.Databricks",
        "elasticsearch": "mem.vector_stores.elasticsearch.ElasticsearchDB",
        "vertex_ai_vector_search": "mem.vector_stores.vertex_ai_vector_search.GoogleMatchingEngine",
        "opensearch": "mem.vector_stores.opensearch.OpenSearchDB",
        "supabase": "mem.vector_stores.supabase.Supabase",
        "weaviate": "mem.vector_stores.weaviate.Weaviate",
        "faiss": "mem.vector_stores.faiss.FAISS",
        "langchain": "mem.vector_stores.langchain.Langchain",
        "s3_vectors": "mem.vector_stores.s3_vectors.S3Vectors",
        "baidu": "mem.vector_stores.baidu.BaiduDB",
        "cassandra": "mem.vector_stores.cassandra.CassandraDB",
        "neptune": "mem.vector_stores.neptune_analytics.NeptuneAnalyticsVector",
        "turbopuffer": "mem.vector_stores.turbopuffer.TurbopufferDB",
        "oracledb": "mem.vector_stores.oracledb.OracleAIVectorSearch",
    }

    @classmethod
    def create(cls, provider_name, config):
        class_type = cls.provider_to_class.get(provider_name)
        if class_type:
            if not isinstance(config, dict):
                config = config.model_dump()
            vector_store_instance = load_class(class_type)
            return vector_store_instance(**config)
        else:
            raise ValueError(f"Unsupported VectorStore provider: {provider_name}")

    @classmethod
    def reset(cls, instance):
        instance.reset()
        return instance


class RerankerFactory:
    """
    Factory for creating reranker instances with appropriate configurations.
    Supports provider-specific configs following the same pattern as other factories.
    """

    # 重排发生在初次召回后，所以它不会影响存储格式，只改变最终结果顺序。
    provider_to_class = {
        "cohere": ("mem.reranker.cohere_reranker.CohereReranker", CohereRerankerConfig),
        "sentence_transformer": (
            "mem.reranker.sentence_transformer_reranker.SentenceTransformerReranker",
            SentenceTransformerRerankerConfig,
        ),
        "zero_entropy": ("mem.reranker.zero_entropy_reranker.ZeroEntropyReranker", ZeroEntropyRerankerConfig),
        "llm_reranker": ("mem.reranker.llm_reranker.LLMReranker", LLMRerankerConfig),
        "huggingface": ("mem.reranker.huggingface_reranker.HuggingFaceReranker", HuggingFaceRerankerConfig),
    }

    @classmethod
    def create(cls, provider_name: str, config: Optional[Union[BaseRerankerConfig, Dict]] = None, **kwargs):
        """
        Create a reranker instance based on the provider and configuration.

        Args:
            provider_name: The reranker provider (e.g., 'cohere', 'sentence_transformer')
            config: Configuration object or dictionary
            **kwargs: Additional configuration parameters

        Returns:
            Reranker instance configured for the specified provider

        Raises:
            ImportError: If the provider class cannot be imported
            ValueError: If the provider is not supported
        """
        if provider_name not in cls.provider_to_class:
            raise ValueError(f"Unsupported reranker provider: {provider_name}")

        class_path, config_class = cls.provider_to_class[provider_name]

        # Handle configuration
        if config is None:
            config = config_class(**kwargs)
        elif isinstance(config, dict):
            config = config_class(**config, **kwargs)
        elif not isinstance(config, BaseRerankerConfig):
            raise ValueError(f"Config must be a {config_class.__name__} instance or dict")

        # Import and create the reranker class
        try:
            reranker_class = load_class(class_path)
        except (ImportError, AttributeError) as e:
            raise ImportError(f"Could not import reranker for provider '{provider_name}': {e}")

        return reranker_class(config)
