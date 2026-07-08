"""Per-item shaka-packager worker.

The TERMINAL stage of the catalog pipeline. Consumes the Kafka topic
`stube.catalog.item.transcoded` (group `packager-workers`), resolves the
item's full detail via `GET /api/analyze/items/{id}`, runs ffmpeg +
shaka-packager to build a streaming-friendly CMAF/HLS tree under
`/var/lib/katalog/packages/{category}/{shard}/{itemId}/`, reports the
outcome via `PUT /api/analyze/items/{id}/steps/package`, and mirrors the
manifest with `POST /api/items/{id}/packaging-complete`. It produces no
downstream event.

When katalog-transcoder ran ahead of us it drops a `prepared.mkv` into
`{packages_root}/_inbox/{itemId}/`; the packager prefers that file over
the original source so the downstream shaka run sees HEVC + the full
subtitle set the transcoder preserved (PGS/ASS/etc.) instead of the
original codec.

Runs in its own pod (katalog-packager) — split out of katalog-analyzer
so a packager OOM / codec crash doesn't take down the analyzer's
in-memory TIDB sweep or per-file ML pipelines.
"""
