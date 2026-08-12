"""
Unified Result Schema
=====================

Pydantic models defining the canonical output format for all benchmarks.
Matches what the EvalsManager TypeScript frontend expects.
"""

# 中文导读：三套 benchmark 共用这组 Pydantic 模型，保证检索、生成、评分和
# 运行元数据字段一致。一个 EvalItem 对应一道题；UnifiedResult 对应一次完整运行。

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class RetrievalData(BaseModel):
    """Retrieval results for a single eval item."""

    search_query: str = ""
    search_results: list[dict[str, Any]] = Field(default_factory=list)
    search_latency_ms: float = 0.0
    total_results: int = 0
    query_debug: dict[str, Any] | None = None


class GenerationData(BaseModel):
    """Answer generation data."""

    generated_answer: str = ""
    model: str = ""
    prompt_tokens: int | None = None
    completion_tokens: int | None = None


class NuggetScore(BaseModel):
    """A single rubric nugget evaluation (BEAM-style)."""

    nugget: str = ""
    score: float = 0.0
    reason: str = ""


class JudgmentData(BaseModel):
    """Judgment/evaluation result for a single item."""

    judgment: str = ""  # CORRECT/WRONG, PASS/FAIL, or numeric score
    score: float = 0.0
    reason: str = ""
    model: str = ""
    nugget_scores: list[NuggetScore] | None = None


class CutoffResult(BaseModel):
    """Results at a specific top-k cutoff."""

    # 同一批检索结果可截成 top-10/top-50 等多种上下文分别回答和评分，无需重新
    # 导入记忆；这样可以隔离“召回数量”对质量、token 和延迟的影响。

    judgment: str = ""
    score: float = 0.0
    generated_answer: str = ""
    memories_evaluated: int = 0
    reason: str = ""
    nugget_scores: list[NuggetScore] | None = None
    error: str | None = None


class EvalItem(BaseModel):
    """A single evaluation item (one question)."""

    id: str
    group: str = ""
    question: str = ""
    ground_truth: str = ""
    retrieval: RetrievalData | None = None
    generation: GenerationData | None = None
    judgment: JudgmentData | None = None
    cutoff_results: dict[str, CutoffResult] | None = None
    # Benchmark-specific extras
    extras: dict[str, Any] = Field(default_factory=dict)


class GroupMetrics(BaseModel):
    """Metrics for a single group/category."""

    group_name: str
    total: int = 0
    correct: int = 0
    accuracy: float = 0.0
    avg_score: float = 0.0


class CutoffMetrics(BaseModel):
    """Metrics at a specific top-k cutoff."""

    cutoff: str
    overall: dict[str, Any] = Field(default_factory=dict)
    by_group: dict[str, GroupMetrics] = Field(default_factory=dict)


class Metrics(BaseModel):
    """Aggregate metrics for a run."""

    overall_accuracy: float = 0.0
    overall_avg_score: float = 0.0
    total: int = 0
    correct: int = 0
    errors: int = 0
    by_group: dict[str, GroupMetrics] = Field(default_factory=dict)
    by_cutoff: dict[str, CutoffMetrics] = Field(default_factory=dict)


class Metadata(BaseModel):
    """Run metadata."""

    # 复现实验至少需要 benchmark、模型、project/run ID、top-k 与时间；额外配置
    # 放入 config，避免只留下一个无法追溯的最终百分数。

    benchmark: str = ""
    project_name: str = ""
    run_id: str = ""
    timestamp: str = Field(default_factory=lambda: datetime.now().isoformat())
    answerer_model: str = ""
    judge_model: str = ""
    provider: str = ""
    top_k: int = 200
    top_k_cutoffs: list[str] = Field(default_factory=list)
    total_questions: int = 0
    config: dict[str, Any] = Field(default_factory=dict)


class UnifiedResult(BaseModel):
    """Top-level result container matching the TypeScript frontend schema."""

    schema_version: str = "1.0"
    metadata: Metadata = Field(default_factory=Metadata)
    metrics: Metrics = Field(default_factory=Metrics)
    evaluations: list[EvalItem] = Field(default_factory=list)

    def to_file(self, path: str) -> None:
        """Write result to a JSON file."""
        import json
        from pathlib import Path

        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(
            json.dumps(self.model_dump(), indent=2, ensure_ascii=False, default=str)
        )
