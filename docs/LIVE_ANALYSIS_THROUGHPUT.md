# Live analysis throughput repair

## Implemented

- Live/settle analysis publishes cumulative results after roughly 6 seconds of
  new sealed video. It no longer samples the entire snapshot before publishing.
  The last batch may be smaller; a single long archive is still indivisible.
- Existing current strips are reused, including those beyond an earlier cache
  hole. Pending ranges remain explicit and the headline cannot jump over them.
  A chip in such a range waits instead of being called missing video.
- Neighbour strips are retained for boundary detection. When a new neighbour
  appears, its signature invalidates the affected detection cache.
- Intermediate settle batches retain a chip/evidence delay. Only the final
  batch removes that delay; stopping recording is not permission to declare
  still-unprocessed video absent.
- The worker shares a detector, two lazy frame readers and up to 256 cropped
  detection observations across batches. Identical crops/PTS/source stamps
  reuse detections. Metadata and line-detection caches reduce repeated NPZ/JSON
  work; changing a source or neighbour invalidates the applicable entry.
- Chip revisions and first-rider calibration are reloaded between batches.
- The GUI refreshes on publication, not only process exit. Successful workers
  have a 0.25-second scheduling delay instead of 10 seconds; failures back off
  for 5 seconds. Existing recorder/CPU/memory protection is unchanged.

## Native-resolution ROI decoder

For unrotated TS/MKV/MP4/MOV files, when bundled/system FFmpeg is available:

1. Probe a real decoded frame for dimensions, first PTS and optional paint snap.
2. Decode the original stream with bounded threads; crop a padded, chroma-aligned
   native-resolution rectangle around the full configured polyline **before**
   conversion to BGR and transfer to Python.
3. Sample the same original-coordinate maps. No downscaling, frame skipping,
   interpolation of frames or original-file changes are involved.
4. Parse integer decoder PTS with its actual time base, not nominal frame rate.
   Validate frame/timestamp counts, strict PTS order and known probe frame count.
5. On invalid timing, an unsupported/large ROI, rotation or decoder failure,
   use the original full-frame decoder. Cancellation tears down the helper.

The decoder still decodes H.264; it does not claim compressed ROI-only decoding.
The saving is reduced full-frame colour conversion/copying and a different
decoder path. Small colour-conversion differences are possible. Tests compare
frame counts, real PTS, geometry and sampled pixels against full-frame decoding.
No asynchronous recorder is stopped or reconfigured.

## Measurements and limitations

On a retained 4096x2160/25fps hotel clip with 50 frames, decoder PTS agreed with
OpenCV (including a non-uniform 38ms interval). Mean sampled channel difference
was 0.924/255, maximum 5/255. One initial full-frame sample took about 4 seconds,
versus about 1 second for the ROI helper. Later idle-machine full-frame readings
were also near 1 second: **do not claim a sustained fourfold speedup** from these
different load states.

A controlled 16-short-file replay covered 33.952 seconds in 22.382 seconds,
published five times and first published at 4.221 seconds. It used a diagnostic
line on hotel footage, with no line candidates, so **this verifies scanning and
publication, not crowded bicycle inference throughput**. It predates the small
batch-duration tolerance adjustment (approximately 8-second to 6-second batches).

Still required before race acceptance:

- At least 30 minutes under simultaneous recording, chip intake and review.
- Crossings of actual riders, pedestrians and motorcycles, including dense groups.
- End-to-end processing ratio below 1, preferably below 0.5, with backlog recovery.
- Original-frame evidence, segment-boundary crossings and first-rider anchor checks.
- Long-archive fallback costs and source gaps: no pretending that these are scanned.

## Repeatable isolated benchmark

```powershell
python -m tools.benchmark_finish_analysis "D:\path\to\event" --segments 30
python -m tools.benchmark_finish_analysis "D:\path\to\event" --segments 30 --reference
```

The source files/journals are read-only. Output and clock models go into a new
temporary workspace printed in the report. Run comparisons serially with the
same camera settings/load. `--no-model` is explicitly a scan-only measurement.
Always inspect candidate counts: enabling a model with zero candidates does not
measure inference performance. Existing app processes need a normal restart to
load edited source; this repair does not interrupt an active recorder.
