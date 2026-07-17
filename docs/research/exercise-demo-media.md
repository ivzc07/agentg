# Research: sourcing exercise demo videos/GIFs

Resolves [#15 — Source exercise demo videos/GIFs](https://github.com/ivzc07/agentg/issues/15).
Question: every Exercise carries a short demo (video/GIF) the Agent sends to show how it's done.
Where does that content come from, what does it cost legally and financially, and how does
Telegram deliver it? Researched 2026-07-17 against primary sources (license pages, API docs,
live API counts, Telegram Bot API reference).

## TL;DR

**Buy the demo animations from Gymvisual (one-time, ~$0.90/GIF in bulk), convert each to a
short soundless H.264 MP4, host the source files ourselves, and deliver via Telegram
`sendAnimation` with a per-Exercise cached `file_id`.** There is no large, genuinely open
library of *animated* exercise demos: the free-GIF-API ecosystem sits on contested rights
that trace back to Gymvisual itself, and the APIs with real commercial licenses either
forbid storing/re-sending media (MuscleWiki — incompatible with how Telegram works) or are
negotiated enterprise deals (ExRx). A ~200-exercise library is a **one-time ~$180–300 line
item** with clean, perpetual, attribution-free rights. Per-gym custom demo overrides are
cheap to allow (see [Per-gym overrides](#per-gym-overrides)).

One caveat to close before purchase: Gymvisual's license enumerates apps/websites/books but
predates chat bots — confirm with them that delivery through a Telegram bot counts as app
use (tracked as its own task ticket). Fallback if they say no: MoveKit (~170 clips,
plain-English commercial license, covers the core lifts).

## Sources compared

| Source | Media | Coverage | Media license | Verdict |
|---|---|---|---|---|
| [Gymvisual](https://gymvisual.com) | Animated GIFs + videos (illustration style) | Thousands; all common lifts | [Non-exclusive commercial royalty-free](https://gymvisual.com/content/9-license), one-time, perpetual, no attribution | **Recommended** — license from the actual copyright holder |
| [MoveKit](https://movekit.com) | 3D-mannequin MP4 loops | ~170 clips; core lifts only | Plain-English commercial (apps/websites/videos) | **Fallback** — nicer style, small library |
| [MuscleWiki API](https://api.musclewiki.com) | Real-human video, 2 angles, M/F | 1,900+ exercises, 7,500+ videos | Paid commercial, but [stream-only](https://musclewiki.com/api-terms): no download/store/re-host | Best quality, but terms conflict with Telegram delivery |
| [ExRx.net](https://exrx.net/Store/Other/APIFAQ) | Real-human video (Vimeo-hosted) | 2,100+ exercises | Proprietary, negotiated API subscription | Viable but enterprise-shaped; pricing on request |
| [wger](https://wger.de) | Mostly static PNGs; some video | 842 exercises, **only 78 videos** (live API count) | CC-BY-SA 3.0/4.0 per item; attribution metadata often incomplete | Open but video coverage far too thin |
| [free-exercise-db](https://github.com/yuhonas/free-exercise-db) | 2 static JPGs per exercise | 800+ exercises | Unlicense claimed; photo provenance [undocumented](https://github.com/yuhonas/free-exercise-db/issues/2) | Static only — doesn't answer "show how it's done" |
| ExerciseDB / [oss.exercisedb.dev](https://oss.exercisedb.dev/docs) / [WorkoutX](https://workoutxapp.com) | Animated GIFs (180p free tier) | ~1,300–1,500 exercises | Contested — see below | **Avoid** — highest infringement exposure |

### Why the "free" GIF APIs are ruled out

The ExerciseDB corpus (~1,324 GIFs, cartoon figure on white background) is sold under at
least three brand names (RapidAPI ExerciseDB, exercisedb.io, WorkoutX). exercisedb.io's
[FAQ](https://exercisedb.io/faq) claims to be "the original creator and owner" of the GIFs,
while an independent mirror of the same corpus
([hasaneyldrm/exercises-dataset](https://github.com/hasaneyldrm/exercises-dataset)) states
the media "is © Gym visual and redistributed here with permission" and tells users to buy
their own Gymvisual license before reuse. The art style matches Gymvisual's catalog. Two
distributors, incompatible ownership claims, no documented chain of rights, no open license
anywhere — for a commercial product this is uninsurable risk, especially when licensing the
apparent upstream (Gymvisual) directly costs under a dollar per exercise.

### Why not MuscleWiki, despite the best videos

MuscleWiki's paid API grants real commercial rights, but its
[Additional Terms](https://musclewiki.com/api-terms) require all playback to happen inside
your own application via API-streamed URLs, with transient caching only and no
download/export/re-hosting. Sending a video into a Telegram chat uploads a permanent copy
to Telegram's servers — the opposite of stream-only. Without a negotiated exception this
use is out of bounds, and scraping the site instead is likewise prohibited.

### Why not the open sources

wger's content is honestly licensed (CC-BY-SA per item) but has 78 videos across 842
exercises (live API counts, 2026-07-17) — a typical barbell/machine routine would mostly
get static illustrations, and per-item attribution metadata is frequently empty.
free-exercise-db is static two-position photos under an unverified public-domain claim.
Neither delivers a moving demo.

## Telegram delivery

All from the official [Bot API reference](https://core.telegram.org/bots/api) and
[Bots FAQ](https://core.telegram.org/bots/faq); this constrains the asset pipeline, not the
source choice.

- **Method**: [`sendAnimation`](https://core.telegram.org/bots/api#sendanimation) — for
  "GIF or H.264/MPEG-4 AVC video without sound". Renders as autoplaying, looping, muted
  media in the chat: exactly the demo UX. `sendVideo` renders a play-button bubble and
  doesn't loop; `sendDocument` renders a file attachment. Caption rides along for form cues.
- **Author MP4, not GIF**: Telegram [re-encodes all GIFs to MP4](https://telegram.org/blog/gif-revolution)
  anyway (~95% smaller). Encode soundless H.264 directly and skip the lossy transcode:
  `ffmpeg -i src -an -vf "scale=720:-2" -c:v libx264 -pix_fmt yuv420p -crf 27 -movflags +faststart demo.mp4`.
  Target ~5–10 s loop, 480–720 px, ~1–3 MB — near-instant on mobile data, nowhere near limits.
- **Limits**: 50 MB per multipart upload; 20 MB when Telegram fetches a URL; **no limit when
  resending by `file_id`** ([Sending files](https://core.telegram.org/bots/api#sending-files)).
  A self-hosted Bot API server (2 GB uploads) is unnecessary for this feature.
- **Caching — the key mechanic**: upload each clip once, read `message.animation.file_id`
  from the response, persist it, and every later send to any Member's chat passes the stored
  string — no bytes re-transferred. Per the FAQ, "file_ids can be treated as persistent"
  (no expiry) but are **unique per bot**: a bot-token migration invalidates them all, so the
  canonical asset must live in our own storage and file_ids are a lazily-seeded cache, never
  the system of record. Store `file_unique_id` alongside for identity/dedup.
- **aiogram v3** exposes this directly: `bot.send_animation(chat_id, animation=<file_id str
  or FSInputFile>)`; its own docs say "once you upload a file, save its file_id and reuse
  that later".
- **Rate limits** for proactive broadcasts: ≤1 message/s per chat, ~30 messages/s overall.

## Recommended pipeline

1. **Buy** the Gymvisual animations for the exercise catalog (GIFs $3.60 each, **$0.90 from
   10+**; videos $6 from 5+ — [price rules](https://gymvisual.com/content/6-price-rules));
   a ~200-exercise v1 catalog ≈ $180 one-time. Bulk packs available on contact.
2. **Transcode** each to a soundless MP4 with the ffmpeg recipe above; keep originals.
3. **Store** originals + MP4s in our own object storage — the license permits app use but
   not redistributing the raw files, so assets are served only through the Agent, never as
   public downloads.
4. **Deliver** via `sendAnimation`; lazily seed and cache `telegram_file_id` per demo asset
   (columns: asset id, `telegram_file_id`, `file_unique_id`, bot id, uploaded at).

## Per-gym overrides

Cheap, and worth allowing from day one. The demo is a media asset referenced by an
Exercise; an override is the same asset type scoped to a Gym — resolution is
"Gym-scoped demo if present, else Exercise default". A Coach films their own squat demo,
it lands in the same storage + file_id cache path, and only that Gym's Members see it.
Uploaded coach videos may carry sound; strip audio in the same transcode step so everything
delivers as an animation. No licensing question: per-gym media is the gym's own content.

## Costs summary

- Gymvisual media: one-time ~$180–300 for a v1 catalog; ~$0.90 per exercise added later.
- Hosting: negligible (a few hundred MB of MP4s in object storage).
- Telegram delivery: free; file_id caching means each asset uploads once per bot.
- No recurring API subscription, no per-request fees, no attribution obligations.
