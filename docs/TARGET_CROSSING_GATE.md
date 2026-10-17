# Candidate-Triggered Target Crossing Gate

The optional configured YOLOX model now verifies candidate target trajectories
before automatic clock fitting or chip pairing. The finish-line strip remains
the cheap trigger and the original frame/time reference. The model does not
run on every full video frame or create arrivals for arbitrary visible people.

## States

- `confirmed`: a same-class target track has its box-bottom-centre anchor on
  opposite sides of a finite part of the drawn polyline, beyond a jitter band.
- `not_crossed`: the available track does not demonstrate this side change.
  No arrival item is emitted. Diagnostics retain the candidate and reason.
- `unverified`: missing context, clipping, ambiguous association or model
  failure. Original evidence remains a `line_activity` item, not an observed
  crossing and not an automatic chip match.

Pedestrians and vehicles can be verified crossings but cannot automatically
become bicycle finishers or supply chip/video clock-fit samples. Bicycle
classification alone does not establish rider identity or race eligibility.
Operator confirmation remains necessary for disputed cases.

## Context And Caching

Three frames around a line-triggered event are examined (-600, 0, +600 ms).
Neighbours may cross sealed short-file boundaries, but must share the recording
session, line digest and decoded dimensions. Excluded footage cannot supply
context. Decoders are opened one at a time; model inference uses one thread.

Cache keys include model content, source-file stamps and sampling geometry.
Stable unverified tracks are cached too, avoiding repeated expensive inference.
Missing future neighbours and model failures remain retryable.

## Limits And Acceptance

This is a conservative candidate gate, not front-wheel measurement, a full
multi-object tracker or an all-riders guarantee. Fast motion, overlapping
targets, missed detections or clipped feet/wheels can remain unverified. The
strip frame keeps its original timestamp; box-anchor interpolation never
replaces the official chip time or the operator's precise crossing frame.

Without a configured model the existing strip-only mode remains available;
`crossing_verification.state` explicitly reports `not_configured`. That mode
does not provide strict target-trajectory verification.

Acceptance needs new footage with visible feet/wheels, a ground-aligned line,
same-side movement, real crossings, stopping/touching the line, reverse motion,
vehicles and overlapping riders. Hotel footage replay alone is not a race
readiness or sustained-throughput sign-off. Compare throughput only within
the same decoded resolution, frame rate, sampling configuration and load.
