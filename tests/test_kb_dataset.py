from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from app.kb.dataset import DatasetError, load_dataset


class KnowledgeDatasetTests(unittest.TestCase):
    def test_loads_documents_and_qa_with_utf8_bom(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "knowledge.json"
            payload = {
                "documents": [
                    {"title": "配送", "content": "48 小时内发货。"},
                    {
                        "title": "售后",
                        "qa": [{"question": "可以退货吗？", "answer": "按已审核政策处理。"}],
                    },
                ]
            }
            path.write_text("\ufeff" + json.dumps(payload, ensure_ascii=False), encoding="utf-8")

            documents = load_dataset(path)

            self.assertEqual(["配送", "售后"], [doc.title for doc in documents])
            self.assertIn("问题：可以退货吗？", documents[1].content)
            self.assertIn("答案：按已审核政策处理。", documents[1].content)

    def test_loads_locomo_style_question_groups(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "locomo.json"
            path.write_text(
                json.dumps(
                    {
                        "0": {
                            "conversation": [],
                            "session_summary": [],
                            "question": [
                                {"question": "营业时间？", "answer": "每天九点到十八点。", "evidence": []},
                                {"question": "未标注的评测题", "answer": None},
                            ],
                        }
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            documents = load_dataset(path)

            self.assertEqual(1, len(documents))
            self.assertEqual("locomo-0", documents[0].title)
            self.assertNotIn("conversation", documents[0].content)

    def test_loads_original_locomo_list_with_qa(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "locomo.json"
            path.write_text(
                json.dumps(
                    [
                        {
                            "sample_id": "locomo_0",
                            "conversation": {},
                            "session_summary": {},
                            "qa": [
                                {"question": "已标注？", "answer": "是"},
                                {"question": "未标注？", "answer": None},
                            ],
                        }
                    ],
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            documents = load_dataset(path)

            self.assertEqual("locomo_0", documents[0].title)
            self.assertIn("答案：是", documents[0].content)
            self.assertNotIn("未标注", documents[0].content)

    def test_rejects_qa_without_answer(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.json"
            path.write_text(
                json.dumps({"title": "坏数据", "qa": [{"question": "只有问题"}]}, ensure_ascii=False),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(DatasetError, "缺少 question 或 answer"):
                load_dataset(path)


if __name__ == "__main__":
    unittest.main()
