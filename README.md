# packager

Per-item CMAF packager for the zaentrum platform — the **terminal stage**
of the catalog pipeline. A small Python worker that consumes the Kafka
topic `stube.catalog.item.transcoded`, runs `ffmpeg` + `shaka-packager`,
and emits a streaming-friendly CMAF/HLS tree (plus trickplay sprites and
subtitle sidecars) under the per-item output directory.

## Status

**Kafka event consumer.**

The packager consumes `stube.catalog.item.transcoded` (consumer group
`packager-workers`), resolves the item's full detail over the katalog
HTTP API, packages it, writes the `package` step + manifest back over
HTTP, and produces **no** downstream event. The manifest it sends the
catalog carries a `source` block (`codec`, `width`, `height`,
`durationMs`, `bitRate`), which the catalog keeps as the title's source
asset: the transcoder's renditions.json `source`, completed by a probe of
the original for what it doesn't say (all of it when there is no
handoff). `manifest.json` on disk keeps no source block. Offsets are committed
manually only after an item is fully processed, so a crash mid-work
reprocesses (idempotent via the katalog `(item_id, step)` unique index +
the finished-step guard).

Packaging runs on the Kafka poll thread, one item at a time, as in the
analyzer, and the consumer's `max.poll.interval.ms` is 24 h (librdkafka's
maximum). A long film with many tracks packages for far longer than the
5-minute default, and a worker that polls too late loses its partition:
the broker would hand the item, not committed yet, to the other replica,
which would package it again into the same directory. It is the
catalog's reaper, not Kafka, that decides when a silent run is dead. A
rebalance (a replica joining or leaving) waits until every busy replica
has finished its item.

The package step is finished when it is `done`, `not_applicable` or
`skipped`; an event for a finished item packages nothing. The catalog
retries a failed or silent package by sending its `transcoded` event
again, marked `"status": "retry"`. A retry whose step has finished since
— a long run the catalog's reaper took for dead that reported done after
all — is acked with one log line (`packager.retry.already_finished`) and
nothing else; any other retry packages as usual.

## Inputs

The transcoder's handoff in `{PACKAGES_ROOT}/_inbox/{itemId}/` (schema:
the transcoder README, "Rendition contract"):

- `renditions.json` — N video rungs, largest first. v0 carries every
  audio and subtitle track (`prepared.mkv`, or the original source when
  v0 is a stream copy); v1… are video only. Segments are cut every
  `segmentSeconds` from the file, the interval the rungs' keyframes were
  forced at. The inbox files share one timeline; they are remuxed with
  `-copyts` and the original is shifted onto it by `timestampOffset`, so
  every rung and the audio carry identical timestamps for identical
  frames.
- only `prepared.mkv` (an older transcoder) — one rung, as before.
- nothing — the original source, one rung, as before.

The handoff is deleted after a successful package.

The item record may carry `trackLanguages`, the catalog's language for
tracks whose tag is missing or wrong: `[{"kind": "audio" | "subtitle",
"ordinal": 0, "language": "eng"}]`, `ordinal` counting the source's
tracks of that kind in ffprobe order, `language` an ISO 639-2 code (B or
T form; `zxx` no dialogue, `und` unknown). A track's language is its
override, else its tag, else `und`, everywhere: the remux, the manifest,
the playlists, the whitelist and the DEFAULT pick. When v0 is the
transcoder's encode, which leaves out the subtitle tracks Matroska can't
stream-copy, an ordinal finds its track through a probe of the source.
Malformed entries, and ordinals no packaged track has, are ignored and
logged.

## Output

```
hls/master.m3u8                         assembled here (below)
hls/v0/ v1/ …   init.mp4 seg-NNNNN.m4s playlist.m3u8 iframes.m3u8
hls/a0/ a1/ …   init.mp4 seg-NNNNN.m4s playlist.m3u8
hls/s0/ s2/ …   seg-NNNNN.vtt playlist.m3u8   (WebVTT tracks only)
subs/N.vtt|.sup|.idx|.dvb                sidecars, as before
trickplay/  manifest.json  .complete
.next/                                  a run's staging folder, same layout
hls.old-<stamp>/ subs.old-<stamp>/ …    replaced, until its grace period ends
```

**Master.** shaka writes the media and I-frame playlists; the master is
written by `packager/hls.py`. It keeps shaka's CODECS (read from the
bitstreams), RESOLUTION, LANGUAGE and I-frame lines, and computes the rest:

- One variant per video rung and audio group: stereo group first, then
  top rung first. That first variant is where players start and what
  client prefetchers warm.
- BANDWIDTH / AVERAGE-BANDWIDTH per RFC 8216 from the real segment
  sizes: the peak is the highest rate over any run of segments lasting
  0.5–1.5× the target duration. Each variant counts its video plus the
  largest rendition of its audio group (plus subtitles).
- FRAME-RATE from ffprobe (shaka reports 23.810 for 23.976 MKVs) and
  VIDEO-RANGE (SDR/PQ/HLG) per rung, so a tone-mapped SDR rung of an
  HDR title is labelled as such.
- Exactly one DEFAULT=YES per audio group. AUTOSELECT goes on the first
  visible rendition of each language, so commentary is never
  auto-picked.
- A rendition's NAME is the English name of its language: "English",
  "German", "No dialogue" for `zxx`, "Unknown" for `und`; " 5.1" in the
  5.1 group, "(forced)" on a forced subtitle. Never the source's title,
  a free text that is often a codec descriptor ("AC3 5.1 @ 640 Kbps").
  NAMEs are unique within a group: a second English track is
  "English (2)".

**Languages.** Every track is packaged. One in a language outside the
`packager.language_whitelist` setting is marked `visible: false` in the
manifest, so the clients leave it out of their menus; when that would
hide every track of a source in one language,
`packager.keep_original_if_single` (on by default) shows them all. A
track tagged `und` (unknown) or `zxx` (no dialogue) is always visible.

**Audio.** Every source track becomes an AAC-LC 48 kHz stereo rendition
in group `audio`, as before. With `SURROUND_AUDIO` set (it is `off` by
default until chino-stream filters the master per client), a visible
track with ≥ 6 channels also gets a 5.1 E-AC-3 rendition in group
`audio-surround`, first per language, commentary excluded. The source track is stream-copied when it
already is E-AC-3, encoded at 448k otherwise. In the stereo group,
DEFAULT=YES goes to the first language of `PREFERRED_LANGUAGES`
(default: the `packager.language_whitelist` order) that has a track. In
the 5.1 group it goes to that track's 5.1 companion; when it has none,
to the 5.1 rendition of its language, else of the first preferred
language that has one, else to the first — so the 5.1 group has its one
default even when the default language has no 5.1 track. Language tags
match across ISO 639-1/-2 (`de` = `ger` = `deu`). In `manifest.json`,
`renditions.audio` still lists the stereo tracks only, with exactly one
`default`. The 5.1 renditions go under `renditions.audioSurround` (also
with exactly one `default`), so readers that count or list `audio` see
the same tracks as before.

**Subtitles.** Sidecars are extracted exactly as before; PGS / VobSub /
DVB stay sidecar-only because HLS can't carry them. Every visible WebVTT
track is also segmented into an HLS rendition `hls/sN/` (N = the sidecar
index; `subtitles[].hls` in the manifest). Their segments carry no
`X-TIMESTAMP-MAP`, which HLS reads as cue time = media time: shaka's
default map (`MPEGTS:9000`, the 100 ms it shifts MPEG-TS output by) put
every cue 100 ms after its frame against our fMP4 media, which start
at 0. The master references them
(TYPE=SUBTITLES, DEFAULT=NO, FORCED=YES on forced tracks) only with
`HLS_SUBTITLES=true`, which is **off by default**. The clients draw the
sidecars themselves, and the playback API doesn't route `sN/` yet.
Turning it on changes the master only; the renditions are already on
disk. In the manifest, at most one subtitle is `default`: a visible
forced track that isn't in a foreign language, in the default audio
track's language first, else of no known language; any forced one when
the audio's language isn't known (`und`, `zxx`). The source's default
flag doesn't count: ffmpeg flags the first subtitle default when a file
has several and flags none, which showed German subtitles by themselves
on an English film.

**Compatibility.** Rendition dirs stay `vN` / `aN`, so the existing
playback routes serve every rung and both audio groups. `renditions.video[0]`
is still the top rung. Before a ladder is enabled, the playback service
must filter the master per client. Players don't switch between HEVC
and H.264, and hls.js starts on H.264 whenever the HEVC variant's
BANDWIDTH is above its 5 Mbit/s initial estimate.

**Packaging again.** A run leaves the live package alone while it
works: it builds the new one in `.next/` inside the item folder (the
item folder itself is never moved or created again: the playback
service caches its path and NFS clients its handle) and swaps it in only
once it is whole: every playlist the master and the manifest name is
there and ends, and every init section, segment, sidecar and sprite they
reference is there. The swap goes entry by entry, `subs/` and
`trickplay/` first and `hls/` last: the live entry is renamed
`<name>.old-<stamp>` in the item folder, then the new one moved up from
`.next/`. Then `manifest.json` is replaced in one rename and `.complete`
written again. A reader gets the old package or the new one, and the new
manifest only once everything it names is in place; between an entry's
two renames, a request for it misses. Until the swap the title plays
from its old package, and a run that fails removes `.next/`, writes
`.failed` and leaves the live package, `.complete` included, as it was.
The replaced package stays for `OLD_PACKAGE_GRACE_SECONDS` (10 minutes),
for the requests that started on it and the NFS clients that still have
it cached, then goes; when the process exits first, the item's next run
or the sweep at startup removes it. The sweep, on a thread of its own,
also removes the staging folder of a run that started more than a day
ago: no run lasts that long (it would have lost its partition), so that
run died; a younger one may be another replica's, at work. A viewer
already watching gets the new package's files from the swap on, under
the same names: seamless where a rendition is unchanged (a copied v0,
the audio), not where a rung was encoded anew.

## Layout

```
src/packager/main.py        # entry point: consumer thread + FastAPI /healthz + /readyz
src/packager/config.py      # env-driven config
src/packager/events.py      # Kafka consumer factory + envelope parsing
src/packager/katalog.py     # HTTP client to the katalog API, OIDC auth
src/packager/worker.py      # consumer loop: one item at a time, serial packaging
src/packager/renditions.py  # reads the transcoder handoff (renditions.json)
src/packager/packager.py    # ffmpeg remux + shaka-packager + trickplay + subtitles
src/packager/hls.py         # master playlist assembly + RFC 8216 bit rates
scripts/                    # one-off backfill / diagnostics helpers
k8s/                        # Deployment, Service, ServiceAccount, ServiceMonitor, GrafanaDashboard
Dockerfile
```

## Configuration

| Variable | Default | Description |
| --- | --- | --- |
| `KATALOG_API_URL`, `OIDC_*` | (required) | katalog API + client credentials |
| `KAFKA_BROKERS` | `kafka:9092` | Bootstrap brokers (`KAFKA_SECURITY_PROTOCOL`, `KAFKA_GROUP_ID`, `CONSUME_TOPIC`) |
| `SEGMENT_SECONDS` | `6` | Segment length when renditions.json doesn't set it |
| `SURROUND_AUDIO` | `off` | 5.1 companion codec: `eac3`, `ac3` or `off`. Turn on only once chino-stream drops the `audio-surround` group for clients that can't decode it |
| `SURROUND_BITRATE` | `448k` | Bitrate of an encoded 5.1 companion |
| `HLS_SUBTITLES` | `false` | Reference the WebVTT renditions from the master |
| `PREFERRED_LANGUAGES` | (empty) | DEFAULT=YES language order, e.g. `de,en`; empty = whitelist order |
| `OLD_PACKAGE_GRACE_SECONDS` | `600` | How long a package replaced by a new one stays on disk for the requests that started on it (see "Packaging again"); keep it well above the NFS mounts' attribute cache time |

## Local development

```bash
pip install -e '.[dev]'
pytest
```

Unit tests need nothing installed. `tests/test_package_real.py`
packages a generated clip end to end when `ffmpeg`, `ffprobe` and
shaka-packager's `packager` are on `PATH`.

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
