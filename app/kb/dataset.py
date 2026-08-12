"""把可审阅的 JSON 数据集转换为现有知识库文档和向量索引。"""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.kb.ingest import ingest_document
from app.llm.base import LLM
from app.models.knowledge import KbDocument


class DatasetError(ValueError):
    pass


@dataclass(frozen=True)
class DatasetDocument:
    title: str
    content: str
    source_file: Path


@dataclass(frozen=True)
class DatasetImportResult:
    discovered: int
    imported: int
    skipped: int


def _qa_text(items: Any, *, where: str, skip_incomplete: bool = False) -> str:
    if not isinstance(items, list):
        raise DatasetError(f"{where}: qa/question 必须是数组")
    sections: list[str] = []
    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            raise DatasetError(f"{where}: 第 {index} 条问答必须是对象")
        question_value = item.get("question")
        answer_value = item.get("answer")
        question = "" if question_value is None else str(question_value).strip()
        answer = "" if answer_value is None else str(answer_value).strip()
        if not question or not answer:
            if skip_incomplete:
                continue
            raise DatasetError(f"{where}: 第 {index} 条问答缺少 question 或 answer")
        sections.append(f"问题：{question}\n答案：{answer}")
    if not sections:
        raise DatasetError(f"{where}: 没有完整的 question/answer 可导入")
    return "\n\n".join(sections)


def _record_to_document(record: Any, *, fallback_title: str, source_file: Path) -> DatasetDocument:
    where = f"{source_file.name}/{fallback_title}"
    if not isinstance(record, dict):
        raise DatasetError(f"{where}: 文档必须是 JSON 对象")

    title = str(record.get("title") or record.get("sample_id") or fallback_title).strip()[:300]
    content = str(record.get("content") or record.get("text") or "").strip()
    if not content and "qa" in record:
        is_locomo = "conversation" in record and (
            "session_summary" in record or "event_summary" in record
        )
        content = _qa_text(record["qa"], where=where, skip_incomplete=is_locomo)
    if not content and isinstance(record.get("question"), list):
        # 兼容 LOCoMo 风格：每个 case 的 question 数组中含 question/answer。
        content = _qa_text(record["question"], where=where, skip_incomplete=True)
    if not content and isinstance(record.get("question"), str):
        content = _qa_text([record], where=where)
    if not title or not content:
        raise DatasetError(f"{where}: 必须提供 title 以及 content/text/qa")
    return DatasetDocument(title=title, content=content, source_file=source_file)


def _payload_documents(payload: Any, source_file: Path) -> list[DatasetDocument]:
    stem = source_file.stem
    if isinstance(payload, list):
        records = payload
        return [
            _record_to_document(record, fallback_title=f"{stem}-{index}", source_file=source_file)
            for index, record in enumerate(records, start=1)
        ]
    if not isinstance(payload, dict):
        raise DatasetError(f"{source_file.name}: 顶层必须是对象或数组")

    if "documents" in payload:
        records = payload["documents"]
        if not isinstance(records, list):
            raise DatasetError(f"{source_file.name}: documents 必须是数组")
        return [
            _record_to_document(record, fallback_title=f"{stem}-{index}", source_file=source_file)
            for index, record in enumerate(records, start=1)
        ]

    if any(key in payload for key in ("content", "text", "qa")) or isinstance(
        payload.get("question"), str
    ):
        return [_record_to_document(payload, fallback_title=stem, source_file=source_file)]

    # 兼容按 case id 分组的 LOCoMo JSON，以及 {"配送": {...}, "售后": {...}}。
    documents: list[DatasetDocument] = []
    for key, record in payload.items():
        if not isinstance(record, dict):
            raise DatasetError(f"{source_file.name}/{key}: 分组值必须是对象")
        documents.append(
            _record_to_document(record, fallback_title=f"{stem}-{key}", source_file=source_file)
        )
    return documents


def load_dataset(path: str | Path) -> list[DatasetDocument]:
    root = Path(path).resolve()
    if not root.exists():
        raise DatasetError(f"数据集路径不存在：{root}")
    files = [root] if root.is_file() else sorted(root.rglob("*.json"))
    if not files:
        raise DatasetError(f"数据集路径中没有 JSON 文件：{root}")

    documents: list[DatasetDocument] = []
    for file in files:
        try:
            payload = json.loads(file.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise DatasetError(f"JSON 读取失败：{file.name}: {exc}") from exc
        documents.extend(_payload_documents(payload, file))
    if not documents:
        raise DatasetError("数据集没有可导入的文档")
    return documents


def import_dataset(
    db: Session,
    llm: LLM,
    *,
    tenant_id: int,
    documents: list[DatasetDocument],
    skip_existing_titles: bool = True,
) -> DatasetImportResult:
    existing = set(
        db.scalars(select(KbDocument.title).where(KbDocument.tenant_id == tenant_id)).all()
    )
    imported = 0
    skipped = 0
    for document in documents:
        if skip_existing_titles and document.title in existing:
            skipped += 1
            continue
        ingest_document(
            db,
            llm,
            tenant_id=tenant_id,
            title=document.title,
            source_type="json",
            content=document.content,
        )
        existing.add(document.title)
        imported += 1
    return DatasetImportResult(
        discovered=len(documents),
        imported=imported,
        skipped=skipped,
    )
