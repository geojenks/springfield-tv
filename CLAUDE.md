# CLAUDE.md — Springfield TV

## Goal
A small physical CRT-style TV that plays the TV-within-the-show clips from *The Simpsons* (mainly Itchy & Scratchy; later Krusty, Troy McClure, Kent Brockman, school infomercials). Channel dial scrolls left/right or shuffles. Browser version exists; hardware (Raspberry Pi Zero 2 W + small display + rotary encoder, not Arduino — it can't decode video) comes later.

## Repo layout
```
catalog/itchy_scratchy.csv   120 I&S shorts, Ullman shorts → S36. start/end columns are EMPTY.
scripts/fill_episode_numbers.py   fills `episode` via TVmaze API (run once)
scripts/scan_episodes.py          finds candidate segments in episode files → work/candidates.csv + review.html
scripts/extract_clips.py          ffmpeg cutter; takes a catalog CSV or work/segments_reviewed.csv, writes clips/index.csv
scripts/review_server.py + review.html   review page with real video scrubbing (frame step, set start/end,
                                  accept/reject, cutaways flag, holds, cuts) -> work/review.json + work/segments_reviewed.csv;
                                  also serves /player/ and /clips/
scripts/auto_cuts.py              frame-accurate bezel test per accepted row -> holds (leading audio) + cuts (sofa shots)
scripts/vision_cuts.py            same for rows without a pixel cue: local shot detection + one Claude vision call
                                  per segment (contact sheet in work/vision/ + subtitle lines per shot) -> trim /
                                  extend / holds / cuts, renders work/preview/<id>.mp4; needs ANTHROPIC_API_KEY
scripts/tags.html                 /tags/ on the review server: accepted rows as videos + tag chips -> review.json tags
scripts/rogue_frames.py           finds 1-3 stray frames next to a hold/cut/clip end; --apply covers them with a tiny hold
scripts/music_scan.py             ♪ subtitle runs -> `music` tag on accepted rows, new `music` rows tagged music+not_tv
scripts/find_dups.py              overlapping rows of one episode; --apply rejects the lesser as "dup of <id>"
scripts/deploy_pages.py           builds the gh-pages branch (player/ + clips/ + redirect) and force-pushes it
player/index.html                 channel-hopper fed by clips/index.csv (channels per category + MIX + OUTLIERS,
                                  wall-clock schedule, optional purple TV frame overlay); drop files still works
refs/                             reference PNGs for hash matching (filename = label); empty so far
```
No video in git. `clips/` and video extensions are gitignored.

## Catalog CSV columns
`id, category, title, simpsons_episode, season, episode, start, end, notes`
- `id` is stable (IS001…) and used in clip filenames: `S05E02_IS043_Spay_Anything.mp4`
- `category`: `itchy_scratchy` now; add `krusty`, `troy_mcclure`, `kent_brockman`, `infomercial` as separate CSVs, same columns
- Source: Wikisimpsons "List of Itchy and Scratchy cartoons" (CC BY-SA 3.0), filtered to shorts that aired inside an episode. Wiki says it's incomplete. IS115 (S34) may be title-card only.

## Pipeline (current)
Goal widened: every in-universe TV programme (I&S, Troy McClure, Kent Brockman, McBain, Krusty,
Bumblebee Man, adverts, "we now return to" bumpers), for a channel-hopping simulated TV.

1. `scripts/fetch_frinkiac_subs.py --seasons 1-9` → `work/subs/SxxEyy.json` + `.srt`.
   Frinkiac's per-episode API gives the full subtitle track with ms timings; it covers at least
   S1–25, and its clock matches the local 360p rips to ~1.5 s. Replaces whisper (faster-whisper
   + RTX 2050 works at 30x realtime if ever needed for uncovered seasons).
2. `scripts/find_segments.py --source <eps> --seasons 5-7 [--cats ...]` → `work/segments.csv`,
   `work/segments.html` (tick boxes + CSV export), thumbs in `work/seg/`.
   - anchors: subtitle lines matching `CATS` regexes (I&S = the sung ♪ title line, not mentions).
   - boundary, primary: **bezel** — all four corners are the in-show TV's purple bezel
     (~RGB 55,38,88, flat) and the centre is lit; runs bridged over ≤ 8 s sofa cutaways.
     Precision on S5–7 review: essentially 100%.
   - boundary, also primary: **mask** — screen-POV cue independent of bezel colour: along each
     corner diagonal (skip px 0–2, rips have a dark edge) a flat plateau then a step ≥ 30 grey
     levels into the picture, vertically paired corners the same colour. Catches season-1 grey
     rounded masks, wooden TVs, full-screen news bulletins/adverts drawn with rounded corners,
     cold opens. ~65–80% precision alone; FPs are credits, telescope/iris masks, dark scenes.
     Frames decoded at 160×120 for this (96×54 is too small for the diagonal profile).
   - fallback: no-yellow run near the anchor (~50% right); else `anchor_only` ±25 s (needs eyes).
     Most remaining anchor_only rows are dialogue *mentions* (not TV); true full-screen shots
     with no mask (Kent in the news chopper, S07E23 1:08) stay unfound.
   - `--anchorless`: every bezel/mask run ≥ 8 s that no anchor claimed becomes a `screen`
     row (method `screen_only`, conf 0.4) with the dialogue heard inside it. ~1 per episode on
     S5–7, ~80% real; on S1 it is the main source (no sung I&S title yet).
   - `screen_spans` column: on-screen sub-runs inside the segment; the gaps are sofa cutaways,
     used only to prefill the cutaways flag (see policy below), never spliced.
   - overlapping rows from different anchors are merged (`category` joined with `+`).
   - `refs/*.png` dHash pulls the start back to a title card if one sits ≤ 10 s before the run.
3. `python scripts/review_server.py --source <eps>` → http://localhost:8765/ . Each row plays the
   episode file (range requests), frame-step / ±1 s / ±5 s, "set start/end = here", accept/reject,
   category edit, **cutaways** flag (prefilled when `screen_spans` has gaps), note, and **holds**:
   `a-b; a-b` ranges where the picture freezes on the frame at b while audio continues (`v` =
   hold from start to here for the I&S theme over the sofa; `{` `}` mark a mid-clip sofa cutaway;
   "prefill from screen gaps" turns the `screen_spans` gaps into holds; `a*-b` freezes on the frame at a
   instead, for a sofa shot that runs to the end of the clip: the "picture frozen on" select and `*` flip
   the hold at the playhead), and **cuts**: `a-b` ranges
   (`(` `)`) removed entirely, video and audio, for cutaways where the show is asynchronous and
   joins up cleanly without the sofa shot. "play segment" previews both (canvas overlay for holds,
   seek-over for cuts). Autosaves to
   `work/review.json`; accepted rows are merged into `work/segments_reviewed.csv`.
   "+ new row at playhead" adds a segment the scan missed (review.json `new=true` rows, method `manual`;
   `review_server.load_rows` merges them everywhere). Accept/reject clears the auto_cuts flag.
   `segments.html` (static, tick + export) still exists but the server page supersedes it.
   `python scripts/auto_cuts.py --source <eps> [--all] [--force] [--ids ...]` fills holds/cuts for accepted
   bezel rows from a per-frame bezel test (matches hand cuts to ~0.1–0.3 s; a segment that goes
   full-screen mid-way gets a wrong trailing cut, e.g. S05E10 Kent, so review afterwards). Skips rows
   that already have holds/cuts unless --force, and rows under 50% bezel (--min-frac); `--todo` also does
   unreviewed rows so they open with cuts prefilled (`--only-todo` = just those). `--cue any` adds the
   mask cue for full-screen programmes drawn with rounded corners (use `--min-gap 12`, it flickers).
   Touched rows get `auto_cuts=true` + `auto_frac`/`auto_cue`: the review page filter "auto-cut, to check"
   lists them, `k` keeps, `u` undoes every hold/cut (whole clip), `r` restores a hand edit that `--force`
   stashed in prev_holds/prev_cuts.
   Reload the review page after running it (the page posts only rows it edited, server merges per row).
   `python scripts/vision_cuts.py --source <eps> --ids ... | --todo [--only-todo] | --all [--sheets-only]`
   does the same for rows the cues can't see (projector, other TVs, full screen, theatre stage): shot
   changes are found locally (frame-to-frame difference, hard cuts land within a frame of hand cuts), one
   frame per shot goes on a numbered contact sheet (`work/vision/<id>.jpg`) and one Claude call per segment
   (`--model`, default claude-opus-5, ~1500 tokens) says which shots are the programme and, for the room
   shots, whether the programme's sound carries on underneath (it reads the subtitle lines per shot). Room
   shots at either end are trimmed off (or, with programme audio, held on the first/last programme frame,
   `a*-b` at the end); interior room shots become a hold (programme audio) or a cut (watchers / silence);
   programme found inside `--pad` (3 s) either side moves start/end out. Rows get `auto_cue=vision`, `shots`
   (state 1 programme, 2 hold, 0 cut/trim), `vision` (note) and `preview`; the script renders
   `work/preview/<id>.mp4` with `extract_clips.render_clip` (skip with `--no-render`). The panel shows the
   shot strip (green / amber / grey, click = seek to first frame), the sheet link, and the rendered clip
   with a "re-render with current edits" button (POST `/render`, encodes from the unsaved field values).
   review.json is re-read and merged per row on every write, so Ctrl+C is safe and the page can stay open.
   `--sheets-only` needs no key; `--dry-run` calls the API but writes nothing.
   Three passes once rows are accepted, each a dry run until `--apply`:
   `python scripts/rogue_frames.py --source <eps> [--ids ...|--all] [--max 3] [--near 2] [--apply --render]`
   decodes each edited clip with ffmpeg's own frame timestamps (showinfo, `-fps_mode passthrough`, the clock
   the cutter's trims use), applies holds/cuts virtually, finds shots with `shot_bounds(min_len=1)` and
   reports runs of ≤ --max frames that start/end within --near frames of a hold/cut edge or the clip's ends
   (`--anywhere` drops that test, but cartoons have real 1-frame flashes). Fix is always a hold: the frame a
   hold froze on → move the hold's b/a one frame; run at the clip end or just before a cut → `prev*-b`
   (consecutive runs merge into one); otherwise `a-b` frozen on the frame after. `--apply` stashes
   prev_holds/prev_cuts, sets auto_cuts/auto_cue=rogue/`rogue` (list of [time, n, fix]) for the "auto-cut,
   to check" filter (k keep / r revert).
   `python scripts/music_scan.py [--seasons 1-9] [--apply]` groups ♪ lines (gap ≤ 6 s, ≥ 3 lines, ≥ 8 s,
   I&S theme / "they fight and fight" / Simpsons title skipped): a song overlapping an accepted row adds the
   tag `music` to it; the rest become `SxxEyy-music-NN` rows (new=true, method `music`, status todo, tags
   music+not_tv, `lyrics`). ~300 of them on S1–9, so the review page method filter has `music` / `all but music`.
   `python scripts/find_dups.py [--overlap 0.5] [--apply]`: accepted/todo rows of one episode whose shorter
   span is > 50% inside the other; loser (todo, then less edit work, shorter, later id) is rejected with note
   "dup of <id>"; different first category tokens are listed only (the user's `music` excerpts inside adverts).
   **Tags**: http://localhost:8765/tags/ (scripts/tags.html) lists every accepted row with its video (clips/
   file if extracted, else work/preview, else the episode at #t=start) and a chip per tag in use (grey / green
   = on, `+ tag` input; names are lowercased, spaces → `_`); POST `/tag {id, tags}` writes `tags` into
   review.json and regenerates segments_reviewed.csv (`tags` column, '+'-joined). Tags decide channels, see 5.
4. `python scripts/extract_clips.py --source <eps> --catalog work/segments_reviewed.csv --precise`
   → `clips/S05E07_<id>_<category>.mp4` + `clips/index.csv` (carries dur, method, cutaways, note, holds, cuts, tags).
5. Player: http://localhost:8765/player/ (or `python -m http.server` at repo root → /player/). Channels
   (`GROUPS` in player/index.html): MAIN = everything in one big shuffle, then I&S · KRUSTY
   (itchy_scratchy, krusty), CHANNEL 6 NEWS (kent_brockman, news), TROY McCLURE, MISC (advert, bumper,
   mcbain, screen, anything else). A category's first token (before `&`/`+`) picks the group. Within every
   channel an episode's clips run back to back in episode order; the episodes shuffle per day. Dial / ↑↓ =
   channel, ◀▶ / ←→ = clip, each channel runs on a shared wall clock (EPOCH 2026-01-01) so a switch lands
   mid-programme. Categories in use: itchy_scratchy, krusty, kent_brockman, news, troy_mcclure, mcbain,
   advert, bumper, screen (unnamed show; relabel on the review page when the dialogue makes it clear).
   Tag channels: `TAG_GROUPS` (MUSIC = tags `music`/`song`) come after the category channels; a clip tagged
   `not_tv` (a song sung in the room, not on a screen) is left out of MAIN and the category channels and only
   plays on its tag channels.
   FRAME button (`f`): auto draws the purple in-show TV frame over clips whose method isn't `bezel`.
   Public copy: https://geojenks.github.io/springfield-tv/ (redirects to player/). `python scripts/deploy_pages.py`
   rebuilds the `gh-pages` branch with the same layout (player/index.html, clips/*.mp4 + index.csv, .nojekyll)
   and force-pushes it; run it after extract_clips. Phones: the set also fits the viewport height (landscape),
   aerial/feet hidden under 520 px tall, and a "tap to switch on" overlay appears when autoplay with sound is refused.
   FULL button (`z`): the page becomes the set: screen fills the viewport height (4:3, centred, purple either
   side), knobs scaled to the height in a column on the right; portrait stacks the knobs under the screen. Also
   requests real fullscreen + landscape lock where allowed; remembered in localStorage (stv-full).
   Rows with holds/cuts are re-encoded (`edit_filter`: trim pieces + tpad clone + concat) even
   without `--precise`.

Cutaways policy (user decision): do NOT splice out sofa shots while the TV audio continues; keep
the whole span and flag `cutaways=1` (kept in index.csv; the player no longer has an OUTLIERS channel).

`scan_episodes.py` (yellow-only scan, ~1 in 5 precision) is superseded; kept for its helpers.

## Known gaps / next steps
0. S1–9 are all scanned; `find_segments.py --merge` keeps existing rows and skips episodes that already
   have any, so a re-scan never overwrites review work (without --merge it rewrites segments.csv). S1–4 and
   S8–9 rows are unreviewed. The McBain/Mendoza pieces are S02E12, S02E15, S03E09.
1. TV-in-shot segments where the set is small in frame (S01E04 8:58) are not worth extracting.
   Mask runs sometimes start a few seconds late when the opening shot is a dim TV frame
   (S05E07 8:42 → really 8:40); the review page is where that gets fixed.
2. Full-screen shots with no mask: tried CLIP ViT-B/32 kNN against bezel-confirmed frames
   (torch cu121 + open_clip installed): no separation, window max below background p99. Not
   pursued for *finding* them; once a row exists (hand-added or anchor_only) `vision_cuts.py`
   handles the cutting.
   Label `screen` rows by category from their dialogue (regex/LLM) instead of leaving "screen".
3. Wikisimpsons `Category:TV shows` and `List of Troy McClure media` give titles but no episode
   or timing; resolve each title against `work/subs` to build the other catalog CSVs.
4. LLM pass over transcript windows for unnamed adverts/shows.
5. Player: done (wall clock). Next: fit real bezel proportions/colour from a bezel frame; Pi build.

## Conventions
- Episode files matched by `SxxEyy` anywhere in filename.
- Timestamps `HH:MM:SS` or `MM:SS`; pad ~1 s each side for keyframe cuts.
- Keep scripts dependency-light (stdlib + numpy + Pillow); ffmpeg on PATH.
- Player chassis is styled after the in-show purple set (bezel colour #4a3e69 measured from bezel frames, so
  bezel clips blend into it); knobs: channel (click/↑↓) and volume (click/wheel/drag, keys - =).
