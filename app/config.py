from pydantic_settings import BaseSettings, SettingsConfigDict

GLM_EMBEDDING_DIMENSIONS = (256, 512, 1024, 2048)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # dev | prod。prod 触发启动自检，拒绝用弱/默认密钥上线（见 validate_production_secrets）。
    app_env: str = "dev"

    database_url: str = "sqlite+pysqlite:///./acs.db"
    jwt_secret: str = "dev-insecure-change-me-please-set-a-strong-random-secret"
    jwt_alg: str = "HS256"
    jwt_expire_minutes: int = 60 * 12
    bootstrap_token: str = "change-me-bootstrap"

    field_enc_key: str = ""            # base64 32B；空则从 jwt_secret 派生(dev)，prod 必配
    llm_provider: str = "minimax"
    llm_embedding_dimension: int = 1536
    minimax_api_key: str = ""
    minimax_api_base: str = "https://api.minimaxi.com/v1"
    minimax_model: str = "MiniMax-M3"
    minimax_embedding_base_url: str = "https://api.minimaxi.com/v1"
    minimax_embed_model: str = "embo-01"
    # 备用智谱组合：glm-4.7 负责对话，embedding-3 负责知识库向量化。
    glm_api_key: str = ""
    glm_api_base: str = "https://open.bigmodel.cn/api/paas/v4"
    glm_model: str = "glm-4.7"
    glm_embed_model: str = "embedding-3"
    # OpenAI Agents SDK：所有消息由 Triage 使用当前对话模型选择售后知识库、闲聊或人工 Agent。
    agent_history_limit: int = 12
    agent_max_turns: int = 4
    # Triage + Handoff 目标 Agent 的整条工作流硬上限；桌面端 chat timeout 必须略大于它。
    agent_workflow_timeout_seconds: float = 120.0
    # DeepSeek 为可选对话后端，搭配 MiniMax embedding。
    deepseek_api_key: str = ""
    deepseek_api_base: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-v4-flash"
    deepseek_thinking: bool = False  # 客服优先低延迟；true 时 DeepSeek 会忽略 temperature
    rag_top_k: int = 4
    rag_chunk_size: int = 500
    rag_chunk_overlap: int = 80
    # 余弦距离；<= 此值算命中(auto_reply)，否则转人工。
    # MiniMax embo-01 在当前已审核知识库上的实测分隔：业务同义问法约 0.07~0.57，
    # 闲聊样本约 0.78~0.96；取 0.60 保留安全间隔。更换模型或知识库后必须重新校准。
    rag_distance_cutoff: float = 0.60
    kb_upload_max_bytes: int = 10 * 1024 * 1024  # 知识库上传单文件上限
    profile_min_chars: int = 4   # 客户消息短于此（或纯寒暄）不触发画像抽取，省 LLM 额度
    # 记忆工作台调用仓库内的本地 Mem0 OSS 服务。服务由交付启动器绑定到 127.0.0.1，
    # 数据目录固定为项目自带的 LoCoMo 索引，不经过 Mem0 Cloud。
    mem0_base_url: str = "http://127.0.0.1:8888"
    # 对话提取包含模型推理和向量化，100 条消息会分批执行；45 秒会让正常任务被误判失败。
    mem0_timeout_seconds: float = 120.0
    # 在线客服不能被记忆服务的长任务超时拖住。聊天召回是可选上下文，超过该时限即降级为无记忆回答；
    # 记忆提取、治理等工作台操作仍沿用上面的长超时。
    mem0_chat_recall_timeout_seconds: float = 1.5
    # 后端→本地 Mem0 companion 的共享凭据。留空时双方从 FIELD_ENC_KEY/JWT_SECRET
    # 确定性派生；生产可显式设置独立强随机值。
    mem0_service_token: str = ""
    admin_cookie_secure: bool = False
    admin_login_max_attempts: int = 5     # 单账号：窗口内失败上限
    admin_login_ip_max_attempts: int = 20  # 单 IP：窗口内失败总上限（挡撞库）
    admin_login_window_s: int = 300


settings = Settings()


def secret_is_configured(value: str) -> bool:
    normalized = str(value or "").strip()
    return bool(normalized) and normalized not in {"待填写", "change-me", "your-api-key"}


# 这些是仓库里公开的 dev 占位值：谁都知道，绝不能带到生产。
_INSECURE_DEFAULTS = {
    "jwt_secret": "dev-insecure-change-me-please-set-a-strong-random-secret",
    "bootstrap_token": "change-me-bootstrap",
}


def validate_agent_routing_config(s: Settings = settings) -> None:
    """Reject configurations that cannot execute the mandatory Triage Handoff turn."""
    provider = s.llm_provider.strip().lower()
    problems: list[str] = []
    if provider == "fake":
        problems.append("llm_provider=fake 不支持 Agents SDK Handoff")
    elif provider == "minimax":
        if not secret_is_configured(s.minimax_api_key):
            problems.append("llm_provider=minimax 但 minimax_api_key 为空")
    elif provider == "glm":
        if not secret_is_configured(s.glm_api_key):
            problems.append("llm_provider=glm 但 glm_api_key 为空")
        if s.llm_embedding_dimension not in GLM_EMBEDDING_DIMENSIONS:
            problems.append("GLM embedding-3 维度必须为 256/512/1024/2048；切换后需重建知识库索引")
    elif provider == "deepseek":
        if not secret_is_configured(s.deepseek_api_key):
            problems.append("llm_provider=deepseek 但 deepseek_api_key 为空")
        if not secret_is_configured(s.minimax_api_key):
            problems.append("llm_provider=deepseek 但 minimax_api_key 为空")
    else:
        problems.append(f"不支持的 llm_provider={provider or '<empty>'}")
    if problems:
        raise RuntimeError("Agent 路由配置无效：\n  - " + "\n  - ".join(problems))


def validate_production_secrets(s: Settings = settings) -> None:
    """生产环境（app_env=prod 或 admin_cookie_secure=True）下，拒绝用弱/默认密钥启动。

    fail-closed：宁可启动即崩、也不要带着"人人可伪造 JWT / bootstrap 任意租户"的洞对外服务。
    dev（默认）完全不受影响，本地照旧用占位密钥跑。
    """
    is_prod = s.app_env.lower() == "prod" or s.admin_cookie_secure
    if not is_prod:
        return

    problems: list[str] = []
    for name, insecure in _INSECURE_DEFAULTS.items():
        val = getattr(s, name) or ""
        if val == insecure or len(val) < 16:
            problems.append(f"{name} 仍是默认/过弱值，请设为足够长的强随机串")
    if not s.field_enc_key:
        problems.append("field_enc_key 未配置（生产必须配独立 base64 32B 密钥，不能从 jwt_secret 派生）")
    if s.llm_provider == "minimax":
        if not secret_is_configured(s.minimax_api_key):
            problems.append("llm_provider=minimax 但 minimax_api_key 为空")
    elif s.llm_provider == "glm":
        if not secret_is_configured(s.glm_api_key):
            problems.append("llm_provider=glm 但 glm_api_key 为空")
    elif s.llm_provider == "deepseek":
        if not secret_is_configured(s.deepseek_api_key):
            problems.append("llm_provider=deepseek 但 deepseek_api_key 为空")
        if not secret_is_configured(s.minimax_api_key):
            problems.append("llm_provider=deepseek 但 minimax_api_key 为空")

    if problems:
        raise RuntimeError(
            "生产环境安全自检未通过（app_env=prod / admin_cookie_secure=True）：\n  - "
            + "\n  - ".join(problems)
        )
