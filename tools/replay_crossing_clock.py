"""Replay clock fitting on existing prototype arrival evaluations.

This validates timestamps only; it does not create native events, decisions,
or source-frame evidence. Run with --help for the input directory layout.
"""

import argparse
import json
from pathlib import Path

from realtime.crossing_clock import align_crossings, fit_crossing_clock


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path,
                        help="Prototype root containing results/eval_v2b.json, "
                             "tongxin/results/eval_full.json and helan/results/eval.json")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    results = {}
    for name, relative in (("dingbian", "results/eval_v2b.json"),
                           ("tongxin", "tongxin/results/eval_full.json"),
                           ("helan", "helan/results/eval.json")):
        data = json.loads((args.directory / relative).read_text(encoding="utf-8"))
        chips = [row["t"] for kind in ("matched", "chip_only", "no_video") for row in data.get(kind, [])]
        events = [row["rec"] for kind in ("matched", "video_only") for row in data.get(kind, [])]
        clock = fit_crossing_clock(chips, events)
        results[name] = dict(clock=clock.to_payload(), chips=len(chips), events=len(events),
                             pairs=len(align_crossings(chips, events, clock)))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(results, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
