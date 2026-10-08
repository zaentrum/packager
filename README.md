# packager

Per-item CMAF packager for the zaentrum platform — the **terminal stage**
of the catalog pipeline. A small Python worker that consumes the Kafka
topic `stube.catalog.item.transcoded`, runs `ffmpeg` + `shaka-packager`,
and emits a streaming-friendly CMAF/HLS tree (plus trickplay sprites and
subtitle sidecars) under the per-item output directory. A second
consumer packages the extras of a title (trailers and other bonus
material) into folders of their own; see [Extras](#extras).

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

It may also carry `subtitleFiles`, the subtitle files next to the
source: `[{"path": "/abs/Movie.en.srt", "language": "eng", "label":
"English", "forced": false}]`. A file is taken when its path is
absolute, in the source's folder or below it, and ends in `.srt`,
`.vtt`, `.ass` or `.ssa`; its language is `und` unless it is a code as
above. Other entries are ignored and logged.

## Output

```
hls/master.m3u8                         assembled here (below)
hls/v0/ v1/ …   init.mp4 seg-NNNNN.m4s playlist.m3u8 iframes.m3u8
hls/a0/ a1/ …   init.mp4 seg-NNNNN.m4s playlist.m3u8
hls/s0/ s2/ …   seg-NNNNN.vtt playlist.m3u8   (WebVTT tracks only)
subs/N.vtt|.sup|.idx|.dvb                sidecars: the source's tracks, then its subtitle files
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
  5.1 group; then what the source's title says besides, by the rule the
  clients label tracks with: "English · Commentary", "English · SDH",
  "German · Kommentar", at most 40 characters. A title that names a
  format ("AC3 5.1 @ 640 Kbps", "DTS-HD MA"), numbers the track
  ("Track 0") or repeats the language ("English", "Deutsch", "eng")
  adds nothing; a track of unknown language is called what its title
  says, else "Unknown". A forced subtitle whose name doesn't say so gets
  "(forced)". NAMEs are unique within a group: a second "English" is
  "English (2)".

**Metadata.** A package carries none of the original's global metadata:
the remuxes shaka-packager packages from leave out the container's title,
its other tags and its chapters (a library v2 version keeps its chapters
in `version.json`). The streams' own metadata, their languages, still
names the renditions.

**Languages.** Every track is packaged. One in a language outside the
`packager.language_whitelist` setting is marked `visible: false` in the
manifest, so the clients leave it out of their menus; when that would
hide every track of a source in one language,
`packager.keep_original_if_single` (on by default) shows them all. A
track tagged `und` (unknown) or `zxx` (no dialogue) is always visible.

**Audio.** Every source track becomes an AAC-LC 48 kHz stereo rendition
in group `audio`, as before. With `SURROUND_AUDIO` set (`eac3`; it is
`off` unless the platform turns it on), every surround track — more than
two channels, as a library record's essence counts surround — also gets
a 5.1 E-AC-3 rendition in group `audio-surround`, the first per
language, commentary excluded: a film in four languages keeps a 5.1 in
each, and a stereo film gets none. A track the language whitelist hides
gets its companion too, hidden as its stereo rendition is (the package
keeps what the original carries). An E-AC-3 5.1 (Atmos included) is
stream-copied, bit for bit; anything else is encoded to a 5.1 at 448k: a
7.1 downmixed (a 5.1 can't keep its two extra channels, which a v2
record's deletion gate still names as lost), fewer channels upmixed.
Every companion is a 5.1: `CHANNELS="6"`, `CODECS` `ec-3`, its NAME the
language's with " 5.1" ("German 5.1"). chino-stream serves the group
only to a client whose caps include `eac3` (`internal/play/ladder.go`);
`/play/info` lists the stereo renditions. In the stereo group,
DEFAULT=YES goes to the first language of `PREFERRED_LANGUAGES`
(default: the `packager.language_whitelist` order) that has a track. In
the 5.1 group it goes to that track's 5.1 companion; when it has none,
to the 5.1 rendition of its language, else of the first preferred
language that has one, else to the first, a shown one before a hidden
one — so the 5.1 group has its one default even when the default
language has no 5.1 track. Language tags
match across ISO 639-1/-2 (`de` = `ger` = `deu`). In `manifest.json`,
`renditions.audio` still lists the stereo tracks only, with exactly one
`default`. The 5.1 renditions go under `renditions.audioSurround` (also
with exactly one `default`), so readers that count or list `audio` see
the same tracks as before.

**Subtitles.** Sidecars are extracted exactly as before; PGS / VobSub /
DVB stay sidecar-only because HLS can't carry them. The source's PGS and
text tracks are written in one ffmpeg run over it, an output per track,
so the file is read once however many tracks it has; when that run
fails, each track is extracted in a run of its own, as before, and a
track ffmpeg can't extract costs that track only. VobSub tracks are
written by mkvextract as `subs/N.idx` + `subs/N.sub` (FFmpeg has no
muxer for the pair), all of a title's in one more read of the source, a
Matroska one only; a track mkvmerge can't match to the probe's stream
(by its id, codec and language) is not extracted, logged. DVB tracks
keep their own attempt, which fails as ffmpeg sets it up (it has no
format for a `.dvb` file): such tracks are not in the package today,
and the failure is logged. The subtitle files
next to the source (`subtitleFiles`) follow the source's own tracks:
each is decoded (by its byte order mark, else as UTF-8, else in the
legacy code page of its language, e.g. Windows-1251 for Russian, else
Windows-1252), converted to WebVTT by ffmpeg as the source's text
tracks are, and written as `subs/N.vtt`, N counting on from the
source's tracks, with the catalog's label as its `title` and
`external: true`; from there on it is a subtitle track like the
others. A file ffmpeg can't read is left out, logged. Every visible WebVTT
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

**HEVC parameter sets.** Some files carry an HEVC decoder configuration
(the Matroska CodecPrivate) that names no VPS, SPS or PPS: they are in
the stream only. FFmpeg's MP4 muxer (7.1 and later) writes an empty
`hvcC` for such a stream copied as `hvc1`, which shaka-packager can't
parse, so the packager copies it through Annex B (`hevc_mp4toannexb`),
and the muxer builds the `hvcC` from the parameter sets of the first
frame. Nothing is re-encoded; every other stream is copied exactly as
before.

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

## Extras

The extras of a title (trailers, teasers, featurettes, making-ofs) are
catalog rows of their own, keyed by an extraId, and are packaged on their
own. A second consumer, on a thread of its own and in a consumer group of
its own, packages them, so a long film's run never holds a two-minute
trailer up behind it. One item and one extra can therefore package at
once per pod.

| | Items | Extras |
| --- | --- | --- |
| Consumes | `CONSUME_TOPIC` | `<KAFKA_TOPIC_PREFIX>catalog.extra.transcoded` |
| Consumer group | `KAFKA_GROUP_ID` (`packager-workers`) | `EXTRAS_GROUP_ID` (`packager-extras`) |
| Inbox | `_inbox/<itemId>/` | `_inbox/extra-<extraId>/` |
| Worker record | `GET /api/analyze/items/{id}` | `GET /api/analyze/extras/{id}` |
| Step | `PUT /api/analyze/items/{id}/steps/package` | `PUT /api/analyze/extras/{id}/steps/package` |
| Package handed over | `POST /api/items/{id}/packaging-complete` | `POST /api/extras/{id}/packaging-complete` |
| Package folder | `movies/`, `shows/`, … `<aa>/<itemId>/` | `extras/<aa>/<extraId>/` |
| Trickplay | yes | no |

`KAFKA_TOPIC_PREFIX` is the tenant's topic prefix, `stube.` by default (a
missing trailing dot is added), so the topic defaults to
`stube.catalog.extra.transcoded`. The transcoder sends the event once it
has encoded the extra, keyed by the extraId:

```json
{"eventId": "9f2b…", "extraId": "1b5c2a8e-…", "parentId": "ea886f9b-…",
 "type": "extra", "kind": "trailer", "step": "package", "status": "queued",
 "occurredAt": "2026-10-06T08:00:00Z", "source": "transcoder"}
```

It carries no `itemId`, so the item loop would skip one that reached its
topic. An extraId that is not a lower-case UUID makes the event
malformed: it is committed and skipped.

Per event:

1. `GET /api/analyze/extras/{id}`. A 404 (unknown or removed), a record
   with `removedAt`, or an extra in state `missing` (its file is gone)
   is skipped: nothing is written.
2. An extra in state `ready` is finished: nothing runs. The transcoder
   sends `catalog.extra.transcoded` again for an extra past its
   transcode whenever a trigger reaches it again, so this is a duplicate
   of a run that finished; a retry (`"status": "retry"`) is acked with
   one log line (`packager.extra.retry.already_finished`). Every other
   state packages, as an unfinished item does, `packaging` included: a
   run that died.
3. The package step goes `in_progress`, and the handoff in
   `_inbox/extra-<extraId>/` is read under the unchanged contract
   ([Inputs](#inputs)): its `itemId` is the extraId, and a
   `"file": null` rung, like no handoff at all (the transcoder's step
   `not_applicable`), is the extra's file as it is. It is packaged as an
   item is, with the same options and language settings, but as type
   `extra`, without trickplay, into `extras/<aa>/<extraId>/`. The
   record's `language` (BCP 47 or ISO 639-2: `en`, `zxx`), when it names
   one, is the language of the extra's first audio track, over the file's
   own tag, as a title's track languages are: a trailer's file often says
   `und`, and one registered as `zxx` has no dialogue.
4. `POST /api/extras/{id}/packaging-complete` with the manifest and the
   source block. Only once the catalog has taken it is the step `done`
   and the handoff removed. When the catalog refuses it, the step fails,
   unlike an item's, which is done regardless: only the catalog's word
   makes an extra playable, so its retry runs the chain again. A record
   without a parent or a path, a handoff that can't be read and a file
   that is gone fail the step without a run.

The manifest has an item's keys, minus `trickplay`, plus the title the
extra belongs to (`parentId`) and the extra's kind (`extraKind`). Its
`itemId` is the extraId, its `type` is `extra`, and `year` and `tmdbId`
are null:

```json
{"version": 2, "itemId": "1b5c2a8e-…", "type": "extra", "title": "Trailer",
 "year": null, "tmdbId": null, "durationMs": 33000, "packagedAt": "…", "packager": "…",
 "renditions": {
   "video": [{"id": "v0", "dir": "hls/v0", "codec": "avc1.64001f", "width": 1280, "height": 720, "…": "…"},
             {"id": "v1", "dir": "hls/v1", "codec": "avc1.64001e", "width": 854, "height": 480, "…": "…"}],
   "audio": [{"id": "a0", "dir": "hls/a0", "codec": "mp4a.40.2", "language": "eng", "default": true, "…": "…"}],
   "audioSurround": []},
 "subtitles": [],
 "hls": {"master": "hls/master.m3u8", "segmentSeconds": 6, "audioGroups": ["audio"],
         "subtitleGroup": null},
 "parentId": "ea886f9b-…", "extraKind": "trailer"}
```

**A folder of its own.** An extra is never written inside its title's
folder. A title packaged again retires everything in its folder that its
new manifest doesn't name ("Packaging again" above), and the playback
service takes every folder with a `.complete` in a title category for a
title. `extras/` is a category of its own, packaged as the others are: a
run builds the next package in `.next/` and swaps it in whole, the
replaced one stays for its grace period, and the startup sweep clears
what dead runs left there. The playback service serves an extra under
its title only (`parentId`), and never lists one as a title.

**Mounts.** An extra's file can sit outside the media root (wherever the
catalog takes extras from). The packager reads it for a rung the
transcoder left as it was, and when there is no handoff, so mount that
directory read-only too. The zaentrum platform chart mounts the whole
volume at `/var/lib/katalog`, which covers it.

**Topic.** The topic has to exist on the broker, unless the broker
creates topics on first use. Until it does, the extras consumer logs a
warning now and then, and the item loop is unaffected.

## Library v2

When the catalog runs the library v2 layout (its setting `library.layout`
is `v2`), both worker records carry a `library` block (contract
`platform-library/1`): the folders of the title's record in the library
tree, its work tree's inbox and staging folders, the original's source
record and the version to build. The packager then writes into the
title's record instead of the package store. Every path comes from the
record; the packager never works one out. A record without the block is
packaged exactly as above. A block the packager can't work from (another
contract, a path that isn't absolute or doesn't follow the record's own
rules) fails the step and is never packaged into the package store
instead; a block whose `blocked` says why the item can't be recorded yet
fails the step with those words.

```
<itemDir>/sources/<sourceId>/    source.json  ffprobe.json  <sidecar copies>  checksums.sha256
<itemDir>/versions/<versionId>/  version.json  original.<ext>  hls/  subs/  trickplay/
                                 checksums.sha256  package.json  .complete
<itemDir>/extras/<extraId>/      extra.json  hls/  subs/  checksums.sha256  package.json  .complete
<workRoot>/staging/<versionId>/       .packaging  .original  source/  version/   a run, until its handover
<workRoot>/staging/extra-<extraId>/   .packaging  extra/
```

A version folder is the one place its media lives: its original, the
package made from it, or both. What a run writes there is the record's
`build.mode`, which the catalog decides:

| mode | when | the version folder |
| --- | --- | --- |
| `establish` | a source's first version, the pipeline's | `version.json`, the package and its chain, the original renamed in as `build.originalName` (`original.mkv`) |
| `takein` | a title that gets no package now (a takein job, below) | `version.json` and the original: no package, and so no chain |
| `add` | a version that holds only its original gets its package | the version folder there already (`build.versionDir`): the package and its chain added |
| `repackage` | a title whose version has a package | a new version folder, `version.json` (no original) with the package and its chain; the original stays where it is |

A record without a mode is a run from before the catalog named them,
built as a `repackage` is. The packager reads the original where the
record's `path` says — its arrival before its version is established, the
version folder's `original.<ext>` after — and names it by the record's
`build.originalName`: it never makes up a path or a name. That name must
be the one its source record has, or gets — the record logic names a
source's file from the name it arrived under (`original.<ext>`) — else
the run fails before anything is built.

**No name a file arrived under** is kept anywhere in the record: not as a
file's or a folder's name, not in a record, not in a package. A source
record names its original `original.<ext>`, keeps what the name it arrived
under claims (its labels, its numbering) but never the name, says nothing
of where the file came from, and keeps no container title; its
`ffprobe.json` is the tool's output with the file named so too and no
title tag. Of what came with the original, only its subtitle files are
copied, as `subtitle-<n>.<lang>[.forced][.sdh].<ext>`. A file that holds
several episodes covers them (`covers`) as the record's
`library.source.covers` lists them, in episode order, this episode — their
holder — first; a list that isn't one of distinct episode ids beginning
with the item's own fails the step, and a movie's file covers none.

Each folder is written once, built in staging and renamed into place in
one step, so a reader sees all of it or nothing. A run:

1. removes what an earlier run of the version left in its staging folder
   (an original in it goes back to its arrival first, below) and writes
   the sentinel `.packaging` (`{startedAt, pid, host}`);
2. builds the package in `version/`, as above (`hls/`, `subs/`,
   `trickplay/`);
3. unless the source is recorded already, builds `source/`: the
   original's probe (`ffprobe.json`), a copy of each subtitle file the
   record names that is there, numbered from 1 in the record's order
   under the name the library gives it, `source.json`, and
   `checksums.sha256` last;
4. writes `version.json`: the catalog's chapters (else the original's
   own) and detected ranges, the edition the name it arrived under
   claims, the presentation and runtime the probe says, and the original
   it keeps (`originalFiles`), for an establish or a takein;
5. closes the chain: `checksums.sha256` over `version.json` and every
   package file — never the original, whose fixity is its source
   record's — `package.json` with the checksums file's hash, then
   `.complete` with `sha256:<hex of package.json>`; then checks that the
   package is whole (as above) and that the chain holds;
6. renames `source/` into `sources/<sourceId>/` (one there with its
   checksums stays as it is; one there without them fails the run), then,
   for an establish or a takein, the original into `version/`, last, then
   `version/` into `versions/<versionId>/`;
7. hands the version to the catalog (`POST /api/items/{id}/packaging-complete`
   with `layout: v2`: the version and package ids, `versionDir`, the
   `.complete` value, `package.json` as written, the source id, the
   sidecars — each subtitle file's catalog id mapped to the rendition made
   from it — the source block, and `original: {path, name}` when the run
   renamed the original into the version folder, for the catalog to move
   the item's path there as it records the version). Only a 2xx makes the
   step `done`; then the staging folder and the handoff go. A refusal
   (409: a stale version; 422: a broken chain) fails the step and leaves
   the version where it is.

A **takein** writes steps 1, 3, 4 and 6 only, and hands over the version
with `takenIn: true` and no package (no `packageId`, `complete` or
`package`). An **add** reads the version folder's `version.json` and
never writes it again: it builds the package in `version/` beside a copy
of it, closes the chain over the two, then renames `hls/`, `subs/` and
`trickplay/` into the version folder, then `checksums.sha256`,
`package.json` and `.complete`, in that order, and checks the chain there;
a rename that fails moves back what it moved. The version folder must be
the one that keeps the original the record names, and hold nothing else.

**The original** is renamed, never copied: from its arrival into the
staged version folder as the last step before that folder is renamed into
the record, so the record gets the version and its original in one step.
The arrivals must be on the library's share, where the packager may
rename. Before the rename the run notes where the original came from
(`.original`, in the staging folder); a run that fails after it puts the
original back there, and so do the next run of the version and the
startup sweep for a run that died. A staging folder that still holds an
original — one that can't be put back, as its arrival path is taken —
is never removed: the run fails, and the folder stays for an operator. A
package is `derived` when its version folder keeps the original beside it
(an establish, an add) and `canonical` when it is the only copy (a
repackage, an extra).

A run that fails removes its staging folder; one that dies leaves it, and
the next run of the version starts clean. A run that died between the two
renames finds the source in place and builds only the version. A version
folder already in place and whole is the work of a run whose handover was
lost: the next run hands it over again as it is, without building
anything — with the original's new place, for an establish or a takein,
though the record still names its arrival. An add that died between its
renames is completed from what it left in staging, when that is the rest
of its package and the chain then holds. Nothing in the record is ever
written over: a version or extra folder that is there but not whole fails
the run. The original must be the file the catalog recorded at its arrival
(its size and `qh1`), else the run fails.

**Takein jobs** come on the item topic too, the envelope's `step`
`"takein"`, for a title the catalog takes in without a package: its
transcode refused, its transcode or package out of attempts, an admin's
word. Their step is `takein` (`PUT /api/analyze/items/{id}/steps/takein`),
and so is their guard: a finished takein is only acked. A takein has no
transcode, so no takein job is a stale handover. A package job whose
record says `takein`, and a takein job whose record says another mode, are
only acked: the catalog sends the job its record names.

**The records' contents** are the schemas repository's record logic,
`src/packager/libv2_records.py`, vendored byte for byte (the migration
writes its records with the same code, and the deletion gate is computed
from them); `tests/test_libv2_records.py` pins its hash and compares the
packager's own records against the golden ones of the same commit. To take
a new copy, copy `tools/libv2_records.py` and `tools/testdata/libv2_records/`
from the schemas repository at one commit and update the test's values.
The packager adds what only it knows: which stream of the original each
rendition was made from. A subtitle made from a file that came with the
original names its copy (`fromSidecar: "sources/<sourceId>/subtitle-<n>…"`):
the file the copy was made from, or the copy itself, by its path, else a
copy of the same bytes — never by a name. No subtitle is `default` in
`package.json`, as no subtitle is DEFAULT=YES in the master: the record
forbids a forced track flagged default, which `manifest.json` uses to say
"show it by itself".

**HEVC only.** A v2 package's video is HEVC: the original's copied, or
the transcoder's HEVC encode of it. A v0 in another codec fails the run;
a lower rung in another is left out.

**Inputs.** The transcoder's handoff in the record's `inboxDir`; else one
it left in the package store's `_inbox/<itemId>/` (`_inbox/extra-<extraId>/`)
for a transcode that finished before the layout switched; else the
original — but never while the item's transcode step says `done`: its
handoff is gone, and the run fails. The subtitle files the record names
are taken beside the original or in its source record's folder, which
keeps a copy of each that came with it: where they are once the original
is in a version folder.

**Extras** go into their title's `extras/<extraId>/` the same way, built
in `extra/` of their staging folder: `extra.json` (what the catalog took
the extra in as — a title the catalog gives none of is its kind's word —
the packager's probe of its file, and that file in `packagedFrom`, named
as the library names an original; the folder keeps no original), the
package (no
trickplay), the chain over `extra.json` and the package, one rename. Their
handover is `POST /api/extras/{id}/packaging-complete` with `layout: v2`.

**Startup sweep.** Besides the package store, the sweep at startup walks
`<WORK_ROOT>/staging/` only — never the library — and removes the entries
of runs that started more than a day ago, once the original such a run
left in one is back at its arrival; one that still holds an original
stays.

## Layout

```
src/packager/main.py        # entry point: consumer threads + FastAPI /healthz + /readyz
src/packager/config.py      # env-driven config
src/packager/events.py      # Kafka consumer factory + envelope parsing
src/packager/katalog.py     # HTTP client to the katalog API, OIDC auth
src/packager/worker.py      # consumer loop: one item at a time, serial packaging
src/packager/extras.py      # the extras' consumer loop: trailers and other bonus material
src/packager/renditions.py  # reads the transcoder handoff (renditions.json)
src/packager/packager.py    # ffmpeg remux + shaka-packager + trickplay + subtitles
src/packager/hls.py         # master playlist assembly + RFC 8216 bit rates
src/packager/library.py     # library v2: staging, the chain, the renames into the record
src/packager/records.py     # library v2: what source.json, version.json, package.json, extra.json say
src/packager/libv2_records.py  # the schemas repository's record logic, vendored byte for byte
scripts/                    # one-off backfill / diagnostics helpers
k8s/                        # Deployment, Service, ServiceAccount, ServiceMonitor, GrafanaDashboard
Dockerfile
```

## Configuration

| Variable | Default | Description |
| --- | --- | --- |
| `KATALOG_API_URL`, `OIDC_*` | (required) | katalog API + client credentials |
| `KAFKA_BROKERS` | `kafka:9092` | Bootstrap brokers (`KAFKA_SECURITY_PROTOCOL`, `KAFKA_GROUP_ID`, `CONSUME_TOPIC`) |
| `KAFKA_TOPIC_PREFIX` | `stube.` | Tenant topic prefix of the extras' topic, `<prefix>catalog.extra.transcoded` (see [Extras](#extras)) |
| `EXTRAS_GROUP_ID` | `packager-extras` | The extras' consumer group |
| `SEGMENT_SECONDS` | `6` | Segment length when renditions.json doesn't set it |
| `SURROUND_AUDIO` | `off` | 5.1 companion codec: `eac3`, `ac3` or `off` (see "Audio"). chino-stream serves the `audio-surround` group only to clients that decode it |
| `SURROUND_BITRATE` | `448k` | Bitrate of an encoded 5.1 companion |
| `HLS_SUBTITLES` | `false` | Reference the WebVTT renditions from the master |
| `PREFERRED_LANGUAGES` | (empty) | DEFAULT=YES language order, e.g. `de,en`; empty = whitelist order |
| `OLD_PACKAGE_GRACE_SECONDS` | `600` | How long a package replaced by a new one stays on disk for the requests that started on it (see "Packaging again"); keep it well above the NFS mounts' attribute cache time |
| `WORK_ROOT` | `/var/lib/katalog/.work` | The library v2 work tree, on the library's share: the startup sweep clears dead runs in its `staging/` (see [Library v2](#library-v2)). A run's own paths come from its worker record |

## Local development

```bash
pip install -e '.[dev]'
pytest
```

Unit tests need nothing installed. `tests/test_package_real.py`
packages a generated clip end to end when `ffmpeg`, `ffprobe` and
shaka-packager's `packager` are on `PATH`; `tests/test_library_real.py`
does so into the library v2 tree (an HEVC clip, so ffmpeg needs
libx265). It also validates the tree it writes with the schemas
repository's `validate-library-v2.py --check-checksums` when a checkout
of it is beside this one (or `ZAENTRUM_SCHEMAS` names one) whose schemas
know the platform's additive package and extra fields, and a Python with
`jsonschema[format-nongpl]` and `referencing` can run it (this one, or
`LIBRARY_V2_PYTHON`). `tests/test_library_modes_real.py` runs each build
mode on such a clip, a takein with `ffmpeg` and `ffprobe` alone, finds no
name the clip arrived under anywhere in the title's folder or its
handovers, and validates each tree the same way.

## Build the container

```bash
docker build -t zaentrum/packager .
```

Build and push the image to your own registry and update the image
reference in `k8s/deployment.yaml` for your environment. The deployment
expects two PVCs (read-only source media, writeable packaged output),
the `KATALOG_API_URL` / `OIDC_*` env vars wired to your katalog API and
identity provider, and `KAFKA_BROKERS` (+ optional `KAFKA_SECURITY_PROTOCOL`,
`KAFKA_GROUP_ID`, `CONSUME_TOPIC`, `KAFKA_TOPIC_PREFIX`, `EXTRAS_GROUP_ID`)
pointing at your broker. Mount the directory the catalog takes extras
from too, when it lies outside the media root ([Extras](#extras)).

## License

[MPL-2.0](LICENSE).
