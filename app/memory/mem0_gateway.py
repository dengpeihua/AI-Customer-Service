"""HTTP boundary to the repository-local Mem0 OSS process."""
from __future__ import annotations

import hashlib
import re
import time
from typing import Any

import httpx


class Mem0Unavailable(RuntimeError):
    pass


_SECRET_PATTERNS = (
    re.compile(r"(?i)\b(authorization\s*:\s*bearer|bearer)\s+[^\s,;]+"),
    re.compile(
        r"(?i)\b(api[_-]?key|token|secret|password)\s*[:=]\s*[\"']?[^\s,;}\"']+"
    ),
    re.compile(r"(?i)\bsk-[a-z0-9_-]{6,}\b"),
)


def _redact_error_detail(value: object) -> str:
    detail = " ".join(str(value).split())
    detail = _SECRET_PATTERNS[0].sub(r"\1 [REDACTED]", detail)
    detail = _SECRET_PATTERNS[1].sub(r"\1=[REDACTED]", detail)
    detail = _SECRET_PATTERNS[2].sub("[REDACTED]", detail)
    return detail[:600]


def _http_failure_message(exc: httpx.HTTPStatusError) -> str:
    """Expose a bounded companion error without echoing request data or credentials."""
    response = exc.response
    detail = ""
    try:
        payload = response.json()
        if isinstance(payload, dict):
            raw_detail = payload.get("detail")
            if isinstance(raw_detail, (str, int, float)):
                detail = _redact_error_detail(raw_detail)
    except ValueError:
        pass
    base = f"本地 Mem0 服务返回 HTTP {response.status_code}"
    return f"{base}：{detail}" if detail else base


class Mem0Gateway:
    def __init__(self, base_url: str, *, timeout: float = 45.0, service_token: str = ""):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.service_token = service_token

    def health(self) -> dict[str, Any]:
        try:
            with httpx.Client(base_url=self.base_url, timeout=1.0, trust_env=False) as client:
                response = client.get("/health")
                response.raise_for_status()
                data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            return {"status": "offline", "mode": "local-oss", "detail": str(exc)}
        return data if isinstance(data, dict) else {"status": "offline", "detail": "invalid health response"}

    def add(
        self,
        messages: list[dict[str, str]],
        *,
        user_id: str,
        metadata: dict[str, Any] | None = None,
        infer: bool = True,
        custom_instructions: str | None = None,
        operation_id: str | None = None,
    ) -> dict[str, Any]:
        request: dict[str, Any] = {
            "messages": messages,
            "user_id": user_id,
            "infer": infer,
        }
        if metadata:
            request["metadata"] = metadata
        if custom_instructions:
            request["custom_instructions"] = custom_instructions
        if operation_id:
            request["operation_id"] = operation_id
        payload = self._request("POST", "/memories", json=request)
        if not isinstance(payload, dict):
            raise Mem0Unavailable("本地 Mem0 返回了无法识别的写入格式")
        return payload

    def get_all(self, *, user_id: str, limit: int = 200) -> list[dict[str, Any]]:
        payload = self._request(
            "GET", "/memories", params={"user_id": user_id, "limit": limit}
        )
        raw = payload.get("results", payload) if isinstance(payload, dict) else payload
        if not isinstance(raw, list):
            raise Mem0Unavailable("本地 Mem0 返回了无法识别的记忆列表")
        return [item for item in raw[:limit] if isinstance(item, dict)]

    def update(self, memory_id: str, content: str, *, user_id: str) -> None:
        self._request(
            "PUT", f"/memories/{memory_id}", json={"data": content, "user_id": user_id}
        )

    def delete(self, memory_id: str, *, user_id: str, missing_ok: bool = False) -> None:
        self._request(
            "DELETE", f"/memories/{memory_id}",
            params={"user_id": user_id, "missing_ok": str(missing_ok).lower()},
        )

    def history(self, memory_id: str, *, user_id: str) -> list[dict[str, Any]]:
        payload = self._request(
            "GET", f"/memories/{memory_id}/history", params={"user_id": user_id}
        )
        raw = payload.get("results", payload) if isinstance(payload, dict) else payload
        return [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []

    def search(self, query: str, *, user_id: str, limit: int,
               filters: dict[str, Any] | None = None,
               timeout: float | None = None) -> tuple[list[dict[str, Any]], float]:
        started = time.perf_counter()
        try:
            request_timeout = self.timeout if timeout is None else max(0.1, float(timeout))
            with httpx.Client(
                base_url=self.base_url, timeout=request_timeout, trust_env=False,
            ) as client:
                request = {
                    "query": query,
                    "user_id": user_id,
                    "limit": limit,
                    "score_debug": True,
                }
                if filters:
                    request["filters"] = dict(filters)
                response = client.post(
                    "/search", json=request, headers=self._service_headers()
                )
                response.raise_for_status()
                payload = response.json()
        except httpx.HTTPStatusError as exc:
            raise Mem0Unavailable(_http_failure_message(exc)) from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise Mem0Unavailable(f"本地 Mem0 服务不可用：{exc}") from exc
        raw = payload.get("results", payload) if isinstance(payload, dict) else payload
        if not isinstance(raw, list):
            raise Mem0Unavailable("本地 Mem0 返回了无法识别的召回格式")
        results: list[dict[str, Any]] = []
        for item in raw[:limit]:
            if not isinstance(item, dict):
                continue
            metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
            breakdown = (
                item.get("score_details")
                or item.get("score_breakdown")
                or item.get("score_debug")
                or {}
            )
            results.append({
                "id": str(item.get("id") or ""),
                "memory": str(item.get("memory", item.get("data", ""))),
                "score": float(item.get("score") or 0.0),
                "created_at": item.get("created_at"),
                "updated_at": item.get("updated_at"),
                "score_debug": breakdown,
                "metadata": metadata,
                "memory_type": item.get("memory_type") or item.get("type"),
                "source": item.get("source") or metadata.get("source") or "mem0",
            })
        return results, (time.perf_counter() - started) * 1000

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        try:
            with httpx.Client(base_url=self.base_url, timeout=self.timeout, trust_env=False) as client:
                response = client.request(
                    method, path, json=json, params=params, headers=self._service_headers()
                )
                response.raise_for_status()
                if response.status_code == 204:
                    return None
                return response.json()
        except httpx.HTTPStatusError as exc:
            raise Mem0Unavailable(_http_failure_message(exc)) from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise Mem0Unavailable(f"本地 Mem0 服务不可用：{exc}") from exc

    def _service_headers(self) -> dict[str, str]:
        return {"X-Mem0-Service-Token": self.service_token} if self.service_token else {}


def derive_mem0_service_token(explicit: str, jwt_secret: str, field_enc_key: str = "") -> str:
    if explicit.strip():
        return explicit.strip()
    material = field_enc_key.strip() or jwt_secret.strip()
    return hashlib.sha256(f"ai-customer-service:mem0:{material}".encode("utf-8")).hexdigest()
