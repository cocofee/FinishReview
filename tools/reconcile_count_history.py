"""Attach chip receipt history to an external count report for FinishReview."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import time

from realtime.count_history import read_jsonl, reconcile_report
from realtime.count_review import read_count_report
from realtime.durable_jsonl import append_jsonl_records


def run_once(source_path, chip_path, output_dir, *, as_of_ms=None, reviews_path=None):
    output_dir = Path(output_dir).absolute()
    target = output_dir / "count_reconciliation.json"
    if Path(source_path).absolute() == target:
        raise ValueError("input report and output report must be different files")
    source = read_count_report(source_path)
    journal = output_dir / "count_anomaly_history.jsonl"
    retained = read_jsonl(journal) if journal.exists() else []
    reviews_path = Path(reviews_path) if reviews_path else output_dir / "count_reviews.jsonl"
    reviews = read_jsonl(reviews_path) if reviews_path.exists() else []
    report, cases = reconcile_report(source, read_jsonl(chip_path),
                                    as_of_ms=as_of_ms if as_of_ms is not None else int(time.time()*1000),
                                    reviews=reviews, retained=retained)
    previous = {r["case_id"]: {k:v for k,v in r.items() if k != "observed_at_ms"} for r in retained}
    changed = [r for r in cases if r != previous.get(r["case_id"])]
    if changed:
        append_jsonl_records(journal, (json.dumps(r, ensure_ascii=False).encode()+b"\n" for r in changed),
                             description="count anomaly history")
    output_dir.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, target)
    return report


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--input", type=Path, required=True)
    ap.add_argument("--chip-journal", type=Path, required=True)
    ap.add_argument("--output-dir", type=Path, required=True)
    ap.add_argument("--reviews", type=Path)
    ap.add_argument("--as-of-ms", type=int)
    ap.add_argument("--watch", action="store_true", help="reconcile when observations, chips or reviews change")
    a = ap.parse_args()
    previous = None
    while True:
        paths = [a.input, a.chip_journal, a.reviews or a.output_dir / "count_reviews.jsonl"]
        signature = tuple((p.stat().st_mtime_ns, p.stat().st_size) if p.exists() else None for p in paths)
        if signature != previous:
            report = run_once(a.input, a.chip_journal, a.output_dir, as_of_ms=a.as_of_ms, reviews_path=a.reviews)
            print(f"Groups {len(report['groups'])}; unresolved {sum(r['status']=='unresolved' for r in report['groups'])}; "
                  f"resolved history {sum(r['status']=='resolved' for r in report['groups'])}", flush=True)
            previous = signature
        if not a.watch:
            break
        time.sleep(1)


if __name__ == "__main__":
    main()
