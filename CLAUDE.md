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
   - fallback: no-yellow run near the anchor (~50% right); else `anchor_only` ±25 s (needs eyes).
   - overlapping rows from different anchors are merged (`category` joined with `+`).
   - `refs/*.png` dHash pulls the start back to a title card if one sits ≤ 10 s before the run.
3. Human ticks rows in `segments.html`, exports CSV, pastes into the catalog; `extract_clips.py`.

`scan_episodes.py` (yellow-only scan, ~1 in 5 precision) is superseded; kept for its helpers.

## Known gaps / next steps
1. **Full-screen segments** (Kent at the news desk, Troy's IBN advert, opening-cold-on-TV
   episodes) have no bezel and no yellow-free run → `anchor_only`. Needs a new cue: per-season
   `refs/` of the Channel 6 desk / IBN card, or CLIP zero-shot on 1 fps frames.
2. Sung I&S line is missed by some Frinkiac tracks; add bezel-run + title-card-hash as an
   anchorless I&S detector to catch the rest (catalog gives expected count per episode).
3. Wikisimpsons `Category:TV shows` and `List of Troy McClure media` give titles but no episode
   or timing; resolve each title against `work/subs` to build the other catalog CSVs.
4. LLM pass over transcript windows for unnamed adverts/shows.
5. Player: each channel on a wall clock so switching lands mid-programme.

## Conventions
- Episode files matched by `SxxEyy` anywhere in filename.
- Timestamps `HH:MM:SS` or `MM:SS`; pad ~1 s each side for keyframe cuts.
- Keep scripts dependency-light (stdlib + numpy + Pillow); ffmpeg on PATH.
- Player design is an original retro CRT, not a copy of the show's set.
