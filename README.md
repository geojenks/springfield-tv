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
python scripts/find_segments.py --source /path/to/episodes --seasons 1-4 --anchorless --merge   # later seasons: keep what is there
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
note, and **holds**: `a-b` ranges (press `v` for start→here, or `{` `}` around a sofa cutaway; `a*-b` or the `*` key freezes on the frame at a instead of b) where the
picture freezes on the frame at `b` while the audio keeps going, and **cuts** (`(` `)`), ranges removed
entirely so an asynchronous show joins up without the sofa shot; "play segment" previews both.
Edits autosave to `work/review.json`; accepted rows land in `work/segments_reviewed.csv`,
which `extract_clips.py --catalog work/segments_reviewed.csv` cuts into `clips/` with an
`index.csv`.

`python scripts/auto_cuts.py --source /path/to/episodes` fills holds and cuts automatically for accepted
rows with the purple bezel, from a frame-by-frame bezel test (leading sofa audio becomes a hold, interior
sofa shots become cuts). Check the result on the review page and delete any cut that is wrong.
For rows with no purple bezel (projector, another TV, full screen, a play on stage)
`python scripts/vision_cuts.py --source /path/to/episodes --todo` finds the shot changes locally, puts one
frame per shot on a numbered contact sheet with the subtitle lines spoken in each, and asks a Claude model (set
`ANTHROPIC_API_KEY`, or `--key-file`) which shots are the programme and whether its sound carries on under the
room shots. It trims the ends, extends into the padding when the programme continues there, freezes the picture
where the programme's audio runs under a reaction shot and cuts where it does not, then renders the finished
clip to `work/preview/<id>.mp4`; the review panel plays it, shows the shots as clickable buttons (green /
amber = frozen / grey = cut) and has a "re-render" button for after you fix anything by hand.

Three passes over the finished rows, all dry-run first:

```
python scripts/rogue_frames.py --source /path/to/episodes            # stray frames left by a hold/cut a frame off
python scripts/music_scan.py                                         # ♪ runs in the subtitles -> music rows / tags
python scripts/find_dups.py                                          # two rows over the same footage
python scripts/auto_tags.py                                          # tag suggestions from the dialogue (sports, news...)
```

`rogue_frames.py` decodes each edited clip, applies its holds and cuts virtually and reports every run of
1–3 frames that flashes by next to a hold, a cut or the clip's ends; `--apply` covers each run with a tiny
hold on the neighbouring frame (`--render` re-encodes the preview) and flags the row `auto_cue=rogue` for
the "auto-cut, to check" filter. `music_scan.py` groups the sung (♪) lines of every episode into songs
(the I&S theme excluded): songs inside an accepted row get the tag `music`, the rest become to-do rows of
category `music` tagged `music` + `not_tv`, timed exactly to the sung lines (the review page's method filter
has "music" / "all but music"; a re-run retimes the rows still to do). `find_dups.py --apply` rejects the
lesser of two overlapping rows as "dup of …" (different categories are only listed). `auto_tags.py` reads
the subtitle lines heard inside each accepted row against one regex per tag (sports, news, mcbain,
troy_mcclure, krusty, itchy_scratchy, advert, bumblebee) and lists the tags it would add with the hit count
and a sample line; `--apply` adds them.

http://localhost:8765/tags/ is a lighter pass over the accepted rows: each one plays (the cut clip if it
exists, else the preview, else the episode at its start) under a row of chips, one per tag in use, click to
toggle, `+ tag` to invent one. Tags go into review.json, the `tags` column of `segments_reviewed.csv` and
`clips/index.csv`, and the player turns them into channels (`CHANNELS` in `player/index.html`); a clip
tagged `not_tv` is only ever on the channels that claim it by tag, never on MAIN or MISC.

Then open http://localhost:8765/player/ : MAIN (everything) then ITCHY & SCRATCHY, KRUSTY (which includes
the I&S shorts), CHANNEL 6 NEWS, SPORTS, TROY McCLURE, McBAIN, MTV (WIP) and MISC (whatever no channel claimed).
On every channel an episode's clips run back to back in episode order and the episodes shuffle (per day;
SHUFFLE deals again); the tag channels get an advert after every three episodes. Each channel runs on a wall
clock so changing channel lands mid-programme; the FRAME button draws the in-show purple TV frame over
clips that were shot full-screen.

To put the same thing on GitHub Pages, `python scripts/deploy_pages.py` builds a `gh-pages` branch holding
`player/`, `clips/` and a redirecting `index.html`, and force-pushes it (the clips go public; the code branch
stays clip-free). Set Pages to that branch once and the player lives at `https://<you>.github.io/<repo>/`.
It fits a phone held sideways; tap the screen once to switch the sound on, and FULL fills the whole display
with the screen in the middle and the knobs down the right.
