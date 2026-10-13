"""Read a *copy* of a CycleRace ``race.db`` for post-race checks.

Never point this at the live database during a race: FinishReview must not
contend with timing writes. :func:`copy_race_db` copies the database (and its
WAL/SHM side files) into a working directory first, and every reader in this
module opens only that copy, read-only.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from realtime.chip_history import BEIJING_TIMEZONE
from realtime.sync_check import UpstreamPassage


SIDE_SUFFIXES = ("-wal", "-shm")


@dataclass(frozen=True, slots=True)
class GunStart:
    group_name: str
    gun_ms: int


@dataclass(frozen=True, slots=True)
class CycleRaceSnapshot:
    """Post-race facts that the live event interface does not carry today."""

    race_name: str
    stage_date: str
    distance_km: Optional[float]
    stage_type: str
    passages: tuple[UpstreamPassage, ...]
    gun_starts: tuple[GunStart, ...]
    statuses: dict[str, str] = field(default_factory=dict)
    roster: dict[str, str] = field(default_factory=dict)
    roster_chips: dict[str, str] = field(default_factory=dict)


def copy_race_db(source: str | Path, destination_dir: str | Path) -> Path:
    """Copy race.db and its side files; refuse to copy onto the source."""

    source = Path(source).expanduser().resolve()
    destination_dir = Path(destination_dir).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(f"CycleRace database not found: {source}")
    if destination_dir == source.parent:
        raise ValueError("the copy must be written outside the CycleRace race folder")
    destination_dir.mkdir(parents=True, exist_ok=True)
    target = destination_dir / source.name
    shutil.copy2(source, target)
    for suffix in SIDE_SUFFIXES:
        side = source.with_name(source.name + suffix)
        copied = target.with_name(target.name + suffix)
        if side.is_file():
            shutil.copy2(side, copied)
        elif copied.exists():
            copied.unlink()
    return target


def _connect_copy(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _epoch_ms(stage_date: str, milliseconds_of_day: int) -> int:
    day = datetime.strptime(stage_date, "%Y-%m-%d").replace(tzinfo=BEIJING_TIMEZONE)
    return int(day.timestamp() * 1000) + int(milliseconds_of_day)


def read_snapshot(copy_path: str | Path) -> CycleRaceSnapshot:
    """Normalise one stage of a copied CycleRace database."""

    path = Path(copy_path)
    connection = _connect_copy(path)
    try:
        race = connection.execute("select name from race order by id limit 1").fetchone()
        stage = connection.execute(
            "select id, date, distance_km, stage_type from stage order by id limit 1"
        ).fetchone()
        stage_id = int(stage["id"]) if stage is not None else 1
        stage_date = str(stage["date"] or "") if stage is not None else ""
        categories = {
            int(row["id"]): str(row["name"])
            for row in connection.execute("select id, name from category")
        }
        athletes = {
            int(row["id"]): (str(row["bib"] or ""), categories.get(row["category_id"], ""))
            for row in connection.execute("select id, bib, category_id from athlete")
        }
        raw_chip = {
            int(row["id"]): str(row["chip_id"] or "")
            for row in connection.execute("select id, chip_id from timing_raw")
        }
        passages = []
        for row in connection.execute(
            "select id, athlete_id, passage_time_ms, selected_raw_id, is_manual, is_active "
            "from timing_passage where stage_id = ? order by id",
            (stage_id,),
        ):
            bib, group = athletes.get(int(row["athlete_id"]), ("", ""))
            passages.append(
                UpstreamPassage(
                    passage_id=int(row["id"]),
                    bib=bib,
                    passage_time_ms=int(row["passage_time_ms"]),
                    is_active=bool(row["is_active"]),
                    is_manual=bool(row["is_manual"]),
                    chip_id=raw_chip.get(int(row["selected_raw_id"] or 0), ""),
                    group_name=group,
                )
            )
        gun_starts = []
        if stage_date:
            for row in connection.execute(
                "select category_id, gun_time_ms from gun_start "
                "where stage_id = ? and is_valid = 1 order by id",
                (stage_id,),
            ):
                group = categories.get(row["category_id"], "") if row["category_id"] is not None else ""
                gun_starts.append(GunStart(group, _epoch_ms(stage_date, int(row["gun_time_ms"]))))
        statuses = {}
        for row in connection.execute(
            "select athlete_id, status from athlete_stage_status where stage_id = ?",
            (stage_id,),
        ):
            bib, _group = athletes.get(int(row["athlete_id"]), ("", ""))
            if bib:
                statuses[bib] = str(row["status"] or "")
        roster = {bib: group for bib, group in athletes.values() if bib}
        roster_chips = {}
        for row in connection.execute(
            "select chip_internal_id, bib from chip_mapping where coalesce(is_active, 1) = 1"
        ):
            roster_chips[str(row["chip_internal_id"])] = str(row["bib"] or "")
    finally:
        connection.close()
    return CycleRaceSnapshot(
        race_name=str(race["name"]) if race is not None else "",
        stage_date=stage_date,
        distance_km=float(stage["distance_km"]) if stage is not None and stage["distance_km"] else None,
        stage_type=str(stage["stage_type"] or "") if stage is not None else "",
        passages=tuple(passages),
        gun_starts=tuple(gun_starts),
        statuses=statuses,
        roster=roster,
        roster_chips=roster_chips,
    )


def snapshot_summary(snapshot: CycleRaceSnapshot) -> dict:
    return {
        "race_name": snapshot.race_name,
        "stage_date": snapshot.stage_date,
        "distance_km": snapshot.distance_km,
        "stage_type": snapshot.stage_type,
        "passages": len(snapshot.passages),
        "active_passages": sum(item.is_active for item in snapshot.passages),
        "manual_passages": sum(item.is_manual for item in snapshot.passages),
        "gun_starts": [{"group": item.group_name, "gun_ms": item.gun_ms} for item in snapshot.gun_starts],
        "statuses": len(snapshot.statuses),
        "roster": len(snapshot.roster),
    }


if __name__ == "__main__":  # pragma: no cover - manual inspection helper
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("race_db", type=Path)
    parser.add_argument("work_dir", type=Path)
    arguments = parser.parse_args()
    copied = copy_race_db(arguments.race_db, arguments.work_dir)
    print(json.dumps(snapshot_summary(read_snapshot(copied)), ensure_ascii=False, indent=2))
