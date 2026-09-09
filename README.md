# Springfield TV

A catalog of the TV-within-the-TV shorts from *The Simpsons*, plus tooling to cut them from your own episode files and play them on a retro CRT-style channel-hopper (browser now, Raspberry Pi later).

**No video is stored in this repo.** It contains a catalog (titles, episode, timestamps you add yourself), scripts, and the player. Cut clips from copies you own; keep the `clips/` folder out of git (it's already in `.gitignore`).

## Layout

```
catalog/itchy_scratchy.csv   120 Itchy & Scratchy shorts, seasons 1–36 + Ullman shorts
catalog/krusty.csv           (todo) Krusty the Clown Show segments
scripts/fill_episode_numbers.py   fills the `episode` column via the TVmaze API
scripts/extract_clips.py          ffmpeg cutter driven by the catalog
player/index.html                 browser channel-hopper — drop clips onto it
```

## Workflow

1. `python scripts/fill_episode_numbers.py` — one-off, fills SxxEyy numbers.
2. Open the CSV, watch each episode, and fill `start` / `end` (`MM:SS` or `HH:MM:SS`). Leave a second of padding on each side; the fast cutter snaps to keyframes.
3. `python scripts/extract_clips.py --source /path/to/episodes --out clips`
   Add `--precise` for frame-accurate (slower, re-encoded) cuts.
4. Open `player/index.html`, drop the `clips/` folder onto the screen.

Source files are matched by an `SxxEyy` tag anywhere in the filename (`Simpsons.S05E02.Cape.Feare.mkv` works).

## CSV columns

| column | meaning |
|---|---|
| `id` | stable key, used in output filenames |
| `category` | `itchy_scratchy`, later `krusty`, `other_tv` |
| `title` | short's title card, or `Untitled (…)` with a one-line description |
| `simpsons_episode`, `season`, `episode` | host episode |
| `start`, `end` | timestamps in your copy — **blank until you fill them** |
| `notes` | anything worth knowing |

## Catalog status

Titles and host episodes come from Wikisimpsons' *List of Itchy and Scratchy cartoons* (CC BY-SA 3.0), filtered to shorts that actually aired inside an episode — comics, trading cards, games, adverts and ride footage are excluded. The wiki marks its own list as incomplete, so expect to add rows as you watch. Timestamps are not published anywhere reliable; they have to be found by hand.

## Hardware notes (next phase)

Arduino boards can't decode video. Plan for a Raspberry Pi Zero 2 W + 2–3" SPI/DPI display, a rotary encoder on a GPIO for the channel dial, and `mpv` reading the same `clips/` folder. The catalog and clip naming above carry straight over.

## Finding timestamps automatically

Two steps, both stdlib + numpy + Pillow + ffmpeg:

```
python scripts/fetch_frinkiac_subs.py --seasons 1-9                 # subtitle tracks -> work/subs/
python scripts/find_segments.py --source /path/to/episodes --seasons 5-7 --anchorless
```

`fetch_frinkiac_subs.py` pulls each episode's full subtitle track (with millisecond timings) from
Frinkiac; the timings line up with common 480×360 rips to within a couple of seconds.
`find_segments.py` searches those subtitles for anchors (Troy McClure's intro, the sung
Itchy & Scratchy title, "I'm Kent Brockman", "we now return to…", McBain, adverts) and then finds
the segment's edges in the video: frames drawn inside the in-show TV are the primary cue, either
the purple bezel or, more generally, the rounded screen mask of any colour (a flat plateau then a
step along each corner diagonal), which also catches full-screen news bulletins, adverts and cold
opens; runs without Simpsons-yellow are the fallback. With `--anchorless` every screen run of 8 s
or more that no subtitle anchor claimed is reported too, with the dialogue heard inside it, which
is how season 1 (no sung I&S title yet) gets found. It writes `work/segments.csv` and
`work/segments.html`, a review page with the frames just inside and outside each cut and a
button that exports the ticked rows as CSV for the catalog. The `screen_spans` column lists the
on-screen sub-runs inside a segment; the gaps are cutaways to the sofa.

To check the cuts against the actual video:

```
python scripts/review_server.py --source /path/to/episodes      # then open http://localhost:8765/
```

Each row gets a player for its episode with frame-step and second-step buttons, "set start/end =
here", accept/reject, a cutaways flag (the camera goes back to the sofa while the TV audio carries
on; those clips are kept whole and flagged so the player can treat them as their own channel) and a
note. Edits autosave to `work/review.json`; accepted rows land in `work/segments_reviewed.csv`,
which `extract_clips.py --catalog work/segments_reviewed.csv` cuts into `clips/` with an
`index.csv`.
