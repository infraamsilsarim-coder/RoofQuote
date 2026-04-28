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
from app.services.openrouter_client import analyze_site_photos_batch, analyze_site_photo
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

    # LLM calls are batched: up to 5 images per request.
    # The updated prompt returns a JSON array with one wrapper object per image:
    # { image_id, discard, discard_reason, estimate }.
    #
    # For Excel output, we keep only deficiency estimates (discard=false and estimate != null),
    # so the downstream workbook generation remains compatible with the existing deficiency schema.
    excel_results: list[dict[str, Any]] = []
    # Photo bytes keyed by DB id for embedding thumbnails in Excel.
    photos_by_id: dict[int, bytes] = {}
    artifacts: list[dict[str, Any]] = []

    batch_size = 5
    total_batches = (len(photos) + batch_size - 1) // batch_size
    logger.info(
        "generation: start output_id=%s project_id=%s photos=%s batches=%s batch_size=%s",
        output_id,
        project.id,
        len(photos),
        total_batches,
        batch_size,
    )
    for start in range(0, len(photos), batch_size):
        batch = photos[start : start + batch_size]
        batch_num = (start // batch_size) + 1
        req_photos: list[dict[str, Any]] = []
        for idx, photo in enumerate(batch, start=1):
            req_photos.append(
                {
                    "image_id": f"img_{idx}",
                    "image_bytes": photo.data,
                    "image_mime": photo.mime,
                    "filename": photo.filename,
                    "photo_db_id": photo.id,
                }
            )

        try:
            wrappers = analyze_site_photos_batch(
                settings=settings,
                system_prompt=system_prompt,
                iroof_pdf_bytes=iroof.data,
                master_pricing_text=pricing_text,
                photos=req_photos,
                notes_combined=notes_combined,
                prepared_by_username=prepared_by,
            )
        except Exception as e:
            logger.exception("OpenRouter batch call failed for photos %s..%s", start, start + len(batch) - 1)
            for p in req_photos:
                artifacts.append(
                    {
                        "image_id": p.get("image_id"),
                        "discard": True,
                        "discard_reason": f"model_error: {e}",
                        "estimate": None,
                        "photo_filename": p.get("filename"),
                        "photo_db_id": p.get("photo_db_id"),
                    }
                )
            continue

        # Join model wrappers back to the underlying photos for traceability.
        for i, w in enumerate(wrappers):
            p = req_photos[i]
            wrapped = dict(w)
            wrapped.setdefault("image_id", p.get("image_id"))
            wrapped["photo_filename"] = p.get("filename")
            wrapped["photo_db_id"] = p.get("photo_db_id")
            artifacts.append(wrapped)

            discard = bool(wrapped.get("discard"))
            estimate = wrapped.get("estimate")
            if not discard and isinstance(estimate, dict) and estimate:
                # Keep the estimate schema intact, but add a private reference field for Excel thumbnail embedding.
                est = dict(estimate)
                photo_db_id = p.get("photo_db_id")
                if isinstance(photo_db_id, int):
                    est["_photo_db_id"] = photo_db_id
                    photos_by_id[photo_db_id] = p.get("image_bytes") or b""
                excel_results.append(est)

        logger.info(
            "generation: batch_done output_id=%s project_id=%s batch=%s/%s photos_in_batch=%s kept_estimates=%s total_kept=%s",
            output_id,
            project.id,
            batch_num,
            total_batches,
            len(batch),
            sum(
                1
                for w in wrappers
                if isinstance(w, dict) and (not bool(w.get("discard"))) and isinstance(w.get("estimate"), dict)
            ),
            len(excel_results),
        )

    address = _address_for_project(project)
    try:
        xlsx_bytes = build_estimate_workbook(
            display_code=project.display_code or project.name,
            prepared_by=prepared_by,
            address=address,
            results=excel_results,
            photos_by_id=photos_by_id,
        )
    except Exception as e:
        logger.exception("Workbook build failed")
        _fail(db, output_id, f"Excel build failed: {e}")
        return

    out = db.get(GeneratedOutput, output_id)
    if out:
        out.status = "completed"
        out.xlsx_blob = xlsx_bytes
        out.json_artifacts = json.dumps(artifacts, ensure_ascii=False)
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
