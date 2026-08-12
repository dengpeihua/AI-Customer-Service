"""Application service used by the authenticated memory workbench API."""
from __future__ import annotations

from functools import lru_cache
from typing import Any

from app.config import settings
from app.memory.locomo import LoCoMoRepository
from app.memory.mem0_gateway import Mem0Gateway, Mem0Unavailable, derive_mem0_service_token


class MemoryWorkbenchService:
    def __init__(self, repository: LoCoMoRepository, gateway: Mem0Gateway):
        self.repository = repository
        self.gateway = gateway

    def load(self, case_index: int, session_index: int, question_index: int,
             preview_limit: int) -> dict[str, Any]:
        payload = self.repository.workbench(
            case_index, session_index, question_index, preview_limit
        )
        health = self.gateway.health()
        payload["mem0"] = {
            "status": health.get("status", "offline"),
            "mode": health.get("mode", "local-oss"),
            "version": health.get("mem0_version", ""),
            "collection": health.get("collection", ""),
            "live": health.get("status") == "ok",
        }
        return payload

    def recall(self, *, case_index: int, question_index: int, query: str,
               limit: int, session_index: int = 1,
               across_sessions: bool = True) -> dict[str, Any]:
        case = self.repository.case(case_index)
        questions = list(case.get("qa") or [])
        if question_index < 0 or question_index >= len(questions):
            raise IndexError(f"LoCoMo 问题索引越界：{question_index}")
        selected = questions[question_index]
        resolved_query = query.strip() or str(selected.get("question") or "")
        if not resolved_query:
            raise ValueError("召回问题不能为空")
        user_id = self.repository.user_id(case_index, question_index)
        snapshot = self.repository.snapshot(case_index, question_index)
        live = True
        warning = ""
        filters = None if across_sessions else {
            "benchmark_timestamp": self.repository.session_timestamp(case_index, session_index)
        }
        try:
            results, latency_ms = self.gateway.search(
                resolved_query, user_id=user_id, limit=limit, filters=filters
            )
        except Mem0Unavailable as exc:
            same_question = resolved_query == str(selected.get("question") or "").strip()
            saved = ((snapshot or {}).get("retrieval") or {}).get("search_results") or []
            if not same_question or not saved:
                raise
            live = False
            warning = f"{exc}；当前明确展示该问题已有的 Mem0 评测快照。"
            results = list(saved)[:limit]
            latency_ms = float(((snapshot or {}).get("retrieval") or {}).get("search_latency_ms") or 0.0)
        answer = self.repository.generated_answer(snapshot, limit)
        if not answer:
            answer = f"Mem0 已召回 {len(results)} 条相关长期记忆，请在右侧核对内容与分数。"
        return {
            "query": resolved_query,
            "answer": answer,
            "ground_truth_answer": str(selected.get("answer") or ""),
            "evidence": list(selected.get("evidence") or []),
            "category": str(selected.get("category") or ""),
            "results": results,
            "result_count": len(results),
            "limit": limit,
            "latency_ms": round(latency_ms, 1),
            "engine": "Mem0 OSS Memory.search",
            "mode": "live" if live else "snapshot",
            "live": live,
            "scope": "cross_session" if across_sessions else "single_session",
            "session_index": session_index,
            "warning": warning,
            "stages": [
                {"key": "record", "label": "记录消息", "status": "complete"},
                {"key": "recall", "label": "召回记忆", "status": "complete"},
                {"key": "answer", "label": "模型回复", "status": "complete"},
                {"key": "save", "label": "保存回复", "status": "readonly"},
                {"key": "settle", "label": "沉淀记忆", "status": "complete"},
            ],
        }


@lru_cache(maxsize=1)
def get_memory_workbench_service() -> MemoryWorkbenchService:
    return MemoryWorkbenchService(
        LoCoMoRepository.from_project(),
        Mem0Gateway(
            settings.mem0_base_url,
            timeout=settings.mem0_timeout_seconds,
            service_token=derive_mem0_service_token(
                settings.mem0_service_token, settings.jwt_secret, settings.field_enc_key
            ),
        ),
    )
