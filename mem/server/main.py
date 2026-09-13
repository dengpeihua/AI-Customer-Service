"""Local-only FastAPI adapter for the open-source Mem0 memory algorithm."""

# 中文导读：这是 benchmark 与 Python SDK 之间的薄适配层，不包含记忆算法。
# 请求模型负责把 HTTP JSON 转成 Memory.add/search 参数；数据仍落在本地 Qdrant
# 与 SQLite。这里故意没有 Cloud client、鉴权、Dashboard 或遥测。

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import sqlite3
import threading
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field


PROJECT_ROOT = Path(__file__).resolve().parent.parent
# The product backend and the local Mem0 companion must derive the same private
# loopback credential. Model-specific values can still live in mem0/.env.
load_dotenv(PROJECT_ROOT.parent / ".env", override=False)
load_dotenv(PROJECT_ROOT / ".env", override=False)

# Defense in depth: this fork also ships a no-op telemetry module.
os.environ["MEM0_TELEMETRY"] = "false"

from mem import Memory, __version__


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("mem0-local")


def _service_token() -> str:
    explicit = os.getenv("MEM0_SERVICE_TOKEN", "").strip()
    if explicit:
        return explicit
    material = (
        os.getenv("FIELD_ENC_KEY", "").strip()
        or os.getenv("JWT_SECRET", "").strip()
        or "dev-insecure-change-me-please-set-a-strong-random-secret"
    )
    return hashlib.sha256(
        f"ai-customer-service:mem0:{material}".encode("utf-8")
    ).hexdigest()


SERVICE_TOKEN = _service_token()
SERVICE_REVISION = "acs-mem0-companion-v5-resilient-dedupe"
_OPERATION_LOCK = threading.Lock()
SERVER_BOOT_ID = uuid.uuid4().hex

_SECRET_PATTERNS = (
    re.compile(r"(?i)\b(authorization\s*:\s*bearer|bearer)\s+[^\s,;]+"),
    re.compile(
        r"(?i)\b(api[_-]?key|token|secret|password)\s*[:=]\s*[\"']?[^\s,;}\"']+"
    ),
    re.compile(r"(?i)\bsk-[a-z0-9_-]{6,}\b"),
)


def _safe_error_detail(exc: Exception) -> str:
    """Return useful stage/type context without exposing credentials or request bodies."""
    detail = " ".join(str(exc).split())
    detail = _SECRET_PATTERNS[0].sub(r"\1 [REDACTED]", detail)
    detail = _SECRET_PATTERNS[1].sub(r"\1=[REDACTED]", detail)
    detail = _SECRET_PATTERNS[2].sub("[REDACTED]", detail)
    return f"{type(exc).__name__}: {detail[:500]}" if detail else type(exc).__name__


def _required(name: str) -> str:
    value = str(os.getenv(name) or "").strip()
    if not value or value in {"待填写", "change-me", "your-api-key"}:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def _minimax_embedding_config() -> dict[str, Any]:
    """Build the MiniMax OpenAI-compatible embedding configuration."""
    provider = os.getenv("MEM0_EMBEDDING_PROVIDER", "minimax").strip().lower()
    if provider != "minimax":
        raise RuntimeError("MEM0_EMBEDDING_PROVIDER must be minimax")
    base_url = os.getenv(
        "MINIMAX_EMBEDDING_BASE_URL",
        "https://api.minimaxi.com/v1",
    ).strip()
    parsed = urlparse(base_url)
    if parsed.scheme != "https" or parsed.hostname != "api.minimaxi.com":
        raise RuntimeError(
            "MINIMAX_EMBEDDING_BASE_URL must use MiniMax at "
            "https://api.minimaxi.com/v1"
        )
    return {
        "api_key": _required("MINIMAX_API_KEY"),
        "openai_base_url": base_url,
        "model": _required("MINIMAX_EMBED_MODEL"),
    }


def build_config() -> dict[str, Any]:
    """Translate the shared benchmark .env keys into a Mem0 OSS config."""
    # 所有运行时路径都锚定仓库 data/。模型 provider 可走外部兼容 API，但记忆
    # 本体不上传 Mem0 Cloud；密钥只从 .env/环境读取，绝不写入返回值或日志。
    data_dir = Path(
        os.getenv("MEM0_DATA_DIR")
        or os.getenv("MEM0_LOCAL_DATA_DIR")
        or PROJECT_ROOT / "data"
    ).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)

    embedding_config = _minimax_embedding_config()
    embedding_dims = int(os.getenv("EMBEDDING_DIMS", "1536"))

    llm_provider = "deepseek" if os.getenv("DEEPSEEK_API_KEY") else "openai"
    if llm_provider != "deepseek":
        raise RuntimeError("The shared benchmark .env must provide DEEPSEEK_API_KEY")

    return {
        "version": "v1.1",
        "llm": {
            "provider": "deepseek",
            "config": {
                "api_key": _required("DEEPSEEK_API_KEY"),
                "deepseek_base_url": os.getenv("DEEPSEEK_API_BASE", "https://api.deepseek.com"),
                "model": _required("DEEPSEEK_MODEL"),
                "temperature": 0.1,
                "thinking": os.getenv("DEEPSEEK_THINKING", "false").strip().lower()
                in {"1", "true", "yes", "on"},
            },
        },
        "embedder": {
            # Mem0 names the adapter "openai" because MiniMax exposes this
            # endpoint in OpenAI-compatible form. The external selector stays minimax.
            "provider": "openai",
            "config": embedding_config,
        },
        "vector_store": {
            "provider": "qdrant",
            "config": {
                "path": str(data_dir / "qdrant"),
                "collection_name": os.getenv("COLLECTION_NAME", "memories"),
                "embedding_model_dims": embedding_dims,
                "on_disk": True,
            },
        },
        "history_db_path": str(data_dir / "history.db"),
    }


config = build_config()
memory_instance: Memory | None = None


def _operation_db_path() -> Path:
    return Path(config["history_db_path"]).parent / "operations.db"


def _init_operation_db() -> None:
    with sqlite3.connect(_operation_db_path()) as db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS completed_operation "
            "(operation_id TEXT PRIMARY KEY, status TEXT NOT NULL DEFAULT 'running', "
            "owner_boot TEXT NOT NULL DEFAULT '', response_json TEXT NOT NULL DEFAULT '{}', "
            "created_at TEXT DEFAULT CURRENT_TIMESTAMP)"
        )
        columns = {
            row[1] for row in db.execute("PRAGMA table_info(completed_operation)").fetchall()
        }
        if "status" not in columns:
            db.execute(
                "ALTER TABLE completed_operation ADD COLUMN status TEXT NOT NULL DEFAULT 'completed'"
            )
        if "owner_boot" not in columns:
            db.execute(
                "ALTER TABLE completed_operation ADD COLUMN owner_boot TEXT NOT NULL DEFAULT ''"
            )


def _claim_operation(operation_id: str) -> tuple[str, dict[str, Any] | None]:
    """Atomically claim an idempotency key for this server boot."""
    with _OPERATION_LOCK, sqlite3.connect(_operation_db_path(), timeout=30) as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT status, owner_boot, response_json FROM completed_operation "
            "WHERE operation_id = ?", (operation_id,),
        ).fetchone()
        if row is None:
            db.execute(
                "INSERT INTO completed_operation(operation_id, status, owner_boot, response_json) "
                "VALUES (?, 'running', ?, '{}')",
                (operation_id, SERVER_BOOT_ID),
            )
            db.commit()
            return "claimed", None
        status, owner_boot, response_json = row
        if status == "completed":
            db.commit()
            return "completed", json.loads(response_json)
        if owner_boot == SERVER_BOOT_ID:
            db.commit()
            return "busy", None
        # A prior process died while owning the operation. This boot may safely
        # resume it; Mem0 consolidation handles infer=True replays, while
        # infer=False is recovered by its operation_id metadata before adding.
        db.execute(
            "UPDATE completed_operation SET owner_boot = ? WHERE operation_id = ?",
            (SERVER_BOOT_ID, operation_id),
        )
        db.commit()
        return "claimed", None


def _record_completed_operation(operation_id: str, response: dict[str, Any]) -> None:
    if not operation_id:
        return
    encoded = json.dumps(response, ensure_ascii=False)
    with _OPERATION_LOCK, sqlite3.connect(_operation_db_path()) as db:
        db.execute(
            "UPDATE completed_operation SET status = 'completed', response_json = ?, owner_boot = ? "
            "WHERE operation_id = ?",
            (encoded, SERVER_BOOT_ID, operation_id),
        )


def _release_operation(operation_id: str) -> None:
    if not operation_id:
        return
    with _OPERATION_LOCK, sqlite3.connect(_operation_db_path()) as db:
        db.execute(
            "UPDATE completed_operation SET owner_boot = '' "
            "WHERE operation_id = ? AND status = 'running'",
            (operation_id,),
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 进程生命周期内只创建一个 Memory，避免重复打开 SQLite/Qdrant 文件句柄。
    global memory_instance
    logger.info("Initialising local Mem0 %s", __version__)
    memory_instance = Memory.from_config(config)
    _init_operation_db()
    logger.info(
        "Mem0 ready (llm=%s, embedder=%s, collection=%s)",
        config["llm"]["provider"],
        config["embedder"]["provider"],
        config["vector_store"]["config"]["collection_name"],
    )
    try:
        yield
    finally:
        instance = memory_instance
        memory_instance = None
        if instance is not None:
            try:
                instance.close()
            except Exception:
                logger.exception("Failed to close local Mem0 resources cleanly")


app = FastAPI(
    title="Mem0 Local OSS Server",
    description="Private loopback adapter with no Mem0 Cloud client, dashboard, or telemetry.",
    lifespan=lifespan,
)


@app.middleware("http")
async def require_service_token(request: Request, call_next):
    """Only the public liveness probe is callable without the companion token."""
    if request.url.path == "/health":
        return await call_next(request)
    supplied = request.headers.get("X-Mem0-Service-Token", "")
    if not supplied or not hmac.compare_digest(supplied, SERVICE_TOKEN):
        return JSONResponse(status_code=401, content={"detail": "Unauthorized"})
    return await call_next(request)


def _memory() -> Memory:
    # 每个请求前检查数据目录仍存在，避免 benchmark 运行中误删 data/ 后静默写入
    # 一个全新的空库，造成看似成功但不可复现的混合实验。
    if memory_instance is None:
        raise HTTPException(503, "Memory is not initialised")
    missing = _missing_data_paths()
    if missing:
        raise HTTPException(
            503,
            "Local data files disappeared while the server was running: "
            + ", ".join(str(path) for path in missing)
            + ". Stop benchmarks and restart the local Mem0 service.",
        )
    return memory_instance


def _missing_data_paths() -> list[Path]:
    data_dir = Path(config["history_db_path"]).parent
    required = (data_dir, data_dir / "qdrant", Path(config["history_db_path"]))
    return [path for path in required if not path.exists()]


def _scope(user_id: str | None, agent_id: str | None, run_id: str | None) -> dict[str, str]:
    # 作用域是多租户隔离边界；至少提供一个 ID，所有查询才不会扫描全库。
    filters = {
        key: value
        for key, value in {"user_id": user_id, "agent_id": agent_id, "run_id": run_id}.items()
        if value
    }
    if not filters:
        raise HTTPException(400, "Provide at least one of: user_id, agent_id, run_id")
    return filters


class AddRequest(BaseModel):
    messages: list[dict[str, Any]]
    user_id: str | None = None
    agent_id: str | None = None
    run_id: str | None = None
    metadata: dict[str, Any] | None = None
    timestamp: int | None = None
    observation_date: str | None = None
    custom_instructions: str | None = None
    infer: bool = True
    operation_id: str | None = Field(default=None, min_length=1, max_length=80)


class SearchRequest(BaseModel):
    query: str
    user_id: str | None = None
    agent_id: str | None = None
    run_id: str | None = None
    limit: int = Field(default=100, ge=1, le=1000)
    top_k: int | None = Field(default=None, ge=1, le=1000)
    filters: dict[str, Any] | None = None
    rerank: bool = False
    score_debug: bool = False


class UpdateRequest(BaseModel):
    data: str
    user_id: str


def _scoped_memory(
    memory_id: str, user_id: str, *, missing_ok: bool = False
) -> dict[str, Any] | None:
    """Resolve an ID only when it belongs to the caller's exact user scope."""
    record = _memory().get(memory_id)
    if record is None and missing_ok:
        return None
    if not isinstance(record, dict) or record.get("user_id") != user_id:
        # Deliberately use the same response for a missing ID and a foreign ID.
        raise HTTPException(404, "Memory not found")
    return record


@app.post("/memories")
def add_memories(req: AddRequest):
    if req.operation_id:
        claim, cached = _claim_operation(req.operation_id)
        if claim == "completed" and cached is not None:
            return cached
        if claim == "busy":
            raise HTTPException(409, "Operation is already running")
    params: dict[str, Any] = {}
    for key in ("user_id", "agent_id", "run_id"):
        if value := getattr(req, key):
            params[key] = value

    metadata = dict(req.metadata or {})
    if req.operation_id:
        metadata["operation_id"] = req.operation_id
    # OSS 拒绝 Platform 专属 timestamp。benchmark 时间只作为来源信息写 metadata，
    # 不注入抽取 Prompt，因此不能声称复现 Platform 的时序推理实现。
    if req.timestamp is not None:
        metadata["benchmark_timestamp"] = req.timestamp
    elif req.observation_date is not None:
        metadata["benchmark_observation_date"] = req.observation_date
    if metadata:
        params["metadata"] = metadata
    if req.custom_instructions:
        params["prompt"] = req.custom_instructions

    try:
        if req.operation_id and not req.infer:
            existing_payload = _memory().get_all(
                filters=_scope(req.user_id, req.agent_id, req.run_id), top_k=1000
            )
            existing_rows = (
                existing_payload.get("results", [])
                if isinstance(existing_payload, dict) else existing_payload
            )
            recovered = [
                {
                    "id": str(item.get("id") or ""),
                    "memory": str(item.get("memory") or item.get("data") or ""),
                    "event": "ADD",
                }
                for item in (existing_rows or [])
                if isinstance(item, dict)
                and isinstance(item.get("metadata"), dict)
                and item["metadata"].get("operation_id") == req.operation_id
            ]
            if recovered:
                result = {"results": recovered}
                _record_completed_operation(req.operation_id, result)
                return result
        result = _memory().add(req.messages, infer=req.infer, **params)
        if req.operation_id:
            # An inferred empty result performed no durable vector write. Do not
            # fossilize it as a completed idempotency response: a better prompt,
            # recovered model, or explicit retry must be able to run extraction again.
            rows = result.get("results", []) if isinstance(result, dict) else result
            if req.infer and not rows:
                _release_operation(req.operation_id)
            else:
                _record_completed_operation(req.operation_id, result)
        return result
    except Exception as exc:
        if req.operation_id:
            _release_operation(req.operation_id)
        logger.exception("add() failed")
        raise HTTPException(500, _safe_error_detail(exc)) from exc


@app.post("/search")
def search_memories(req: SearchRequest):
    # 顶层 user/agent/run ID 与显式 filters 合并；同名作用域值以前者覆盖，最终
    # 交给 Memory.search 做高级过滤解析、混合召回与可选 rerank。
    filters = dict(req.filters or {})
    filters.update(_scope(req.user_id, req.agent_id, req.run_id) if any((req.user_id, req.agent_id, req.run_id)) else {})
    if not any(key in filters for key in ("user_id", "agent_id", "run_id")):
        raise HTTPException(400, "filters must include user_id, agent_id, or run_id")

    try:
        return _memory().search(
            req.query,
            top_k=req.top_k or req.limit,
            filters=filters,
            rerank=req.rerank,
            explain=req.score_debug,
        )
    except Exception as exc:
        logger.exception("search() failed")
        raise HTTPException(500, _safe_error_detail(exc)) from exc


@app.get("/memories")
def get_memories(
    user_id: str | None = Query(None),
    agent_id: str | None = Query(None),
    run_id: str | None = Query(None),
    limit: int = Query(200, ge=1, le=1000),
):
    try:
        return _memory().get_all(filters=_scope(user_id, agent_id, run_id), top_k=limit)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(500, _safe_error_detail(exc)) from exc


@app.get("/memories/{memory_id}")
def get_memory(memory_id: str, user_id: str = Query(min_length=1)):
    try:
        return _scoped_memory(memory_id, user_id)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(404, _safe_error_detail(exc)) from exc


@app.put("/memories/{memory_id}")
def update_memory(memory_id: str, req: UpdateRequest):
    try:
        _scoped_memory(memory_id, req.user_id)
        return _memory().update(memory_id, text=req.data)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(500, _safe_error_detail(exc)) from exc


@app.delete("/memories/{memory_id}")
def delete_memory(
    memory_id: str,
    user_id: str = Query(min_length=1),
    missing_ok: bool = Query(False),
):
    try:
        record = _scoped_memory(memory_id, user_id, missing_ok=missing_ok)
        if record is None:
            return {"message": "Memory already absent"}
        _memory().delete(memory_id)
        return {"message": "Memory deleted"}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(500, _safe_error_detail(exc)) from exc


@app.delete("/memories")
def delete_user_memories(user_id: str = Query(min_length=1)):
    """Benchmark cleanup is allowed only for one authenticated user scope."""
    try:
        _memory().delete_all(user_id=user_id)
        return {"message": "Scoped memories deleted"}
    except Exception as exc:
        raise HTTPException(500, _safe_error_detail(exc)) from exc


@app.get("/memories/{memory_id}/history")
def memory_history(memory_id: str, user_id: str = Query(min_length=1)):
    try:
        _scoped_memory(memory_id, user_id)
        return _memory().history(memory_id)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(500, _safe_error_detail(exc)) from exc


@app.get("/health")
def health(response: Response):
    missing = _missing_data_paths()
    if missing:
        response.status_code = 503
    return {
        "status": "ok" if not missing else "degraded",
        "mode": "local-oss",
        "mem0_version": __version__,
        "llm": config["llm"]["provider"],
        "embedder": config["embedder"]["provider"],
        "collection": config["vector_store"]["config"]["collection_name"],
        "data_dir": str(Path(config["history_db_path"]).parent),
        "missing_data_paths": [str(path) for path in missing],
        "telemetry": False,
        "service_auth": "token",
        "service_revision": SERVICE_REVISION,
    }


@app.get("/")
def root():
    return {"message": "Mem0 Local OSS Server", "docs": "/docs"}
