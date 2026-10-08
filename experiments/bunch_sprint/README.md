# Video-only bunch sprint experiment

This is a phase-one experiment, independent of the FinishReview application.
It inspects original video, associates person and bicycle detections, and
records rider tracks and forward crossing candidates. Chip records and expected
participant counts are not inputs or acceptance labels.

The current checkpoint is not certified rider separation. Manual per-rider
annotations, identity-switch evaluation and wider group coverage remain in
progress. `observed_candidate` means an observed candidate crossing, not human
confirmation; `tentative` requires review. Counts alone cannot establish accuracy.
The crossing anchor is the box centre, not official front-wheel finish timing.

## Inputs and commands

Run from the repository root with an environment containing NumPy and OpenCV.
Ultralytics and Torch are needed only for optional interval re-extraction.
Original recordings and existing local detector caches remain read-only.

```powershell
$env:BUNCH_SPRINT_RACE = 'C:\path\to\race-directory'
$env:BUNCH_SPRINT_REFERENCE = 'C:\path\to\finish_probe'
$env:BUNCH_SPRINT_WEIGHTS = 'C:\path\to\VideoPipe\yolo26n.pt'

python -m experiments.bunch_sprint.review
python -m experiments.bunch_sprint.review 0008 --start 48.32 --end 53.6 --step .16
python -m experiments.bunch_sprint.run
python -m experiments.bunch_sprint.run --group G08B
python -m unittest -v experiments.bunch_sprint.test_tracker
python -m compileall -q experiments/bunch_sprint
python -m ruff check experiments/bunch_sprint
```

The race directory must contain `video_timeline.jsonl` and the referenced
camera-01 recordings. The reference directory must contain trusted local
`yolo_cache/{video_name}.yolo26n.1280.0.pickle` and `.1.pickle` files for
person and bicycle detections respectively. Never load untrusted pickle inputs.
The optional extraction command processes only the requested frame interval:

```powershell
python -m experiments.bunch_sprint.detect_interval 0008 1208 1340 --imgsz 1920
python -m experiments.bunch_sprint.run --group G08B --highres artifacts/bunch_sprint/0008_1208_1340_yolo26n_1920.json
```

Six bounded intervals are defined in `run.py`, including three development and
three held-out clips. These are not exhaustive race coverage. Generated images,
detection results and track JSON stay under ignored `artifacts/bunch_sprint/`.
No media, model weights or private task handoff are part of the source checkpoint.

## Checkpoint validation

Synthetic tests cover simultaneous crossings, reverse flow, repeated line
crossings, detection-gap uncertainty, merged bicycle evidence and nested person
duplicates. They verify counting invariants, not real-video completeness.
Running all six intervals is an execution smoke check; human per-rider video
review is still required before claiming accuracy or integrating the feature.
