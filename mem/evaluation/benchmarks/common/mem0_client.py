"""
Mem0 Client
===========

Async client for the local Mem0 OSS server. It exposes the interface used by
the benchmark runners without carrying a Mem0 Cloud client or API-key path:
  client.add(messages, user_id, ...)
  client.search(query, user_id, top_k=200, ...)
  client.delete_user(user_id)
"""

# 中文导读：三个 benchmark 只依赖这个统一异步客户端，因此评测运行器无需知道
# Mem0 内部类。当前 fork 固定走本地 OSS HTTP 服务；重试处理临时网络/模型错误，
# 但最终失败返回 None/空结果并由上层 checkpoint 记录。

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiohttp
from aiolimiter import AsyncLimiter
from dotenv import dotenv_values

logger = logging.getLogger(__name__)


def _service_token(explicit: str = "") -> str:
    if explicit.strip():
        return explicit.strip()
    repository_root = Path(__file__).resolve().parents[3]
    root_values = dotenv_values(repository_root.parent / ".env")
    mem0_values = dotenv_values(repository_root / ".env")
    configured = str(
        os.getenv("MEM0_SERVICE_TOKEN")
        or root_values.get("MEM0_SERVICE_TOKEN")
        or mem0_values.get("MEM0_SERVICE_TOKEN")
        or ""
    ).strip()
    if configured:
        return configured
    material = str(
        os.getenv("FIELD_ENC_KEY")
        or root_values.get("FIELD_ENC_KEY")
        or os.getenv("JWT_SECRET")
        or root_values.get("JWT_SECRET")
        or "dev-insecure-change-me-please-set-a-strong-random-secret"
    ).strip()
    return hashlib.sha256(
        f"ai-customer-service:mem0:{material}".encode("utf-8")
    ).hexdigest()


class Mem0Client:
    """Async client for the local OSS server.

    Args:
        mode: Must be ``oss``. Retained for CLI compatibility.
        host: Server URL. Defaults to MEM0_HOST or http://localhost:8888.
        max_retries: Maximum retry attempts for API calls.
        retry_delay: Base delay in seconds between retries (doubles each attempt).
        rpm: Requests per minute rate limit.
        timeout: HTTP request timeout in seconds.
    """

    def __init__(
        self,
        mode: str = "oss",
        host: str | None = None,
        max_retries: int = 5,
        retry_delay: float = 5.0,
        rpm: int = 60,
        timeout: float = 300.0,
        service_token: str = "",
    ):
        if mode != "oss":
            raise ValueError("This merged repository supports only the local OSS backend")
        self.mode = "oss"
        self.host = (host or os.getenv("MEM0_HOST", "http://localhost:8888")).rstrip("/")
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self.limiter = AsyncLimiter(100000, 60)  # no client-side rate limiting
        self.service_token = _service_token(service_token)
        self._session: aiohttp.ClientSession | None = None

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "X-Mem0-Service-Token": self.service_token,
        }

    async def _get_session(self) -> aiohttp.ClientSession:
        # 复用一个 ClientSession 和连接池，避免每个问题都重新握手。
        if self._session is None or self._session.closed:
            connector = aiohttp.TCPConnector(limit=0)  # unlimited concurrent connections
            self._session = aiohttp.ClientSession(
                headers=self._headers,
                timeout=self.timeout,
                connector=connector,
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def __aenter__(self) -> Mem0Client:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    async def ensure_available(self) -> None:
        """Fail fast with an actionable message when the OSS server is down."""
        session = await self._get_session()
        try:
            async with session.get(
                f"{self.host}/health",
                timeout=aiohttp.ClientTimeout(total=5),
            ) as response:
                response.raise_for_status()
        except Exception as exc:
            raise RuntimeError(
                f"Mem0 OSS server is unavailable at {self.host}. "
                "Start it with `cd /home/dengpeihua/mem/server && make start`, then retry."
            ) from exc

    # =========================================================================
    # Add
    # =========================================================================

    async def add(
        self,
        messages: list[dict[str, str]],
        user_id: str,
        observation_date: str | None = None,
        timestamp: int | None = None,
        custom_instructions: str | None = None,
        metadata: dict | None = None,
    ) -> dict | None:
        """Add memories from a conversation.

        Returns dict with "results" key listing extracted memories, or None on failure.
        """
        return await self._add_oss(messages, user_id, observation_date, timestamp, custom_instructions, metadata)

    async def _add_oss(
        self, messages, user_id, observation_date, timestamp, custom_instructions, metadata,
    ) -> dict | None:
        """Add via OSS server — synchronous endpoint, no event polling."""
        session = await self._get_session()

        # LOCOMO 的观测时间转换成 epoch 发送，但服务端只把它保存为 provenance。
        payload: dict[str, Any] = {"messages": messages, "user_id": user_id}
        if timestamp is not None:
            payload["timestamp"] = timestamp
        elif observation_date is not None:
            # Convert ISO date to unix epoch
            try:
                d = datetime.strptime(observation_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                payload["timestamp"] = int(d.timestamp())
            except ValueError:
                pass
        if custom_instructions:
            payload["custom_instructions"] = custom_instructions
        if metadata:
            payload["metadata"] = metadata

        # 线性递增退避；一次会话导入可能触发外部 LLM/embedding 调用，超时不能
        # 立即判定为不可恢复，但达到上限后必须让评测看见失败而非伪造成功。
        for attempt in range(self.max_retries):
            try:
                async with self.limiter:
                    async with session.post(f"{self.host}/memories", json=payload) as resp:
                        if resp.status >= 400:
                            body = await resp.text()
                            raise RuntimeError(f"Mem0 ADD HTTP {resp.status}: {body[:500]}")
                        data = await resp.json()

                # Normalise: OSS returns {"results": [...]} directly
                if isinstance(data, dict) and "results" in data:
                    return data
                if isinstance(data, list):
                    return {"results": data}
                return {"results": []}

            except Exception as exc:
                logger.warning("ADD attempt %d/%d failed (user=%s): %s", attempt + 1, self.max_retries, user_id, str(exc)[:200])
                if attempt < self.max_retries - 1:
                    await asyncio.sleep(self.retry_delay * (attempt + 1))
                else:
                    logger.error("ADD failed after %d attempts for user=%s", self.max_retries, user_id)
                    return None

    # =========================================================================
    # Search
    # =========================================================================

    async def search(
        self,
        query: str,
        user_id: str,
        top_k: int = 200,
        rerank: bool = False,
        score_debug: bool = False,
    ) -> list[dict]:
        """Search memories. Returns list of results sorted by score descending."""
        return await self._search_oss(query, user_id, top_k, rerank, score_debug)

    async def _search_oss(self, query, user_id, top_k, rerank, score_debug) -> list[dict]:
        """Search via OSS server."""
        session = await self._get_session()
        payload: dict[str, Any] = {
            "query": query,
            "user_id": user_id,
            "limit": top_k,
        }
        if rerank:
            payload["rerank"] = True
        if score_debug:
            payload["score_debug"] = True

        for attempt in range(self.max_retries):
            try:
                async with self.limiter:
                    async with session.post(f"{self.host}/search", json=payload) as resp:
                        if resp.status >= 400:
                            body = await resp.text()
                            raise RuntimeError(f"Mem0 SEARCH HTTP {resp.status}: {body[:500]}")
                        data = await resp.json()

                # 把服务端/版本差异统一成 benchmark 只认识的 memory/score/id 结构。
                results = data.get("results", data) if isinstance(data, dict) else data
                if not isinstance(results, list):
                    results = []

                normalised = []
                for r in results:
                    entry: dict[str, Any] = {
                        "memory": r.get("memory", r.get("data", "")),
                        "score": r.get("score", 0),
                        "id": r.get("id", ""),
                    }
                    if r.get("created_at"):
                        entry["created_at"] = r["created_at"]
                    if r.get("updated_at"):
                        entry["updated_at"] = r["updated_at"]
                    # Map score_breakdown → score_debug for consistency
                    breakdown = (
                        r.get("score_details")
                        or r.get("score_breakdown")
                        or r.get("score_debug")
                    )
                    if breakdown:
                        entry["score_debug"] = {
                            "combined_score": r.get("score", 0),
                            "semantic_score": breakdown.get("semantic", 0),
                            "bm25_score": breakdown.get("bm25", 0),
                            "entity_boost": breakdown.get("entity_boost", 0),
                        }
                    normalised.append(entry)

                normalised.sort(key=lambda x: x.get("score", 0), reverse=True)
                return normalised

            except Exception as exc:
                logger.warning("SEARCH attempt %d/%d failed (user=%s): %s", attempt + 1, self.max_retries, user_id, str(exc)[:200])
                if attempt < self.max_retries - 1:
                    await asyncio.sleep(self.retry_delay * (attempt + 1))
                else:
                    logger.error("SEARCH failed after %d attempts for user=%s", self.max_retries, user_id)
                    return []

    # =========================================================================
    # Delete
    # =========================================================================

    async def delete_user(self, user_id: str) -> bool:
        """Delete all memories for a user. Returns True on success."""
        return await self._delete_user_oss(user_id)

    async def _delete_user_oss(self, user_id: str) -> bool:
        session = await self._get_session()
        try:
            async with self.limiter:
                async with session.delete(
                    f"{self.host}/memories",
                    params={"user_id": user_id},
                ) as resp:
                    resp.raise_for_status()
            logger.info("Deleted memories for user %s", user_id)
            return True
        except Exception as exc:
            logger.warning("Failed to delete user %s: %s", user_id, exc)
            return False

# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def format_search_results(search_results: list[dict]) -> tuple[list[dict], dict | None]:
    """Normalize search results into a clean format for benchmark output.

    Returns:
        Tuple of (formatted results list, query_debug dict or None).
    """
    if not search_results:
        return [], None

    query_debug = None
    if isinstance(search_results, dict):
        query_debug = search_results.get("query_debug")
        search_results = search_results.get("results", [])

    sorted_results = sorted(search_results, key=lambda x: x.get("score", 0), reverse=True)
    formatted = []
    for r in sorted_results:
        entry: dict[str, Any] = {
            "memory": r.get("memory", ""),
            "score": r.get("score", 0),
            "id": r.get("id", ""),
        }
        if r.get("created_at"):
            entry["created_at"] = r["created_at"]
        if r.get("updated_at"):
            entry["updated_at"] = r["updated_at"]
        if r.get("score_debug"):
            entry["score_debug"] = r["score_debug"]
        formatted.append(entry)
    return formatted, query_debug
