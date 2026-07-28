# Exercise demo media — ops runbook

How the demo animations get from the dataset into the Agent. Spec:
[docs/spec.md — Exercise demo media](spec.md#exercise-demo-media); rights gap
knowingly accepted by the owner on [#15](https://github.com/ivzc07/agentg/issues/15)
(the dataset's media is © Gymvisual; reusers are told to buy their own license).

The code side (catalog wiring, resolution, the file_id cache, delivery) ships
in the app. The three steps below are **ops**: they produce the MP4s and the
manifest the app ingests. They run once per catalog refresh, not per deploy.

## 1. Clone the dataset

The free [hasaneyldrm/exercises-dataset](https://github.com/hasaneyldrm/exercises-dataset)
— a JSON index plus 1,324 GIFs (180×180). Clone it somewhere on the box.

## 2. Transcode each GIF to a soundless H.264 MP4

Telegram re-encodes GIFs to MP4 anyway; author the MP4 directly to skip the
lossy step. Run the build script over the whole dataset:

```
python -m agentg.scripts.build_demo_media <dataset_dir> <media_root> [--jobs N]
```

It transcodes every GIF with the recipe below (in parallel, skipping MP4s
that already exist, so re-runs are resumable), writes the MP4s under the
media root, and writes `manifest.json` next to them — step 3's input. Slugs
are `<dataset-id>-<name>.mp4`, anchored on the dataset id so re-runs never
rename a file. The script also wires the seed catalog's staples (bench press,
squat, ...) onto their dataset counterparts, so ingest resolves onto the
existing rows instead of creating near-duplicates. Per-file equivalent:

```
ffmpeg -i <src>.gif -an -vf "scale=720:-2" -c:v libx264 -pix_fmt yuv420p \
  -crf 27 -movflags +faststart <slug>.mp4
```

Put the MP4s under the app's `DEMO_MEDIA_ROOT` (default `/data/demos`, a
Coolify persistent volume). These files are the **system of record** — served
only through the Agent, never as public downloads (the license permits app use,
not redistribution).

## 3. Build a manifest and ingest it

A manifest maps each Exercise name to its MP4 slug (filename under the media
root):

```json
[
  { "name": "goblet squat", "slug": "goblet-squat.mp4" },
  { "name": "barbell bench press", "slug": "bench-press.mp4" }
]
```

Ingest wires those slugs onto the Exercise catalog, creating Exercises the seed
set doesn't already have (`agentg.demo_ingest.ingest_demo_manifest`):

```
python -m agentg.scripts.ingest_demos /data/demos/manifest.json
```

Re-running is idempotent: an Exercise already wired to a slug is left as-is;
changing a slug drops the stale file_id cache so the new media uploads on the
next send.

## Delivery and the file_id cache

The Agent's `show_demo` tool queues an Exercise; the Telegram adapter sends it
with `sendAnimation` (autoplaying, looping, muted). The first send uploads the
MP4 and caches the returned `file_id`; every later send resends by that id with
no upload. file_ids are **per bot** — a token migration simply misses the cache
and re-uploads, because the canonical MP4 still lives in our store.

## Per-gym overrides

A Coach's own filmed clip is transcoded the same way, dropped under the media
root, and wired with `DemoStore.set_override(gym_id, exercise, slug)` — it wins
over the Exercise default for that Gym's Members only.
