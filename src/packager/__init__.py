"""Per-item shaka-packager worker.

Claims items whose package step is pending from katalog-app's
`/api/analyze/claim?pass=packager` endpoint, runs ffmpeg + shaka-packager
to build a streaming-friendly CMAF/HLS tree under
`/var/lib/katalog/packages/{category}/{shard}/{itemId}/`, and reports
the outcome back via `PUT /api/analyze/items/{id}/steps/package`.

When katalog-transcoder ran ahead of us it drops a `prepared.mkv` into
`{packages_root}/_inbox/{itemId}/`; the packager prefers that file over
the original source so the downstream shaka run sees HEVC + the full
subtitle set the transcoder preserved (PGS/ASS/etc.) instead of the
original codec.

Runs in its own pod (katalog-packager) — split out of katalog-analyzer
so a packager OOM / codec crash doesn't take down the analyzer's
in-memory TIDB sweep or per-file ML pipelines.
"""
