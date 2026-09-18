from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fastapi import HTTPException

from app.memory.locomo import LoCoMoRepository
from app.memory.service import MemoryWorkbenchService
from app.routers.memories import MemoryBenchmarkRecallIn, recall_memory_benchmark


class _LiveMem0:
    def health(self):
        return {
            "status": "ok", "mode": "local-oss", "mem0_version": "1.0",
            "collection": "memories_minimax_embo_01_1536",
        }

    def search(self, query: str, *, user_id: str, limit: int, filters=None):
        self.query = query
        self.user_id = user_id
        self.filters = filters
        return ([{"id": "m1", "memory": "live result", "score": 0.88}], 12.5)


class Mem0WorkbenchTests(unittest.TestCase):
    def test_project_locomo_payload_has_sessions_questions_and_optional_snapshots(self) -> None:
        repository = LoCoMoRepository.from_project()
        payload = repository.workbench(0, 1, 0, 8)

        self.assertEqual("LoCoMo-10", payload["dataset"]["name"])
        self.assertEqual(10, payload["dataset"]["case_count"])
        self.assertGreater(len(payload["messages"]), 10)
        self.assertGreater(len(payload["questions"]), 100)
        self.assertGreater(payload["stats"]["events"], len(payload["messages"]))
        self.assertLessEqual(len(payload["preview_results"]), 8)
        if repository.snapshot(0, 0):
            self.assertEqual(8, len(payload["preview_results"]))
        else:
            self.assertEqual([], payload["preview_results"])
        self.assertTrue(payload["mem0_user_id"])
        self.assertTrue(payload["mem0_user_id"].startswith("locomo_0_"))

    def test_recall_uses_live_mem0_search_and_reports_engine(self) -> None:
        gateway = _LiveMem0()
        service = MemoryWorkbenchService(LoCoMoRepository.from_project(), gateway)

        response = service.recall(
            case_index=0, question_index=0,
            query="When did Caroline go to the LGBTQ support group?", limit=8,
        )

        self.assertTrue(response["live"])
        self.assertEqual("Mem0 OSS Memory.search", response["engine"])
        self.assertEqual("live result", response["results"][0]["memory"])
        self.assertTrue(gateway.user_id.startswith("locomo_0_"))
        self.assertEqual("complete", response["stages"][1]["status"])

    def test_single_session_recall_filters_mem0_by_ingestion_timestamp(self) -> None:
        gateway = _LiveMem0()
        service = MemoryWorkbenchService(LoCoMoRepository.from_project(), gateway)

        response = service.recall(
            case_index=0, question_index=0,
            query="What happened in this session?", limit=8,
            session_index=1, across_sessions=False,
        )

        self.assertEqual("single_session", response["scope"])
        self.assertIn("benchmark_timestamp", gateway.filters)

    def test_recall_runtime_validation_failure_finishes_worker_task(self) -> None:
        registry = Mock()
        registry.start.return_value = "task-1"
        service = Mock()
        service.recall.side_effect = RuntimeError("session timestamp unavailable")

        with patch("app.routers.memories.ops_registry", registry), \
                patch("app.routers.memories.get_memory_workbench_service", return_value=service):
            with self.assertRaises(HTTPException) as raised:
                recall_memory_benchmark(
                    MemoryBenchmarkRecallIn(across_sessions=False), SimpleNamespace(tenant_id=7)
                )

        self.assertEqual(422, raised.exception.status_code)
        registry.finish.assert_called_once_with(
            "task-1", status="failed", detail="RuntimeError"
        )

    def test_unexpected_recall_failure_does_not_leave_worker_running(self) -> None:
        registry = Mock()
        registry.start.return_value = "task-2"
        service = Mock()
        service.recall.side_effect = KeyError("bad payload")

        with patch("app.routers.memories.ops_registry", registry), \
                patch("app.routers.memories.get_memory_workbench_service", return_value=service):
            with self.assertRaises(KeyError):
                recall_memory_benchmark(
                    MemoryBenchmarkRecallIn(), SimpleNamespace(tenant_id=8)
                )

        registry.finish.assert_called_once_with(
            "task-2", status="failed", detail="KeyError"
        )


if __name__ == "__main__":
    unittest.main()
