from io import BytesIO

from docx import Document
from pypdf import PdfReader


def extract_pdf_text(data: bytes, max_chars: int = 120_000) -> str:
    reader = PdfReader(BytesIO(data))
    parts: list[str] = []
    for page in reader.pages:
        t = page.extract_text() or ""
        parts.append(t)
    text = "\n\n".join(parts).strip()
    if len(text) > max_chars:
        return text[:max_chars] + "\n\n[... truncated ...]"
    return text


def extract_docx_text(data: bytes, max_chars: int = 120_000) -> str:
    doc = Document(BytesIO(data))
    parts = [p.text for p in doc.paragraphs if p.text.strip()]
    text = "\n".join(parts).strip()
    if len(text) > max_chars:
        return text[:max_chars] + "\n\n[... truncated ...]"
    return text
