"""Shared configuration model for the FinishReview application."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class FinishReviewSettings:
    source: str
    output_dir: Path
    passage_host: str
    passage_port: int
    camera_index: int
    secondary_source: str = ""
    high_speed_dir: Path | None = None
    finishreview_ip: str = "192.168.50.10"
    cyclerace_ip: str = "192.168.50.20"
    high_speed_pc_ip: str = "192.168.50.30"
    switch_ip: str = "192.168.50.2"
    timing_provider: str = "cyclerace"
    racetiger_base_url: str = ""
    racetiger_pc: str = ""
    racetiger_rid: str = ""
    racetiger_token: str = ""
    racetiger_poll_interval_seconds: float = 2.0


__all__ = ["FinishReviewSettings"]
