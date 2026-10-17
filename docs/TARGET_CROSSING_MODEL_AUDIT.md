# Target crossing model audit - 2026-10-17

## Scope

The earlier hotel replay had zero line candidates. It measured scanning and
publication, not real target inference. This audit instead ran the configured
local YOLOX-S ONNX model through the current candidate gate on historical race
footage. No model download or cloud inference was involved.

Three consecutive retained five-minute strips supplied original geometry, PTS
and neighbour context. The current line detector found 276 candidates across
those strips. The first 12 candidates in the middle strip were selected in time
order, without filtering by the model result. This is a small diagnostic sample,
not a random sample, a full-race replay, a blind test or a labelled accuracy set.
Existing recordings, strips and source journals were read-only; all verdicts,
reports and extracted reference frames went into new OS temporary directories.

## Findings And Repairs

1. A complete stationary spectator track could produce `not_crossed` even when
   another target was detected only in the middle frame. That discarded an
   unresolved line event. A negative verdict now requires all eligible tracks
   to span all observations; incomplete evidence stays `unverified` for review.
2. Matching checked one track against multiple boxes, but not multiple tracks
   against one box. Equally plausible many-to-one associations now fail closed
   instead of potentially joining different targets into a confirmed crossing.
3. Shared readers were keyed only by path. Replacing a video could invalidate
   observation keys while still reading from its old open capture. Readers now
   reopen when source size/mtime or requested thread count changes.
4. On a camera with a manual clock anchor, later status construction overwrote
   `model_failed` with `no_candidates`. Failure now stays visible in both bike
   and crossing status. A genuinely empty candidate set also explicitly reports
   `no_candidates` rather than claiming the model ran successfully.

The persisted verdict cache includes verification rule version 2, so old
negative/confirmed verdicts cannot bypass the revised association rules.
Regression tests reproduce the faulty behaviours before the fixes and exercise
cache-version invalidation and reader lifecycle after the fixes.

Validation after the repairs: 141 targeted regressions passed, then the full
repository suite passed all 1,262 tests. Ruff on the changed Python files and
`git diff --check` also passed. These tests verify software behaviour, not
real-world detection accuracy or field-load acceptance.

## Same-Candidate Replay

| Gate rules | Confirmed | Not crossed | Unverified |
| --- | ---: | ---: | ---: |
| Previous rules | 0 | 9 | 3 |
| Version 2 | 0 | 4 | 8 |

Five candidates previously discarded as `no_side_change` now retain their
evidence as `insufficient_track`. In several audited windows the model returned
bicycle boxes only at the middle observation while spectator boxes persisted.
Reference-frame inspection also showed a visible cyclist in one such window;
this does not by itself establish its precise crossing time or identity.

Temporary local reports:

- `finish-model-audit-x4f15hqb/audit.json`: previous-rule checks and source refs.
- `finish-model-audit-fixed-ktn3welm/audit.json`: version-2 checks and per-frame
  detections for the same 12 candidates.

The initial pass took 98.934 seconds including line detection (1.934 seconds).
The revised pass spent 93.828 seconds in candidate verification alone. These
are different timing scopes, with uncontrolled warm caches/load: they do not
prove a speedup. Neither includes the full decode/sample/publish workflow, so
neither is an end-to-end processing ratio or sustained-throughput acceptance.
Temporary reports may be removed by the OS and are not release artifacts.

## Still Unfinished

- This sample confirmed no crossings. The repairs prevent specific evidence
  loss; they do not demonstrate useful recall or race readiness.
- Audit denser, labelled rider sequences using original frames. Check whether
  the fixed -600/0/+600 ms observations, crop boundaries, occlusion and box IoU
  association explain incomplete tracks before changing those rules.
- A box-bottom-centre anchor is not the front-wheel contact point. Do not use
  it as an official finish timestamp or claim front-wheel precision.
- The remaining four negative verdicts have not been independently labelled;
  do not count them as true negatives or infer accuracy from these totals.
- Profile seeking/decoding separately from DNN calls on real candidates, then
  measure the complete live workflow under simultaneous recording, chip intake
  and review for at least 30 minutes, including backlog recovery.
- Validate pedestrians, motorcycles, reverse travel, touching/stopping at the
  line, segment boundaries and dense groups. Keep unresolved events reviewable.

No active recorder was stopped, no live UI process was restarted and no release
package was built or deployed during this audit.
