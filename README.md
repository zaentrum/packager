# packager

Per-item CMAF packager for the zaentrum platform. A small Python worker
that claims items from the katalog API, runs `ffmpeg` + `shaka-packager`,
and emits a streaming-friendly CMAF/HLS tree (plus trickplay sprites and
WebVTT subtitles) under the per-item output directory.

## Status

**Scaffold; pipeline wiring in progress.**

The packager logic (HEVC passthrough, AAC transcode-on-demand, subtitle
extraction, trickplay generation) is in place. The intended task contract
is **Kafka topics** (`stube.processing.task.package.*`); the current worker
still claims items over the HTTP claim API while that migration lands.

## Layout

```
src/packager/main.py        # entry point: worker thread + FastAPI /healthz + /readyz
src/packager/config.py      # env-driven config (KATALOG_API_URL, OIDC_*, batch/sleep tunables)
src/packager/katalog.py     # HTTP client to the katalog API (claim + upsert_step), OIDC auth
src/packager/worker.py      # claim loop: one item at a time, serial packaging
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
expects two PVCs (read-only source media, writeable packaged output) and
the `KATALOG_API_URL` / `OIDC_*` env vars wired to your katalog API and
identity provider.

## License

[MPL-2.0](LICENSE).
