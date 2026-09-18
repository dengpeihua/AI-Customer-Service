"""Read-only access to the repository's LoCoMo dataset and Mem0 run artifacts."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
import json
from pathlib import Path
import re
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
_SESSION_RE = re.compile(r"^session_(\d+)$")
_CATEGORY_NAMES = {1: "single-hop", 2: "temporal", 3: "open-domain", 4: "multi-hop"}


@lru_cache(maxsize=128)
def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


@dataclass(frozen=True)
class LoCoMoRepository:
    dataset_path: Path
    results_dir: Path

    @classmethod
    def from_project(cls) -> "LoCoMoRepository":
        project = "predicted_mem0-v2017-locomo-clean"
        results_dir = PROJECT_ROOT / "mem" / "data" / "results" / "locomo" / project
        return cls(PROJECT_ROOT / "dataset" / "locomo10.json", results_dir)

    def cases(self) -> list[dict[str, Any]]:
        data = _read_json(self.dataset_path)
        if not isinstance(data, list) or not data:
            raise RuntimeError(f"LoCoMo 数据集格式无效：{self.dataset_path}")
        return data

    def case(self, case_index: int) -> dict[str, Any]:
        cases = self.cases()
        if case_index < 0 or case_index >= len(cases):
            raise IndexError(f"LoCoMo 案例索引越界：{case_index}")
        return cases[case_index]

    @staticmethod
    def _sessions(case: dict[str, Any]) -> list[dict[str, Any]]:
        conversation = case.get("conversation") or {}
        sessions: list[dict[str, Any]] = []
        for key, messages in conversation.items():
            match = _SESSION_RE.match(key)
            if not match or not isinstance(messages, list):
                continue
            index = int(match.group(1))
            sessions.append({
                "index": index,
                "date_time": str(conversation.get(f"session_{index}_date_time") or ""),
                "message_count": len(messages),
                "messages": messages,
            })
        return sorted(sessions, key=lambda item: item["index"])

    def snapshot(self, case_index: int, question_index: int) -> dict[str, Any] | None:
        path = self.results_dir / f"conv{case_index}_q{question_index}.json"
        return _read_json(path) if path.exists() else None

    def ingestion(self, case_index: int) -> dict[str, Any]:
        for name in (f"_ingestion_{case_index}.json", f"_progress_{case_index}.json"):
            path = self.results_dir / name
            if path.exists():
                return _read_json(path)
        # A completed question artifact also records the exact Mem0 user scope.
        first = self.results_dir / f"conv{case_index}_q0.json"
        return _read_json(first) if first.exists() else {}

    def user_id(self, case_index: int, question_index: int = 0) -> str:
        ingestion = self.ingestion(case_index)
        value = ingestion.get("user_id")
        if value:
            return str(value)
        snapshot = self.snapshot(case_index, question_index) or {}
        if snapshot.get("user_id"):
            return str(snapshot["user_id"])
        # A GitHub source install intentionally starts without the ignored benchmark
        # result directory. Keep the public dataset workbench usable with an empty,
        # deterministic Mem0 scope until a real benchmark ingestion creates its own ID.
        return f"locomo_{case_index}_source"

    def session_timestamp(self, case_index: int, session_index: int) -> int:
        session = next((item for item in self._sessions(self.case(case_index))
                        if item["index"] == int(session_index)), None)
        if session is None:
            raise IndexError(f"LoCoMo 会话索引越界：{session_index}")
        raw = str(session.get("date_time") or "")
        for fmt in ("%I:%M %p on %d %B, %Y", "%I:%M %p on %d %b, %Y"):
            try:
                return int(datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc).timestamp())
            except ValueError:
                continue
        raise RuntimeError(f"无法解析 LoCoMo 会话时间：{raw}")

    def workbench(self, case_index: int, session_index: int, question_index: int,
                  preview_limit: int = 8) -> dict[str, Any]:
        cases = self.cases()
        case = self.case(case_index)
        sessions = self._sessions(case)
        if not sessions:
            raise RuntimeError(f"LoCoMo 案例 {case_index + 1} 没有会话")
        selected = next((item for item in sessions if item["index"] == session_index), sessions[0])
        questions = list(case.get("qa") or [])
        if not questions:
            raise RuntimeError(f"LoCoMo 案例 {case_index + 1} 没有评测问题")
        question_index = min(max(question_index, 0), len(questions) - 1)
        question = questions[question_index]
        snapshot = self.snapshot(case_index, question_index) or {}
        retrieval = snapshot.get("retrieval") or {}
        all_messages = [message for session in sessions for message in session["messages"]]
        evidence = {
            str(item)
            for qa in questions
            for item in (qa.get("evidence") or [])
        }
        ingestion = self.ingestion(case_index)
        processed = ingestion.get("total_chunks_processed")
        if processed is None:
            processed = len(ingestion.get("completed_chunks") or [])
        conversation = case.get("conversation") or {}
        speaker_a = str(conversation.get("speaker_a") or "Speaker A")
        speaker_b = str(conversation.get("speaker_b") or "Speaker B")
        return {
            "dataset": {
                "name": "LoCoMo-10",
                "sample_id": str(case.get("sample_id") or f"locomo_{case_index}"),
                "case_index": case_index,
                "case_count": len(cases),
            },
            "cases": [
                {"index": index, "label": f"案例 {index + 1} · {item.get('sample_id', f'locomo_{index}')}"}
                for index, item in enumerate(cases)
            ],
            "speakers": {"a": speaker_a, "b": speaker_b},
            "sessions": [
                {key: item[key] for key in ("index", "date_time", "message_count")}
                for item in sessions
            ],
            "selected_session": selected["index"],
            "messages": [
                {
                    "id": str(message.get("dia_id") or ""),
                    "speaker": str(message.get("speaker") or ""),
                    "role": "user" if message.get("speaker") == speaker_a else "assistant",
                    "text": str(message.get("text") or ""),
                    "session": selected["index"],
                    "date_time": selected["date_time"],
                }
                for message in selected["messages"]
            ],
            "questions": [
                {
                    "index": index,
                    "question": str(item.get("question") or ""),
                    "answer": str(item.get("answer") or ""),
                    "category": _CATEGORY_NAMES.get(item.get("category"), str(item.get("category") or "")),
                    "evidence": list(item.get("evidence") or []),
                }
                for index, item in enumerate(questions)
            ],
            "selected_question": question_index,
            "stats": {
                "events": len(all_messages),
                "evidence": len(evidence),
                "worker_tasks": int(processed or len(sessions)),
                "long_term_memories": int(retrieval.get("total_results") or len(retrieval.get("search_results") or [])),
            },
            "preview_results": list(retrieval.get("search_results") or [])[:preview_limit],
            "mem0_user_id": self.user_id(case_index, question_index),
        }

    @staticmethod
    def generated_answer(snapshot: dict[str, Any] | None, limit: int) -> str:
        if not snapshot:
            return ""
        cutoffs = snapshot.get("cutoff_results") or {}
        if not cutoffs:
            return ""
        available = sorted(
            (int(key.removeprefix("top_")), value)
            for key, value in cutoffs.items()
            if key.startswith("top_") and key.removeprefix("top_").isdigit()
        )
        chosen = next((value for cutoff, value in available if cutoff >= limit), available[-1][1])
        return str(chosen.get("generated_answer") or "")
