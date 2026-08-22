"""Runtime path helpers shared by source and packaged executions."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional


def application_dir(
    *,
    frozen: Optional[bool] = None,
    executable: Optional[str] = None,
    module_file: Optional[str] = None,
) -> Path:
    """Return the directory that owns runtime files and race data."""
    if frozen is None:
        frozen = bool(getattr(sys, "frozen", False))
    if frozen:
        return Path(executable or sys.executable).expanduser().resolve().parent
    return Path(module_file or __file__).expanduser().resolve().parent.parent


def resource_dir(relative: str = "") -> Path:
    """Resolve a bundled PyInstaller resource, with a source-tree fallback."""
    bundle_root = Path(getattr(sys, "_MEIPASS", application_dir())).resolve()
    return (bundle_root / relative).resolve() if relative else bundle_root


def resolve_runtime_path(value: str | os.PathLike[str], *, base_dir: Optional[Path] = None) -> Path:
    """Resolve a CLI path relative to the executable/project directory."""
    path = Path(value).expanduser()
    if path.is_absolute():
        return path.resolve()
    return ((base_dir or application_dir()) / path).resolve()


def resolve_output_dir(value: str | os.PathLike[str], *, base_dir: Optional[Path] = None) -> Path:
    return resolve_runtime_path(value, base_dir=base_dir)


def resolve_source(value: str, *, base_dir: Optional[Path] = None) -> str:
    """Resolve local video files while preserving cameras and network streams."""
    normalized = str(value).strip()
    if not normalized or normalized.isdigit() or "://" in normalized:
        return normalized
    return str(resolve_runtime_path(normalized, base_dir=base_dir))
