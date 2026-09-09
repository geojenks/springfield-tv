# CLAUDE.md — Springfield TV

## Goal
A small physical CRT-style TV that plays the TV-within-the-show clips from *The Simpsons* (mainly Itchy & Scratchy; later Krusty, Troy McClure, Kent Brockman, school infomercials). Channel dial scrolls left/right or shuffles. Browser version exists; hardware (Raspberry Pi Zero 2 W + small display + rotary encoder, not Arduino — it can't decode video) comes later.

## Repo layout
```
catalog/itchy_scratchy.csv   120 I&S shorts, Ullman shorts → S36. start/end columns are EMPTY.
scripts/fill_episode_numbers.py   fills `episode` via TVmaze API (run once)
scripts/scan_episodes.py          finds candidate segments in episode files → work/candidates.csv + review.html
scripts/extract_clips.py          ffmpeg cutter driven by catalog rows that have start/end
player/index.html                 browser channel-hopper; drop clip files onto the screen
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
     to be spliced out at extraction time (extract_clips does not do this yet).
   - overlapping rows from different anchors are merged (`category` joined with `+`).
   - `refs/*.png` dHash pulls the start back to a title card if one sits ≤ 10 s before the run.
3. Human ticks rows in `segments.html`, exports CSV, pastes into the catalog; `extract_clips.py`.

`scan_episodes.py` (yellow-only scan, ~1 in 5 precision) is superseded; kept for its helpers.

## Known gaps / next steps
1. `extract_clips.py` should honour `screen_spans` (concat the on-screen sub-runs, drop cutaways).
   TV-in-shot segments where the set is small in frame (S01E04 8:58) are not worth extracting.
2. Full-screen shots with no mask: tried CLIP ViT-B/32 kNN against bezel-confirmed frames
   (torch cu121 + open_clip installed): no separation, window max below background p99. Not
   pursued; needs a different idea (audio? per-scene shot matching) or eyes.
   Label `screen` rows by category from their dialogue (regex/LLM) instead of leaving "screen".
3. Wikisimpsons `Category:TV shows` and `List of Troy McClure media` give titles but no episode
   or timing; resolve each title against `work/subs` to build the other catalog CSVs.
4. LLM pass over transcript windows for unnamed adverts/shows.
5. Player: each channel on a wall clock so switching lands mid-programme.

## Conventions
- Episode files matched by `SxxEyy` anywhere in filename.
- Timestamps `HH:MM:SS` or `MM:SS`; pad ~1 s each side for keyframe cuts.
- Keep scripts dependency-light (stdlib + numpy + Pillow); ffmpeg on PATH.
- Player design is an original retro CRT, not a copy of the show's set.
