"""Per-item shaka-packager worker.

The TERMINAL stage of the catalog pipeline. Consumes the Kafka topic
`stube.catalog.item.transcoded` (group `packager-workers`), resolves the
item's full detail via `GET /api/analyze/items/{id}`, runs ffmpeg +
shaka-packager to build a streaming-friendly CMAF/HLS tree under
`/var/lib/katalog/packages/{category}/{shard}/{itemId}/`, reports the
outcome via `PUT /api/analyze/items/{id}/steps/package`, and mirrors the
manifest with `POST /api/items/{id}/packaging-complete`. It produces no
downstream event.

When katalog-transcoder ran ahead of us it leaves its handoff in
`{packages_root}/_inbox/{itemId}/` — `renditions.json` listing one or
more video renditions (`prepared.mkv` carrying every audio + subtitle
track, `v1.mkv`... video-only lower rungs), or just `prepared.mkv` from
an older transcoder. The packager packages every rendition into one HLS
master (`packager.renditions`, `packager.hls`); without a handoff it
packages the original source.

When the catalog runs the library v2 layout, the worker record carries a
`library` block and the packager writes the title's source and version
folders into the library tree instead, each built in the work tree's
staging folder and renamed into place in one step (`packager.library`).

Runs in its own pod (katalog-packager) — split out of katalog-analyzer
so a packager OOM / codec crash doesn't take down the analyzer's
in-memory TIDB sweep or per-file ML pipelines.
"""
