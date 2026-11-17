# packager

The packager is the last worker of the platform's media pipeline, a Python service. It
consumes `stube.catalog.item.transcoded` from Kafka (consumer group `packager-workers`),
reads the item's worker record from the catalog API (katalog-manager), and packages the
transcoder's renditions (`renditions.json` in the item's inbox), or the original, with
ffmpeg and shaka-packager into one CMAF/HLS tree: video, stereo and 5.1 audio, text and
image subtitles, fonts, trickplay. On the library v2 layout it writes the version's records
too. It reports the `package` step to the catalog and sends no event; a second consumer
packages a title's extras, from `<KAFKA_TOPIC_PREFIX>catalog.extra.transcoded` ([README](README.md)).

How a change is proposed and what a pull request has to pass is in the
[guide to contributing](https://github.com/zaentrum/.github/blob/main/CONTRIBUTING.md);
what may be built is in [building from the specification](https://github.com/zaentrum/zaentrum/blob/main/AGENTS.md).

## Build

```sh
python3.11 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
python -m compileall src
ruff check src
```

Python 3.11 or later (`requires-python` in `pyproject.toml`; `build.yml` uses 3.11). At run
time it calls `ffmpeg`, `ffprobe`, shaka-packager's `packager` and, for a VobSub track,
`mkvmerge` and `mkvextract` from the PATH; without either, no VobSub pair is written. The
image, `python:3.11-slim` with Debian's ffmpeg and mkvtoolnix and shaka-packager v3.4.2,
which `image.yml` pushes on `main`, on a tag and when started by hand:

```sh
docker build -t zaentrum/packager .
```

## Test

`build.yml` runs in the image's base, `python:3.11-slim`, with the tools the image installs:
its Debian's `ffmpeg` and `mkvtoolnix`, and shaka-packager v3.4.2 by the same sha256. After
the build above it runs:

```sh
pytest
```

- Most tests need nothing installed: plans, commands, playlists and records are checked
  on probes and files the tests hold (`tests/fixtures/`, `tests/golden/`), against a fake
  catalog behind an httpx mock transport and a fake broker.
- The `tests/*_real.py` modules and `tests/test_styled_subtitles.py` package generated clips
  with real ffmpeg. They skip without `ffmpeg` and `ffprobe` on the PATH; most also without
  shaka-packager's `packager`, those that make an HEVC clip without libx265, the package,
  surround and parameter-set ones below ffmpeg 7, the downmix and loudness ones below 5.1.
  The VobSub cases need `mkvmerge` and `mkvextract`.
- `tests/test_fonts.py`, `tests/test_subtitles.py` and `tests/test_subtitle_tracks.py` have
  real-ffmpeg cases too, which skip without it; those of `test_subtitle_tracks.py` also
  without `mkvmerge` and `mkvextract`.
- Five modules validate the trees they write (`tests/test_library_real.py`,
  `tests/test_library_modes_real.py`, `tests/test_surround_real.py`, `tests/test_hdr10_real.py`
  and `tests/test_styled_subtitles.py`) with the schemas repository's
  `validate-library-v2.py --check-checksums`, when a checkout of it is beside this one (or `ZAENTRUM_SCHEMAS` names one) and a Python with
  `jsonschema[format-nongpl]` and `referencing` (this one, or `LIBRARY_V2_PYTHON`) runs it.
  `build.yml` checks the schemas repository out at the commit the vendored record logic is
  (`tests/test_libv2_records.py`) and installs both, pinned; with `CI` set, a test that
  cannot validate fails instead of skipping.

## Run it

The packager runs inside the platform: the operator's chart starts it when the `Zaentrum`
resource sets `features.pipeline` ([self-hosting](https://github.com/zaentrum/zaentrum/blob/main/docs/self-hosting.md)),
as on the reference demo ([how it is deployed](https://github.com/zaentrum/zaentrum/blob/main/docs/reference-demo.md)).
It needs the catalog API, an OIDC client's id and secret (`OIDC_CLIENT_ID`,
`OIDC_CLIENT_SECRET`, for the client-credentials grant; the platform's chart gives analyzer,
packager and transcoder one shared client), a Kafka broker and the library share at
`/var/lib/katalog`. Started by hand against those:

```sh
export KATALOG_API_URL=http://localhost:8081 OIDC_TOKEN_URL=https://idp.example.org/token \
  OIDC_CLIENT_ID=packager OIDC_CLIENT_SECRET=... KAFKA_BROKERS=localhost:9092
python -m packager.main
```

`KATALOG_API_URL` and the three `OIDC_` variables are required; the README's "Configuration"
and `config.py` have the rest. Port 8080 serves `/healthz` and `/readyz` (whether both
consumer threads are alive). Without a broker or a catalog it starts all the same and logs
each failed connection. No mock server or recorded answers ship; the tests' fakes stand in.

## What the tests leave unchecked

- No test plays a package in a player, talks to a real broker or catalog, or fetches a token;
  `scripts/backfill_image_subs.py` and `scripts/populate_item_diagnostics.py` have no tests.
- The image is built only by `image.yml`, on `main`, on tags and when started by hand; no
  test runs in it.

## Before a pull request

- `src/packager/libv2_records.py` is the schemas repository's `tools/libv2_records.py`,
  vendored byte for byte. `tests/test_libv2_records.py` pins its SHA-256 and `LIBV2_RECORDS`
  with the golden records in `tests/fixtures/libv2_records/`: a new one comes with the golden
  records of the same schemas commit, as that test's docstring says. Ruff leaves it out.
- `src/packager/regions.py` is written by `node scripts/regions.mjs > src/packager/regions.py`
  from Node's CLDR data, not by hand; `tests/test_track_names.py` spot-checks it.
