from datetime import datetime

from sqlalchemy import (
    BLOB,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(256), nullable=False)


class MasterPricingVersion(Base):
    __tablename__ = "master_pricing_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    label: Mapped[str] = mapped_column(String(256), default="Master pricing")
    parent_version_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("master_pricing_versions.id"), nullable=True
    )
    created_reason: Mapped[str] = mapped_column(
        String(32), nullable=False
    )  # upload | finalize_edit
    file_blob: Mapped[bytes] = mapped_column(BLOB, nullable=False)
    original_filename: Mapped[str] = mapped_column(String(512), default="master.xlsx")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    display_code: Mapped[str] = mapped_column(String(64), default="")
    notes_text: Mapped[str] = mapped_column(Text, default="")
    selected_master_version_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("master_pricing_versions.id"), nullable=True
    )
    master_editor_grid_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    master_baseline_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    master_editor_source_version_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("master_pricing_versions.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    user: Mapped["User"] = relationship()
    photos: Mapped[list["ProjectPhoto"]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )
    files: Mapped[list["ProjectFile"]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )
    outputs: Mapped[list["GeneratedOutput"]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )


class ProjectPhoto(Base):
    __tablename__ = "project_photos"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(Integer, ForeignKey("projects.id"), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    mime: Mapped[str] = mapped_column(String(128), nullable=False)
    data: Mapped[bytes] = mapped_column(BLOB, nullable=False)

    project: Mapped["Project"] = relationship(back_populates="photos")


class ProjectFile(Base):
    __tablename__ = "project_files"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(Integer, ForeignKey("projects.id"), nullable=False)
    kind: Mapped[str] = mapped_column(
        String(32), nullable=False
    )  # iroof | notes_pdf | notes_docx
    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    mime: Mapped[str] = mapped_column(String(128), nullable=False)
    data: Mapped[bytes] = mapped_column(BLOB, nullable=False)

    project: Mapped["Project"] = relationship(back_populates="files")


class GeneratedOutput(Base):
    __tablename__ = "generated_outputs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(Integer, ForeignKey("projects.id"), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="pending"
    )  # pending | running | completed | failed
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    json_artifacts: Mapped[str | None] = mapped_column(Text, nullable=True)
    xlsx_blob: Mapped[bytes | None] = mapped_column(BLOB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    project: Mapped["Project"] = relationship(back_populates="outputs")
