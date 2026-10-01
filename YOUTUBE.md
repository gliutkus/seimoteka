# YouTube appearances: project assessment and proposed ingestion

Prepared 2026-09-11. This is a design, not an implemented importer. No YouTube
transcripts have been imported or their availability measured in this review.

## Fit with the existing project

The executable code is currently `ingest/config.py`, `http.py`, and `recon.py`.
The repository contains real reconnaissance samples and the findings in
`PHASE1.md` and `SOURCES.md`; README's claim that recon has not run is stale.
There are no database loaders, retrieval implementation, or UI in this tree.
Database contents were not inspected during this assessment.

Keep the existing single Postgres architecture. `documents` and `chunks` already
provide source URLs, document types, dates, and a nullable `speaker_mp_id`.
YouTube requires timestamps, transcript provenance, and explicit attribution
review on top of this foundation.

The user's request extends the original official-documents-only scope in
the project brief: a recording can support what an MP said during an interview.
It does not independently establish the truth of allegations made in that
interview. Keep these as `youtube_interview` documents and distinguish them
from official findings in retrieval and answers. Host commentary must never
become a statement attributed to an MP.

## Scope and discovery

Use term 10 (2024–2028) as the initial scope, consistent with the project.
Load every member returned by the official term roster, including members who
left during the term. Preserve mandate dates and historical fraction membership.
The reconnaissance reports 148 term records; do not interpret that as the
number of simultaneously sitting MPs. A future multi-term importer needs a
separate person/mandate model because the current `mps` primary key is person ID.

Start with [OpTV](https://www.youtube.com/@OpTV_jusu_tiesa). Resolve its stable
channel ID through the API before saving channel configuration. The web page
was reachable, but this review did not enumerate uploads or verify captions.

Use two discovery paths:

1. Enumerate configured channels' uploads playlists, page through the chosen
   date range, and checkpoint progress. Start with OpTV; accept additional
   channels through the same configuration and ingestion path.
2. Search across YouTube for every roster member, using canonical full names
   and reviewed Lithuanian variants. Rotate uniformly through MPs under the
   available quota. Allow direct video URL submissions to recover missed items.

The [YouTube channel guide](https://developers.google.com/youtube/v3/guides/implementation/channels)
documents the uploads playlist, and [playlistItems.list](https://developers.google.com/youtube/v3/docs/playlistItems/list)
supports pagination. [Search documentation](https://developers.google.com/youtube/v3/docs/search/list)
warns of incomplete results and directs channel upload discovery to playlists.
Search is supplementary discovery, not proof of exhaustive coverage.

Metadata matching creates appearance candidates. It does not establish that
someone appeared: a title may discuss them without including them. Conversely,
an appearance can be missed when no name occurs in metadata. Audit samples of
unmatched channel episodes to measure this blind spot.

Never hardcode a subset of politicians. Include all MPs in coverage reports,
even with zero discovered appearances. Show searched date ranges, channels,
last checks, candidate episodes, confirmed appearances, missing transcripts,
and unresolved speakers. Say “no appearance found in indexed sources,” not
“this MP never discussed the subject.”

## Transcript acquisition

[YouTube Help](https://support.google.com/youtube/answer/6373554?hl=lt) currently
lists Lithuanian automatic captions for uploaded videos. Availability and
accuracy vary per video; overlapping speech is a documented problem.

Prefer original Lithuanian creator captions, then original-language automatic
captions. Preserve language, whether generated or translated, track identity,
raw timings, retrieval time, and a content hash. Do not silently substitute an
English translation. Keep the original alongside any translation.

The official [captions.download API](https://developers.google.com/youtube/v3/docs/captions/download)
requires permission to edit the video. A public watch URL and API key do not
grant that permission. Creator-supplied SRT/VTT files are another ingestion path.

An optional [youtube-transcript-api](https://github.com/jdepoix/youtube-transcript-api)
adapter can test public caption retrieval; its maintainer documents request/IP
blocking, particularly on cloud hosts. Treat it as replaceable and record
blocked, unavailable, and retryable failures separately. Avoid unbounded retries.

When captions are missing or unusable, use speech recognition on an authorized
audio source, such as a recording supplied by the publisher. Benchmark Lithuanian
names, numbers, negation, and overlapping speech before selecting a model or
estimating archive costs. Store model/version and preserve raw output separately
from corrections. Never fill inaudible gaps with inferred text.

## Speaker attribution and retrieval

Speaker diarization divides audio into speaker turns; assigning a turn to a
named MP is a separate task. For the pilot, use human-reviewed identification
from introductions, captions identifying guests, and the recording itself.
Do not infer identity from a voice alone or the order of names in the title.

Keep episode participants, mentions of people, and actual segment speakers as
separate relations. Hosts, guests outside parliament, inserted clips, and unknown
speakers are expected. Leave MP attribution NULL until supported and reviewed.
Overlapping or mixed-speaker spans must not receive a single MP attribution.

Chunk contiguous turns by speaker and topic, retaining nearby questions as
separately labelled context. Derive time links such as `watch?v=VIDEO_ID&t=123s`
from stored start times. Direct quotations require checked wording and speaker
identity; otherwise present appropriately labelled transcript summaries.

For “what did MP X say,” filter on verified segment attribution, not on an
episode participant list. Treat transcript text as untrusted evidence, never
instructions. Statements about third parties remain attributed claims.

## Proposed database additions

These are design requirements; implement them through migrations.

| Entity | Essential data |
| --- | --- |
| `youtube_channels` | Stable channel ID, display name, handle, enabled flag, crawl state |
| `youtube_videos` | Video ID, channel ID, document ID, title, published time, duration, recording date if evidenced, availability |
| `youtube_appearance_candidates` | Video ID, MP ID, discovery evidence, candidate/confirmed/rejected state, review provenance |
| `transcript_versions` | Video ID, source/track, language, generated/translated flags, model version, raw artifact, hash, retrieval time, active version |
| `transcript_speakers` | Version ID, local speaker label, optional MP ID, role, identity evidence and review state |
| `transcript_segments` | Version ID, ordinal, start/end milliseconds, speaker ID, raw text, corrected text, text review state |
| `ingestion_jobs` | Stage, target, attempt count, next retry time, result/error, checkpoint |

Add transcript-version references, start/end times, and attribution state to
retrieval chunks. Enforce nonnegative valid intervals and unique segment
ordinals per version. Activate a new transcript and its chunks transactionally
so reprocessing cannot leave stale duplicate quotations in retrieval. Preserve
review history when transcripts change; do not transfer reviews blindly.

Deduplicate by video ID, not URL form. Link clips/reuploads to the same underlying
appearance when verified; repeated uploads are not independent evidence.
Keep publication and recording dates distinct, and leave recording date unknown
when unsupported. Never derive historical party membership from today's value.

Use provider-specific request handling: the existing HTTP helper assumes text
responses and permanent URL caching, which is insufficient for changing upload
lists, credential-bearing requests, and audio. Preserve its rate-limit discipline
while adding bounded retries, refresh semantics, and secret-free cache identities.
Keep YouTube dependencies in an optional `youtube` dependency group, managed by uv.

## First implementation slice and acceptance checks

1. Load the verified Seimas roster and date-aware identities.
2. Add migrations and an offline timestamped SRT/VTT import path with provenance.
3. Inspect a pilot of roughly 10–20 OpTV episodes plus several from other
   channels, using the same MP inclusion rules. Capture actual metadata and
   caption samples before writing provider-specific parsers.
4. Implement configurable upload discovery and a replaceable caption adapter.
5. Review pilot speaker assignments and quotations, then generate chunks and a
   coverage report. Measure missing-caption rate and review minutes per episode.
6. Only then expand the archive, add cross-channel MP searches, and benchmark
   speech recognition for missing captions. No ongoing scheduler is created by
   this design document.

Verify idempotent imports; URL deduplication; malformed and overlapping timings;
inflected names and surname collisions; mentions without appearances; host and
guest separation; clips with additional speakers; unavailable captions; bounded
retry behavior; transcript revisions; temporal membership; exact time citations;
and zero-coverage MPs remaining visible. Manually check all attributed quotations
in the pilot against the recordings before exposing them as evidence.

The next executable milestone is one real episode with a versioned transcript,
reviewed MP turns, and working timestamp citations. Archive size, actual caption
coverage, speech-recognition quality, and operating cost remain unmeasured.
