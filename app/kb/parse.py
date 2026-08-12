import io


def extract_text(source_type: str, data: bytes | str) -> str:
    st = (source_type or "").lower()
    if st == "pdf":
        from pypdf import PdfReader

        raw = data if isinstance(data, (bytes, bytearray)) else str(data).encode()
        reader = PdfReader(io.BytesIO(raw))
        return "\n".join((page.extract_text() or "") for page in reader.pages).strip()
    if st == "docx":
        import docx  # python-docx

        raw = data if isinstance(data, (bytes, bytearray)) else str(data).encode()
        document = docx.Document(io.BytesIO(raw))
        return "\n".join(p.text for p in document.paragraphs).strip()
    # txt / faq / plain
    if isinstance(data, (bytes, bytearray)):
        return data.decode("utf-8", errors="ignore").strip()
    return str(data).strip()
