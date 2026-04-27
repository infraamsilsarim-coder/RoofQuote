"""Persist project file inputs from a multipart (or mixed) form."""

from __future__ import annotations

from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.models import Project, ProjectFile, ProjectPhoto

MAX_PHOTOS = 25


async def persist_project_uploads_from_form(
    db: Session, project: Project, form
) -> str | None:
    """
    Update project from Starlette/FastAPI FormData (same field names as legacy /save).
    Commits on success. Returns FLASH_ERR key on validation failure, else None.
    """
    project.notes_text = str(form.get("notes_text") or "")
    project.display_code = str(form.get("display_code") or "").strip()
    mid = str(form.get("master_version_id") or "").strip()
    if mid.isdigit():
        project.selected_master_version_id = int(mid)
    elif not mid:
        project.selected_master_version_id = None

    notes_doc = form.get("notes_doc")
    if notes_doc is not None and hasattr(notes_doc, "filename") and notes_doc.filename:
        fn = str(notes_doc.filename).lower()
        if not (fn.endswith(".docx") or fn.endswith(".doc")):
            return "notes_doc_format"
        if fn.endswith(".doc") and not fn.endswith(".docx"):
            return "doc_legacy"

    iroof_pdf = form.get("iroof_pdf")
    if iroof_pdf is not None and hasattr(iroof_pdf, "filename") and iroof_pdf.filename:
        data = await iroof_pdf.read()
        db.execute(
            delete(ProjectFile).where(
                ProjectFile.project_id == project.id,
                ProjectFile.kind == "iroof",
            )
        )
        db.add(
            ProjectFile(
                project_id=project.id,
                kind="iroof",
                filename=str(iroof_pdf.filename)[:500],
                mime=getattr(iroof_pdf, "content_type", None) or "application/pdf",
                data=data,
            )
        )

    notes_pdf = form.get("notes_pdf")
    if notes_pdf is not None and hasattr(notes_pdf, "filename") and notes_pdf.filename:
        data = await notes_pdf.read()
        db.execute(
            delete(ProjectFile).where(
                ProjectFile.project_id == project.id,
                ProjectFile.kind == "notes_pdf",
            )
        )
        db.add(
            ProjectFile(
                project_id=project.id,
                kind="notes_pdf",
                filename=str(notes_pdf.filename)[:500],
                mime=getattr(notes_pdf, "content_type", None) or "application/pdf",
                data=data,
            )
        )

    if notes_doc is not None and hasattr(notes_doc, "filename") and notes_doc.filename:
        data = await notes_doc.read()
        db.execute(
            delete(ProjectFile).where(
                ProjectFile.project_id == project.id,
                ProjectFile.kind == "notes_docx",
            )
        )
        db.add(
            ProjectFile(
                project_id=project.id,
                kind="notes_docx",
                filename=str(notes_doc.filename)[:500],
                mime=getattr(notes_doc, "content_type", None)
                or "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                data=data,
            )
        )

    photo_list = form.getlist("photos")
    if photo_list:
        db.execute(delete(ProjectPhoto).where(ProjectPhoto.project_id == project.id))
        count = 0
        for p in photo_list:
            if not hasattr(p, "filename") or not p.filename or count >= MAX_PHOTOS:
                continue
            data = await p.read()
            db.add(
                ProjectPhoto(
                    project_id=project.id,
                    ordinal=count,
                    filename=str(p.filename)[:500],
                    mime=getattr(p, "content_type", None) or "image/jpeg",
                    data=data,
                )
            )
            count += 1

    db.commit()
    db.expire(project)
    return None
