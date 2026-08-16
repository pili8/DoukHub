# TTD Collection UI Roadmap Design

- Date: 2026-08-16
- Status: Ready for review
- Scope: DoukHub positioning, collection feature phases, phase one single-work closed loop

## 1. Background

DoukHub already contains an account workflow, TTD service management, a persistent account batch downloader, and a basic single-work page. TTD itself supports more collection and data functions than DoukHub currently exposes.

The user does not want to operate TTD through API documentation or terminal menus. DoukHub is therefore positioned as both:

1. The management console for TTD services, accounts, tasks, and configuration.
2. The graphical UI for TTD functions.

Where TTD's Web API can fulfill an action, DoukHub calls that API. Where the action only exists in TTD's terminal workflow, DoukHub launches and supervises a TTD process while presenting the operation as a normal web task.

## 2. Product Direction

DoukHub must not merely expose API fields. It translates TTD capability into task-oriented pages, forms, progress, logs, cancel/retry actions, file paths, and readable errors.

The priority is:

1. Single-work collection.
2. Account batch collection.
3. Live collection.
4. Remaining TTD capabilities.

Navigation entries appear only when their underlying function is ready. The exception is live collection, which may appear earlier as a clearly marked reserved submenu because live download is already a confirmed phase-three target.

## 3. Phase Roadmap

### 3.1 Phase One: Single-Work Closed Loop

Goal: paste one or more Douyin/TikTok work links and complete retrieval without leaving DoukHub.

Scope:

- Support ordinary videos.
- Support Douyin and TikTok image sets.
- Support Douyin live-photo works.
- Support animated works represented by TTD as video files.
- Preserve per-asset type and order instead of flattening every URL into one list.
- Allow users to copy, open, and directly download individual asset URLs.
- Allow a work-level filename override while retaining the template-generated default.
- Add a reusable filename template library.
- Add a drag-and-drop template builder with live preview.
- Persist a default save directory and recent directories.
- Record lightweight download history with file paths, status, time, and errors.
- Allow retrying failed downloads.

Non-goals:

- No GIF conversion.
- No multi-threaded custom download kernel for small ad-hoc downloads.
- No cloud synchronization of history.
- No global work metadata database.
- Batch account downloads continue using TTD's downloader and archive behavior.

### 3.2 Phase Two: Account Batch Collection

Goal: complete the existing account batch workflow.

Scope:

- Make the account collection type effective: published works, liked works, and collected works.
- Apply date-window and incremental strategy by collection type.
- Continue using one supervised TTD terminal process at a time.
- Represent account progress, logs, cancellation, retry, and confirmed successes accurately.
- Connect DoukHub filename templates to TTD `name_format` where useful without duplicating TTD's downloader.
- Keep TTD `download_data` and archive deduplication under TTD ownership.

### 3.3 Phase Three: Live Collection

Goal: provide a complete live-collection UI rather than only a link parser.

Expected progression:

1. Add the live submenu.
2. Resolve live links and show room state.
3. Show available qualities and stream URLs.
4. Copy URLs and generate ffmpeg commands.
5. Detect ffmpeg availability.
6. Start, monitor, and stop managed recording processes.
7. Record output paths, elapsed time, process status, and errors.

Live recording is a long-running process and should reuse lessons from the account batch process manager rather than the synchronous single-work request flow.

### 3.4 Phase Four: Remaining TTD Functions

Goal: gradually turn other TTD terminal or API capabilities into normal DoukHub UI.

File-oriented functions:

- Douyin and TikTok collections/playlists.
- Logged-in Douyin collected works.
- Favorites folders.
- Collected music.

Data-export functions:

- Comments and replies.
- General, video, user, and live search.
- Douyin hot boards.

File-oriented functions can reuse batch task management. Data-export functions should focus first on structured result display and local export, then optional Feishu integration.

## 4. Phase One Data Design

### 4.1 Work Assets

A resolved work exposes a normalized asset list. Each asset contains:

- `kind`: `video`, `image`, `live_photo`, `music`, `static_cover`, or `dynamic_cover`.
- `index`: stable display and filename index.
- `url`: downloadable URL.
- Optional paired URLs for a live-photo asset.

Existing `downloads` behavior may remain as a compatibility field while the UI and downloader migrate to assets.

TTD types map as follows:

- Video work: one or more video URLs.
- Image set: ordered image URLs.
- Live-photo work: ordered live-photo assets represented by TTD as MP4 output.
- Animated work: video output retained as MP4; no GIF conversion.

### 4.2 Filename Templates

Templates are user-defined and reusable. Each template contains:

- Unique ID.
- Name.
- Ordered parts.
- Default flag.
- Creation and update times.

Parts are field tokens or literal separator text. Supported initial fields are:

- `create_time`
- `author`
- `title`
- `id`
- `type`
- `platform`

The builder supports dragging field order, inserting separator text, live preview, saving, renaming, deletion, and setting one default. A work-level filename override replaces the generated stem but not the extension.

All templates are sanitized through the existing filename rules. Path separators, absolute paths, malformed format fields, and template syntax are rejected.

### 4.3 Save Preferences

DoukHub persists:

- Default save directory.
- Recent directories in most-recent-first order.
- Default filename template ID.

The single-work form defaults to the default directory and template while allowing per-run changes.

### 4.4 Download History

Add a local history table for single-work downloads with:

- Work ID and source link.
- Platform and work type.
- Title and author.
- Template used and optional filename override.
- Save directory and output file paths.
- Status and error message.
- Created and updated times.

History is one work action per row, with file paths stored as JSON when a work produces multiple files. Failed actions retain enough normalized work data to retry without requiring the user to paste the source link again.

## 5. Phase One Interface Design

The single-work page keeps its current two-step model:

1. Paste links and resolve.
2. Review assets, edit overrides, then download.

Resolved results show:

- Work title, author, publish time, platform, and type.
- Asset kind and count.
- Per-asset actions: copy URL, open URL, download.
- Work-level filename override and generated filename preview.

The template builder is reachable from the single-work page. It edits templates without forcing the user to understand TTD's `name_format` syntax.

Download history appears below or beside the active form, showing recent outcomes and a retry action for failures.

## 6. Error Handling

- Unsupported or unrecognized links are reported individually and do not abort the batch.
- TTD unavailable state gives an actionable service error.
- Missing asset URLs are shown per asset.
- A failed file is removed from its temporary path.
- Existing target files are deduplicated with a stable suffix rather than overwritten.
- Filename validation rejects unsafe templates before any network request or file write.
- A retry reuses stored work data and user preferences.

## 7. Verification Plan

Unit tests:

- TTD type normalization to assets.
- Live-photo and image-set ordering.
- Filename template construction and sanitization.
- Unsafe and malformed template rejection.
- Save preference defaults and recent-directory ordering.
- Download history creation, failure recording, and retry data.

Integration tests:

- Resolve and download mocked video, image-set, and live-photo works.
- Direct-download of one selected asset.
- Work-level filename override.
- Persisting selected directory and template.
- Retry of a previously failed work.

Manual checks:

- Resolve real Douyin video, image-set, live-photo, and animated links.
- Verify actual output extensions and filenames on Windows.
- Verify duplicate filename handling.
- Verify template drag-and-drop and preview.
- Verify recent history and file paths.

## 8. Implementation Notes

- Keep TTD Web API as the metadata source for phase one.
- Keep single-work downloads outside TTD's `download_data`.
- Keep the existing temporary-file and atomic-replace behavior.
- Add only the persistence and API surface needed by phase one.
- Do not pre-build empty pages for phase four.
