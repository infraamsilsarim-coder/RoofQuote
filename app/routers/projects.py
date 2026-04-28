import json
import logging
import time
from datetime import datetime
from io import BytesIO
from pathlib import Path

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    Query,
    Request,
    UploadFile,
)
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import session_user
from app.models import (
    GeneratedOutput,
    MasterPricingVersion,
    Project,
    ProjectFile,
    ProjectPhoto,
    User,
)
from app.services.generation import (
    run_generation_job_async,
    validate_project_ready_for_generate,
)
from app.services.project_persist import persist_project_uploads_from_form
from app.services.pricing_grid import (
    grid_from_flat_map,
    grid_to_xlsx_bytes,
    normalize_grid,
    stable_grid_hash,
    xlsx_first_sheet_to_grid,
)
from app.templates_env import templates

router = APIRouter(tags=["projects"])
MAX_PHOTOS = 25
logger = logging.getLogger(__name__)

# region agent log
_DBG_LOG = Path(__file__).resolve().parents[2] / "debug-e4de73.log"


def _debug_generate_log(message: str, hypothesis_id: str, data: dict) -> None:
    try:
        with open(_DBG_LOG, "a", encoding="utf-8") as f:
            f.write(
                json.dumps(
                    {
                        "sessionId": "e4de73",
                        "hypothesisId": hypothesis_id,
                        "location": "projects.py:project_generate",
                        "message": message,
                        "data": data,
                        "timestamp": int(time.time() * 1000),
                    },
                    default=str,
                )
                + "\n"
            )
    except Exception:
        pass


# endregion

FLASH_ERR = {
    "no_iroof": "Add the iRoof PDF in step 2 (or pick a file and click Generate — inputs save automatically).",
    "no_master": "Upload or load master pricing in step 3, choose a version in the dropdown, then Generate (or use Load/Upload first).",
    "no_photos": "Upload at least one site photo.",
    "bad_master": "Master pricing selection is invalid — reload and pick a version.",
    "need_xlsx": "Master pricing file must be .xlsx.",
    "bad_version": "That pricing version no longer exists.",
    "empty_grid": "Load a master pricing sheet into the editor before finalizing.",
    "notes_doc_format": "Notes attachment must be PDF or DOCX.",
    "doc_legacy": "Legacy .doc is not supported — please save as .docx.",
}


def _redirect_login():
    return RedirectResponse("/login", status_code=302)


def _get_project_for_user(
    db: Session, user: User, project_id: int
) -> Project | None:
    p = db.get(Project, project_id)
    if not p or p.user_id != user.id:
        return None
    return p


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)):
    user = session_user(request, db)
    if not user:
        return _redirect_login()
    projects = (
        db.scalars(
            select(Project)
            .where(Project.user_id == user.id)
            .order_by(Project.created_at.desc())
        )
        .all()
    )
    versions = db.scalars(select(MasterPricingVersion).order_by(MasterPricingVersion.id.desc())).all()
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "request": request,
            "user": user,
            "projects": projects,
            "master_versions": versions,
        },
    )


@router.post("/projects")
def create_project(
    request: Request,
    db: Session = Depends(get_db),
    name: str = Form(),
    display_code: str = Form(""),
):
    user = session_user(request, db)
    if not user:
        return _redirect_login()
    p = Project(
        user_id=user.id,
        name=name.strip() or "Untitled",
        display_code=(display_code or "").strip(),
    )
    db.add(p)
    db.commit()
    db.refresh(p)
    return RedirectResponse(f"/projects/{p.id}/inputs", status_code=302)


@router.post("/projects/{project_id}/delete")
def delete_project(project_id: int, request: Request, db: Session = Depends(get_db)):
    user = session_user(request, db)
    if not user:
        return _redirect_login()
    project = _get_project_for_user(db, user, project_id)
    if not project:
        return RedirectResponse("/", status_code=302)
    db.delete(project)
    db.commit()
    return RedirectResponse("/", status_code=302)


@router.get("/master-pricing", response_class=HTMLResponse)
def master_pricing_page(
    request: Request,
    db: Session = Depends(get_db),
    err: str | None = Query(None),
):
    user = session_user(request, db)
    if not user:
        return _redirect_login()
    versions = db.scalars(select(MasterPricingVersion).order_by(MasterPricingVersion.id.desc())).all()
    flash = {
        "not_found": "That version no longer exists.",
        "need_xlsx": "File must be an .xlsx workbook.",
    }.get(err or "", err)
    return templates.TemplateResponse(
        request,
        "master_pricing.html",
        {"request": request, "user": user, "versions": versions, "flash_err": flash},
    )


@router.post("/master-pricing/upload")
async def master_pricing_global_upload(
    request: Request,
    db: Session = Depends(get_db),
    file: UploadFile = File(...),
):
    user = session_user(request, db)
    if not user:
        return _redirect_login()
    if not file.filename or not file.filename.lower().endswith(".xlsx"):
        return RedirectResponse("/master-pricing?err=need_xlsx", status_code=302)
    data = await file.read()
    mv = MasterPricingVersion(
        label=file.filename[:250],
        parent_version_id=None,
        created_reason="upload",
        file_blob=data,
        original_filename=file.filename[:500],
    )
    db.add(mv)
    db.commit()
    return RedirectResponse("/master-pricing", status_code=302)


@router.post("/master-pricing/delete/{version_id}")
def delete_master_version(version_id: int, request: Request, db: Session = Depends(get_db)):
    user = session_user(request, db)
    if not user:
        return _redirect_login()
    mv = db.get(MasterPricingVersion, version_id)
    if not mv:
        return RedirectResponse("/master-pricing?err=not_found", status_code=302)
    db.execute(
        update(Project)
        .where(Project.selected_master_version_id == version_id)
        .values(selected_master_version_id=None)
    )
    db.execute(
        update(Project)
        .where(Project.master_editor_source_version_id == version_id)
        .values(master_editor_source_version_id=None)
    )
    db.execute(
        update(MasterPricingVersion)
        .where(MasterPricingVersion.parent_version_id == version_id)
        .values(parent_version_id=None)
    )
    db.delete(mv)
    db.commit()
    return RedirectResponse("/master-pricing", status_code=302)


@router.get("/master-pricing/download/{version_id}")
def download_master_version(version_id: int, request: Request, db: Session = Depends(get_db)):
    user = session_user(request, db)
    if not user:
        return _redirect_login()
    mv = db.get(MasterPricingVersion, version_id)
    if not mv:
        return RedirectResponse("/master-pricing", status_code=302)
    return StreamingResponse(
        BytesIO(mv.file_blob),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": f'attachment; filename="{mv.original_filename}"',
        },
    )


@router.get("/projects/{project_id}/photos/{photo_id}/view")
def project_photo_view(
    project_id: int,
    photo_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    user = session_user(request, db)
    if not user:
        return _redirect_login()
    project = _get_project_for_user(db, user, project_id)
    if not project:
        return RedirectResponse("/", status_code=302)
    ph = db.get(ProjectPhoto, photo_id)
    if not ph or ph.project_id != project.id:
        return RedirectResponse("/", status_code=302)
    return StreamingResponse(
        BytesIO(ph.data),
        media_type=ph.mime or "image/jpeg",
    )


@router.get("/projects/{project_id}/inputs", response_class=HTMLResponse)
def project_inputs(
    project_id: int,
    request: Request,
    db: Session = Depends(get_db),
    err: str | None = Query(None),
):
    user = session_user(request, db)
    if not user:
        return _redirect_login()
    project = _get_project_for_user(db, user, project_id)
    if not project:
        return RedirectResponse("/", status_code=302)
    photos = (
        db.scalars(
            select(ProjectPhoto)
            .where(ProjectPhoto.project_id == project.id)
            .order_by(ProjectPhoto.ordinal)
        )
        .all()
    )
    iroof = next((f for f in project.files if f.kind == "iroof"), None)
    notes_pdf = next((f for f in project.files if f.kind == "notes_pdf"), None)
    notes_docx = next((f for f in project.files if f.kind == "notes_docx"), None)
    versions = db.scalars(select(MasterPricingVersion).order_by(MasterPricingVersion.id.desc())).all()
    grid: list[list[str]] = []
    if project.master_editor_grid_json:
        try:
            raw = json.loads(project.master_editor_grid_json)
            grid = normalize_grid(raw) if isinstance(raw, list) else []
        except (json.JSONDecodeError, TypeError):
            grid = []
    nrows = len(grid)
    ncols = max((len(r) for r in grid), default=0)
    return templates.TemplateResponse(
        request,
        "project_inputs.html",
        {
            "request": request,
            "user": user,
            "project": project,
            "photos": photos,
            "iroof": iroof,
            "notes_pdf": notes_pdf,
            "notes_docx": notes_docx,
            "master_versions": versions,
            "grid": grid,
            "nrows": nrows,
            "ncols": ncols,
            "flash_err": FLASH_ERR.get(err) or err,
        },
    )


@router.post("/projects/{project_id}/save")
async def project_save(
    project_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    user = session_user(request, db)
    if not user:
        return _redirect_login()
    project = _get_project_for_user(db, user, project_id)
    if not project:
        return RedirectResponse("/", status_code=302)

    form = await request.form()
    err = await persist_project_uploads_from_form(db, project, form)
    if err:
        return RedirectResponse(f"/projects/{project_id}/inputs?err={err}", status_code=302)
    return RedirectResponse(f"/projects/{project_id}/inputs", status_code=302)


@router.post("/projects/{project_id}/master/load")
def master_load_into_editor(
    project_id: int,
    request: Request,
    db: Session = Depends(get_db),
    version_id: str = Form(""),
):
    user = session_user(request, db)
    if not user:
        return _redirect_login()
    project = _get_project_for_user(db, user, project_id)
    if not project:
        return RedirectResponse("/", status_code=302)
    if not version_id.strip().isdigit():
        return RedirectResponse(f"/projects/{project_id}/inputs?err=bad_version", status_code=302)
    mv = db.get(MasterPricingVersion, int(version_id))
    if not mv:
        return RedirectResponse(f"/projects/{project_id}/inputs?err=bad_version", status_code=302)
    grid = xlsx_first_sheet_to_grid(mv.file_blob)
    project.selected_master_version_id = mv.id
    project.master_editor_source_version_id = mv.id
    project.master_editor_grid_json = json.dumps(grid)
    project.master_baseline_hash = stable_grid_hash(grid)
    db.commit()
    return RedirectResponse(f"/projects/{project_id}/inputs", status_code=302)


@router.post("/projects/{project_id}/master/upload")
async def master_upload_for_project(
    project_id: int,
    request: Request,
    db: Session = Depends(get_db),
    file: UploadFile = File(...),
):
    user = session_user(request, db)
    if not user:
        return _redirect_login()
    project = _get_project_for_user(db, user, project_id)
    if not project:
        return RedirectResponse("/", status_code=302)
    if not file.filename or not file.filename.lower().endswith(".xlsx"):
        return RedirectResponse(f"/projects/{project_id}/inputs?err=need_xlsx", status_code=302)
    data = await file.read()
    mv = MasterPricingVersion(
        label=file.filename[:250],
        parent_version_id=None,
        created_reason="upload",
        file_blob=data,
        original_filename=file.filename[:500],
    )
    db.add(mv)
    db.flush()
    grid = xlsx_first_sheet_to_grid(data)
    project.selected_master_version_id = mv.id
    project.master_editor_source_version_id = mv.id
    project.master_editor_grid_json = json.dumps(grid)
    project.master_baseline_hash = stable_grid_hash(grid)
    db.commit()
    return RedirectResponse(f"/projects/{project_id}/inputs", status_code=302)


@router.post("/projects/{project_id}/master/finalize")
async def master_finalize(
    project_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    user = session_user(request, db)
    if not user:
        return _redirect_login()
    project = _get_project_for_user(db, user, project_id)
    if not project:
        return RedirectResponse("/", status_code=302)
    form = await request.form()
    nrows = int(form.get("nrows") or 0)
    ncols = int(form.get("ncols") or 0)
    if nrows <= 0 or ncols <= 0:
        return RedirectResponse(f"/projects/{project_id}/inputs?err=empty_grid", status_code=302)
    flat = {str(k): str(v) for k, v in form.items() if str(k).startswith("cell_")}
    grid = grid_from_flat_map(flat, nrows, ncols)
    h = stable_grid_hash(grid)
    baseline = project.master_baseline_hash or ""

    if h != baseline:
        blob = grid_to_xlsx_bytes(grid)
        parent_id = project.master_editor_source_version_id or project.selected_master_version_id
        mv = MasterPricingVersion(
            label=f"Edited {datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC",
            parent_version_id=parent_id,
            created_reason="finalize_edit",
            file_blob=blob,
            original_filename="master_pricing_edited.xlsx",
        )
        db.add(mv)
        db.flush()
        project.selected_master_version_id = mv.id
        project.master_editor_source_version_id = mv.id

    project.master_editor_grid_json = json.dumps(grid)
    project.master_baseline_hash = h
    db.commit()
    return RedirectResponse(f"/projects/{project_id}/inputs", status_code=302)


@router.get("/generated-results", response_class=HTMLResponse)
def all_generated_results(
    request: Request,
    db: Session = Depends(get_db),
):
    user = session_user(request, db)
    if not user:
        return _redirect_login()
    rows = (
        db.execute(
            select(GeneratedOutput, Project)
            .join(Project, GeneratedOutput.project_id == Project.id)
            .where(Project.user_id == user.id)
            .order_by(GeneratedOutput.created_at.desc())
        )
        .all()
    )
    results = [{"output": o, "project": p} for o, p in rows]
    return templates.TemplateResponse(
        request,
        "all_generated_results.html",
        {
            "request": request,
            "user": user,
            "results": results,
        },
    )


@router.get("/projects/{project_id}/results", response_class=HTMLResponse)
def project_results(
    project_id: int,
    request: Request,
    db: Session = Depends(get_db),
    started: str | None = Query(None),
):
    user = session_user(request, db)
    if not user:
        return _redirect_login()
    project = _get_project_for_user(db, user, project_id)
    if not project:
        return RedirectResponse("/", status_code=302)
    outputs = (
        db.scalars(
            select(GeneratedOutput)
            .where(GeneratedOutput.project_id == project.id)
            .order_by(GeneratedOutput.created_at.desc())
        )
        .all()
    )
    return templates.TemplateResponse(
        request,
        "project_results.html",
        {
            "request": request,
            "user": user,
            "project": project,
            "outputs": outputs,
            "started_id": started,
        },
    )


@router.post("/projects/{project_id}/generate")
async def project_generate(
    project_id: int,
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    user = session_user(request, db)
    if not user:
        return _redirect_login()
    project = _get_project_for_user(db, user, project_id)
    if not project:
        return RedirectResponse("/", status_code=302)
    form = await request.form()
    fk = list(form.keys())
    ir = form.get("iroof_pdf")
    _debug_generate_log(
        "after_form",
        "H1",
        {
            "project_id": project.id,
            "form_keys": fk,
            "has_iroof_key": "iroof_pdf" in fk,
            "iroof_has_filename": bool(getattr(ir, "filename", None) if ir is not None else False),
            "master_version_raw": (str(form.get("master_version_id") or ""))[:32],
        },
    )
    persist_err = await persist_project_uploads_from_form(db, project, form)
    _debug_generate_log("after_persist", "H2", {"project_id": project.id, "persist_err": persist_err})
    if persist_err:
        logger.info(
            "generate: persist failed project_id=%s user_id=%s err=%s",
            project.id,
            user.id,
            persist_err,
        )
        return RedirectResponse(f"/projects/{project_id}/inputs?err={persist_err}", status_code=302)
    db.refresh(project)
    err = validate_project_ready_for_generate(project, db)
    _debug_generate_log("after_validate", "H3", {"project_id": project.id, "validate_err": err})
    if err:
        logger.info(
            "generate: validation failed project_id=%s user_id=%s err=%s",
            project.id,
            user.id,
            err,
        )
        return RedirectResponse(f"/projects/{project_id}/inputs?err={err}", status_code=302)

    # Basic generate logging (no file contents).
    iroof_present = any((f.kind == "iroof") for f in (project.files or []))
    notes_pdf_present = any((f.kind == "notes_pdf") for f in (project.files or []))
    notes_docx_present = any((f.kind == "notes_docx") for f in (project.files or []))
    photo_count = db.scalar(
        select(func.count()).select_from(ProjectPhoto).where(ProjectPhoto.project_id == project.id)
    ) or 0
    master_vid = project.selected_master_version_id or project.master_editor_source_version_id
    batch_size = 5
    batch_count = (photo_count + batch_size - 1) // batch_size if photo_count else 0
    logger.info(
        "generate: queued project_id=%s user_id=%s photos=%s batches=%s iroof=%s master_version_id=%s notes_text_len=%s notes_pdf=%s notes_docx=%s",
        project.id,
        user.id,
        photo_count,
        batch_count,
        iroof_present,
        master_vid,
        len(project.notes_text or ""),
        notes_pdf_present,
        notes_docx_present,
    )
    out = GeneratedOutput(project_id=project.id, status="pending")
    db.add(out)
    db.commit()
    db.refresh(out)
    background_tasks.add_task(run_generation_job_async, out.id)
    return RedirectResponse(f"/projects/{project_id}/results?started={out.id}", status_code=302)


@router.get("/projects/{project_id}/outputs/{output_id}/status", response_class=HTMLResponse)
def output_status_fragment(
    project_id: int,
    output_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    user = session_user(request, db)
    if not user:
        return HTMLResponse("")
    project = _get_project_for_user(db, user, project_id)
    if not project:
        return HTMLResponse("")
    out = db.get(GeneratedOutput, output_id)
    if not out or out.project_id != project.id:
        return HTMLResponse("")
    progress = None
    try:
        if out.json_artifacts:
            payload = json.loads(out.json_artifacts)
            if isinstance(payload, dict):
                progress = payload.get("progress")
    except Exception:
        progress = None
    return templates.TemplateResponse(
        request,
        "partials/output_status.html",
        {"request": request, "out": out, "progress": progress},
    )


@router.get("/outputs/{output_id}/download")
def download_output(
    output_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    user = session_user(request, db)
    if not user:
        return _redirect_login()
    out = db.get(GeneratedOutput, output_id)
    if not out:
        return RedirectResponse("/", status_code=302)
    project = db.get(Project, out.project_id)
    if not project or project.user_id != user.id:
        return RedirectResponse("/", status_code=302)
    if out.status != "completed" or not out.xlsx_blob:
        return RedirectResponse(f"/projects/{project.id}/results", status_code=302)
    name = f"estimate_{project.name.replace(' ', '_')}_{out.id}.xlsx"
    return StreamingResponse(
        BytesIO(out.xlsx_blob),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )
