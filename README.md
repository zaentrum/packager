# packager

Per-item CMAF packager for the zaentrum platform — the **terminal stage**
of the catalog pipeline. A small Python worker that consumes the Kafka
topic `stube.catalog.item.transcoded`, runs `ffmpeg` + `shaka-packager`,
and emits a streaming-friendly CMAF/HLS tree (plus trickplay sprites and
WebVTT subtitles) under the per-item output directory.

## Status

**Kafka event consumer.**

The packager consumes `stube.catalog.item.transcoded` (consumer group
`packager-workers`), resolves the item's full detail over the katalog
HTTP API, runs the packaging (HEVC passthrough, AAC transcode-on-demand,
subtitle extraction, trickplay generation), writes the `package` step +
manifest back over HTTP, and produces **no** downstream event. Offsets
are committed manually only after an item is fully processed, so a crash
mid-work reprocesses (idempotent via the katalog `(item_id, step)`
unique index + the done-step guard).

## Layout

```
src/packager/main.py        # entry point: consumer thread + FastAPI /healthz + /readyz
src/packager/config.py      # env-driven config (KATALOG_API_URL, OIDC_*, KAFKA_* topic/group)
src/packager/events.py      # Kafka consumer factory + envelope parsing
src/packager/katalog.py     # HTTP client to the katalog API (get_item/get_steps/upsert_step/packaging_complete), OIDC auth
src/packager/worker.py      # consumer loop: one item at a time, serial packaging
src/packager/packager.py    # the package itself: ffmpeg remux + shaka-packager + trickplay + VTT
scripts/                    # one-off backfill / diagnostics helpers
k8s/                        # Deployment, Service, ServiceAccount, ServiceMonitor, GrafanaDashboard
Dockerfile
```

## Design notes

- **Video is HEVC passthrough** — packages never re-encode. A source whose
  video codec isn't `hevc`/`h264` fails the job; the operator picks a
  different source.
- **Audio** is AAC passthrough when the source track is already AAC,
  otherwise transcoded to AAC-LC 48 kHz stereo (the minimum browsers can
  decode natively).
- **Subtitles** are extracted to WebVTT.
- **All state is on disk** under the per-item output directory; three
  mutually exclusive sentinels (`.packaging`, `.complete`, `.failed`) tell
  every reader where a package is in its lifecycle without a database
  round-trip.

## Local development

```bash
pip install -e '.[dev]'
pytest
```

## Build the container

```bash
docker build -t zaentrum/packager .
```

Build and push the image to your own registry and update the image
reference in `k8s/deployment.yaml` for your environment. The deployment
expects two PVCs (read-only source media, writeable packaged output),
the `KATALOG_API_URL` / `OIDC_*` env vars wired to your katalog API and
identity provider, and `KAFKA_BROKERS` (+ optional `KAFKA_SECURITY_PROTOCOL`,
`KAFKA_GROUP_ID`, `CONSUME_TOPIC`) pointing at your broker.

## License

[MPL-2.0](LICENSE).
