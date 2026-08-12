"""上传文件 → 提取文字 → 走摄取管线。API 与后台页共用。"""
from sqlalchemy.orm import Session

from app.config import settings
from app.kb.ingest import ingest_document
from app.kb.parse import extract_text
from app.llm.base import LLM
from app.models.knowledge import KbDocument

# 扩展名 → parse.extract_text 认识的 source_type
SUPPORTED_EXT = {"txt": "txt", "md": "txt", "pdf": "pdf", "docx": "docx"}

TITLE_MAX = 300  # KbDocument.title 是 String(300)


class KbUploadError(Exception):
    """带 HTTP 状态码的上传失败；由调用方翻成 JSON 或页面提示。"""

    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def source_type_for(filename: str) -> str:
    name = filename or ""
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    source_type = SUPPORTED_EXT.get(ext)
    if source_type is None:
        raise KbUploadError(400, f"不支持的文件类型 .{ext or '?'}，仅支持 txt / md / pdf / docx")
    return source_type


def ingest_upload(
    db: Session, llm: LLM, *, tenant_id: int, filename: str, raw: bytes, title: str | None = None
) -> KbDocument:
    source_type = source_type_for(filename)

    if len(raw) > settings.kb_upload_max_bytes:
        limit_mb = settings.kb_upload_max_bytes / 1024 / 1024
        raise KbUploadError(413, f"文件过大，上限 {limit_mb:.0f} MB")

    try:
        text = extract_text(source_type, raw)
    except KbUploadError:
        raise
    except Exception as exc:  # 损坏/伪造的文件不该冒成 500
        raise KbUploadError(400, "文件解析失败，请确认文件未损坏") from exc

    if not text.strip():
        raise KbUploadError(400, "没能从文件里读出文字（扫描件或纯图片 PDF 需要先做 OCR）")

    final_title = (title or "").strip() or (filename or "").strip() or "未命名文档"
    return ingest_document(
        db, llm, tenant_id=tenant_id, title=final_title[:TITLE_MAX],
        source_type=source_type, content=text,
    )
