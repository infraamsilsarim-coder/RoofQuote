import json
import logging
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import (
    GeneratedOutput,
    MasterPricingVersion,
    Project,
    ProjectFile,
    ProjectPhoto,
    User,
)
from app.services.estimate_workbook import build_estimate_workbook
from app.services.notes_extract import extract_docx_text, extract_pdf_text
from app.services.openrouter_client import analyze_site_photo
from app.services.pricing_grid import grid_to_xlsx_bytes, normalize_grid
from app.services.xlsx_text import xlsx_bytes_as_text

logger = logging.getLogger(__name__)

DEFAULT_ADDRESS = "1101 N D St, Sacramento, California 95811"


def _combined_notes_text(project: Project, db: Session) -> str:
    parts: list[str] = []
    if project.notes_text.strip():
        parts.append(project.notes_text.strip())
    for pf in project.files:
        if pf.kind == "notes_pdf":
            try:
                parts.append(extract_pdf_text(pf.data))
            except Exception as e:
                parts.append(f"[PDF notes extraction failed: {e}]")
        elif pf.kind == "notes_docx":
            try:
                parts.append(extract_docx_text(pf.data))
            except Exception as e:
                parts.append(f"[DOCX notes extraction failed: {e}]")
    return "\n\n---\n\n".join(parts) if parts else "(no additional notes)"


def _address_for_project(project: Project) -> str:
    t = project.notes_text.lower()
    if "address:" in project.notes_text:
        for line in project.notes_text.splitlines():
            if line.lower().strip().startswith("address:"):
                return line.split(":", 1)[1].strip()
    if len(project.notes_text.strip()) > 20 and "sacramento" in t:
        return project.notes_text.strip()[:200]
    return DEFAULT_ADDRESS


def run_generation_job(db: Session, output_id: int) -> None:
    settings = get_settings()
    if not settings.openrouter_api_key:
        _fail(db, output_id, "OPENROUTER_API_KEY is not set")
        return

    out = db.get(GeneratedOutput, output_id)
    if not out:
        return
    project = db.get(Project, out.project_id)
    if not project:
        _fail(db, output_id, "Project not found")
        return

    out.status = "running"
    db.commit()

    try:
        prompt_path = get_settings().prompt_path
        system_prompt = prompt_path.read_text(encoding="utf-8")
    except OSError as e:
        _fail(db, output_id, f"Cannot read prompt.txt: {e}")
        return

    iroof = next((f for f in project.files if f.kind == "iroof"), None)
    if not iroof:
        _fail(db, output_id, "Missing iRoof PDF")
        return
    master_vid = project.selected_master_version_id or project.master_editor_source_version_id
    if not master_vid:
        _fail(db, output_id, "No master pricing version selected")
        return
    mp = db.get(MasterPricingVersion, master_vid)
    if not mp:
        _fail(db, output_id, "Master pricing version not found")
        return

    pricing_blob = mp.file_blob
    if project.master_editor_grid_json:
        try:
            grid = json.loads(project.master_editor_grid_json)
            if isinstance(grid, list) and grid:
                grid = normalize_grid(grid)
                pricing_blob = grid_to_xlsx_bytes(grid)
        except (json.JSONDecodeError, TypeError, ValueError):
            pricing_blob = mp.file_blob

    photos = list(
        db.scalars(
            select(ProjectPhoto)
            .where(ProjectPhoto.project_id == project.id)
            .order_by(ProjectPhoto.ordinal)
        ).all()
    )
    if not photos:
        _fail(db, output_id, "Add at least one site photo")
        return

    pricing_text = xlsx_bytes_as_text(pricing_blob)
    notes_combined = _combined_notes_text(project, db)
    u = db.get(User, project.user_id)
    prepared_by = u.username if u else "Estimator"

    results: list[dict[str, Any]] = []
    for photo in photos:
        try:
            obj = analyze_site_photo(
                settings=settings,
                system_prompt=system_prompt,
                iroof_pdf_bytes=iroof.data,
                master_pricing_text=pricing_text,
                image_bytes=photo.data,
                image_mime=photo.mime,
                notes_combined=notes_combined,
                prepared_by_username=prepared_by,
            )
            results.append(obj)
        except Exception as e:
            logger.exception("OpenRouter call failed for photo %s", photo.id)
            results.append(
                {
                    "status": "model_error",
                    "notes": str(e),
                    "photo_filename": photo.filename,
                }
            )

    address = _address_for_project(project)
    try:
        xlsx_bytes = build_estimate_workbook(
            display_code=project.display_code or project.name,
            prepared_by=prepared_by,
            address=address,
            results=results,
        )
    except Exception as e:
        logger.exception("Workbook build failed")
        _fail(db, output_id, f"Excel build failed: {e}")
        return

    out = db.get(GeneratedOutput, output_id)
    if out:
        out.status = "completed"
        out.xlsx_blob = xlsx_bytes
        out.json_artifacts = json.dumps(results, ensure_ascii=False)
        out.error_message = None
        db.commit()


def _fail(db: Session, output_id: int, message: str) -> None:
    out = db.get(GeneratedOutput, output_id)
    if out:
        out.status = "failed"
        out.error_message = message
        db.commit()


def run_generation_job_async(output_id: int) -> None:
    from app.db import SessionLocal

    db = SessionLocal()
    try:
        run_generation_job(db, output_id)
    finally:
        db.close()


def validate_project_ready_for_generate(project: Project, db: Session) -> str | None:
    if not project.files or not any(f.kind == "iroof" for f in project.files):
        return "no_iroof"
    master_vid = project.selected_master_version_id or project.master_editor_source_version_id
    if not master_vid:
        return "no_master"
    photos = db.scalar(
        select(func.count()).select_from(ProjectPhoto).where(ProjectPhoto.project_id == project.id)
    )
    if not photos or photos < 1:
        return "no_photos"
    mp = db.get(MasterPricingVersion, master_vid)
    if not mp:
        return "bad_master"
    return None
