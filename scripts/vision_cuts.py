#!/usr/bin/env python3
"""Vision-model cuts for segments the pixel cues can't see (projector, other TVs, full screen, stage).

auto_cuts.py needs the purple bezel or a screen mask in every frame. This tool instead

  1. decodes the segment (plus --pad seconds either side) and finds every shot change locally,
     frame-exact, from the frame-to-frame difference (animation cuts are hard cuts);
  2. tiles one frame per shot into a numbered contact sheet (work/vision/<id>.jpg, kept so you
     can see what the model saw) and lists, per shot, the subtitle lines spoken during it;
  3. asks a Claude model, once per segment, which shots are the programme itself (what the TV /
     projector / cinema screen / stage is showing, bezel included) and, for the room shots,
     whether the programme's sound carries on underneath (the model reads the subtitles: the
     programme's dialogue continuing = yes; the watchers talking, or nothing = no);
  4. turns that into the review-page edit: room shots with the programme audio under them
     become holds (picture frozen, sound continues; a*-b when they run to the end), room shots
     without it become cuts (video and audio removed, the show joins up), and room shots at the
     very start or end are trimmed off;
  5. renders the result (work/preview/<id>.mp4, the same ffmpeg graph extract_clips uses) so the
     review page can play the finished clip; "re-render" there redoes it after hand edits.

Rows get auto_cuts=true, auto_cue="vision", auto_frac, `shots` ([t0, t1, state] per shot:
1 programme, 2 room with programme audio -> hold, 0 room -> cut/trim), `vision` (the model's
note) and `preview`=true. Hand-made holds/cuts are left alone unless --force (stashed in
prev_holds/prev_cuts, `r` on the page reverts).

  python scripts/vision_cuts.py --source <eps> --ids ID ...          # named rows (redone even if cut already)
  python scripts/vision_cuts.py --source <eps> --todo [--only-todo]  # unreviewed rows
  python scripts/vision_cuts.py --source <eps> --all                 # every accepted row without cuts
       [--model claude-opus-5] [--pad 3] [--no-render] [--sheets-only] [--dry-run] [--force] [--work work]

Needs ANTHROPIC_API_KEY in the environment (or --key-file). No SDK: plain HTTPS to the Messages
API, ~1500 input tokens per segment. --sheets-only builds the sheets and stops (no key needed).
review.json is re-read and merged per row on every write, so the run can be stopped (Ctrl+C)
at any time and the review page can be used while it runs. RELOAD the page afterwards.
"""
import argparse, base64, csv, io, json, os, re, subprocess, sys, time, urllib.request
import numpy as np
from PIL import Image, ImageDraw

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from extract_clips import secs, find_source, fps_of, render_clip
from review_server import write_reviewed, load_rows
from auto_cuts import hms

TW, TH = 200, 150          # decoded frame / tile size
COLS = 6                   # tiles per sheet row
PER_SHEET = 36             # tiles per sheet (6x6 at 200x150 = 1200x900 + labels)
API = "https://api.anthropic.com/v1/messages"

PROMPT = """These numbered tiles are one frame from each shot of a clip from The Simpsons, in order. The clip is meant to be an in-universe programme{cat}: something the characters watch on a television, a projector, a cinema screen, or a stage performance in a theatre. Some shots are the programme itself; others are the room or audience (people watching, the sofa, the street, reaction shots, the cinema seats).

Below the tiles is what the subtitle track says is spoken during each shot (with its time span). Lines in a room shot are either the programme carrying on underneath (its presenter, characters or narrator still talking, its song still playing) or the watchers talking to each other.

The goal is a clip of just the programme that stands alone and sounds coherent. For every tile decide:
  on = true  -> the tile shows the programme: the picture on the screen (with or without a TV bezel or rounded screen mask around it, and including the screen filling the whole frame), or the stage/performers of the show being watched.
  on = false -> the tile is the world around the screen: characters watching, the room, the outside of the TV set seen small in a wide shot, reaction shots, a title of the episode itself.
  audio (only for on = false): "programme" if the programme's own sound continues through this shot (its dialogue, narration or music keeps going, so the shot must be kept with a frozen picture), or "room" if what is heard is the watchers, silence, or the programme is effectively paused (the shot can be dropped and the programme joins up). When a room shot has no subtitle lines at all, prefer "room" unless the lines just before and after it are clearly one continuous programme sentence or song.

A frame that is mostly the TV set from the room (a small screen inside a big room) is off; a frame where the screen's picture fills most of the tile is on. Be decisive; every tile needs an answer.

{lines}

Reply with JSON only, no prose:
{{"shots": [{{"n": 1, "on": true}}, {{"n": 2, "on": false, "audio": "programme"}}, ...], "category": "<one of itchy_scratchy, krusty, kent_brockman, news, troy_mcclure, mcbain, advert, bumper, film, play, other>", "title": "<programme name if you can tell, else empty>", "note": "<one short sentence about what the programme is>"}}"""


def decode(path, start, dur):
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", f"{start:.3f}", "-t", f"{dur:.3f}",
           "-i", path, "-vf", f"scale={TW}:{TH}", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    raw = subprocess.run(cmd, capture_output=True, check=True).stdout
    n = len(raw) // (TW * TH * 3)
    return np.frombuffer(raw, np.uint8)[: n * TW * TH * 3].reshape(n, TH, TW, 3)


def shot_bounds(fr, min_len=3):
    """Frame indices where a new shot starts (0 first). Hard cuts: the mean absolute difference
    to the previous frame jumps well above its local level; a low-res histogram difference
    catches cuts between two similar-brightness scenes."""
    if len(fr) < 2:
        return [0]
    g = fr[:, ::2, ::2, :].astype(np.int16)
    d = np.abs(g[1:] - g[:-1]).mean(axis=(1, 2, 3))                    # pixel change
    h = np.stack([np.histogram(f, bins=16, range=(0, 256))[0] for f in g[:, :, :, 1]]).astype(float)
    hd = np.abs(h[1:] - h[:-1]).sum(axis=1) / h[0].sum()                # green-channel histogram change
    cuts = []
    for i in range(len(d)):
        lo, hi = max(0, i - 12), min(len(d), i + 13)
        local = np.median(np.concatenate([d[lo:i], d[i + 1:hi]])) if hi - lo > 2 else 0
        if (d[i] > 22 and d[i] > 3 * local + 8) or (hd[i] > 0.45 and d[i] > 12):
            cuts.append(i + 1)
    out = [0]
    for c in cuts:
        if c - out[-1] >= min_len:
            out.append(c)
    return out


def sheets(fr, bounds, path):
    """Contact sheets (list of JPEG bytes) with one numbered tile per shot; also saves them."""
    ends = bounds[1:] + [len(fr)]
    tiles = [(i + 1, fr[(a + b) // 2]) for i, (a, b) in enumerate(zip(bounds, ends))]
    out = []
    for s in range(0, len(tiles), PER_SHEET):
        chunk = tiles[s:s + PER_SHEET]
        rows = (len(chunk) + COLS - 1) // COLS
        im = Image.new("RGB", (COLS * (TW + 4), rows * (TH + 22)), (40, 40, 40))
        dr = ImageDraw.Draw(im)
        for k, (n, f) in enumerate(chunk):
            x, y = (k % COLS) * (TW + 4), (k // COLS) * (TH + 22)
            im.paste(Image.fromarray(f), (x + 2, y + 20))
            dr.rectangle([x + 2, y, x + 60, y + 18], fill=(255, 220, 0))
            dr.text((x + 6, y + 3), f"#{n}", fill=(0, 0, 0))
        buf = io.BytesIO(); im.save(buf, "JPEG", quality=85)
        out.append(buf.getvalue())
        p = path if len(tiles) <= PER_SHEET else path.replace(".jpg", f"_{s // PER_SHEET + 1}.jpg")
        open(p, "wb").write(out[-1])
    return out


def load_subs(work, episode):
    p = os.path.join(work, "subs", f"{episode}.json")
    if not os.path.exists(p):
        return []
    subs = json.load(open(p, encoding="utf-8")).get("Subtitles") or []
    return [(s["StartTimestamp"] / 1000, s["EndTimestamp"] / 1000, s["Content"].replace("\n", " ").strip())
            for s in subs]


def shot_lines(subs, times):
    """One text line per shot: '#n  mm:ss.s-mm:ss.s  «what is said»'."""
    out = []
    for i, (a, b) in enumerate(times):
        said = [t for s0, s1, t in subs if s1 > a + 0.15 and s0 < b - 0.15]
        out.append(f"#{i + 1}  {hms(a)[3:]}-{hms(b)[3:]}  " + (" / ".join(said) if said else "(no subtitle)"))
    return "Spoken during each shot (subtitle track):\n" + "\n".join(out)


def ask(key, model, images, prompt, retries=3):
    content = []
    for i, im in enumerate(images):
        if len(images) > 1:
            content.append({"type": "text", "text": f"Sheet {i + 1} of {len(images)}:"})
        content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                    "data": base64.b64encode(im).decode()}})
    content.append({"type": "text", "text": prompt})
    body = json.dumps({"model": model, "max_tokens": 2000,
                       "messages": [{"role": "user", "content": content}]}).encode()
    req = urllib.request.Request(API, data=body, headers={
        "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    txt = ""
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                rep = json.load(r)
            txt = "".join(c.get("text", "") for c in rep["content"])
            m = re.search(r"\{.*\}", txt, re.S)
            return json.loads(m.group(0)), rep.get("usage", {})
        except urllib.error.HTTPError as e:
            msg = e.read().decode(errors="replace")[:300]
            if e.code in (429, 500, 529) and attempt + 1 < retries:
                time.sleep(5 * (attempt + 1)); continue
            raise SystemExit(f"API error {e.code}: {msg}")
        except (json.JSONDecodeError, AttributeError):
            if attempt + 1 < retries:
                continue
            raise SystemExit(f"unparseable reply: {txt[:300]}")


def merge_runs(shots):
    """[(t0, t1, state)] -> runs [[t0, t1, state]] with equal neighbouring states joined."""
    m = []
    for a, b, s in shots:
        if m and m[-1][2] == s and abs(a - m[-1][1]) < 1e-3:
            m[-1][1] = b
        else:
            m.append([a, b, s])
    return m


def edit_from_shots(shots, fps=30.0):
    """[(t0, t1, state)] absolute times, state 1 programme / 2 room+programme audio / 0 room ->
    (holds, cuts, start, end) or None. Leading and trailing state-0 shots are trimmed off;
    leading/trailing state-2 runs become holds (frozen on the first / last programme frame);
    interior gaps become one hold or one cut by which state covers more of the gap.
    Shot times are the first frame of the new shot, so a hold `a-b` ending on a boundary freezes
    on the first programme frame (edit_filter takes the frame at b), while a trailing `a*-b`
    hold must start half a frame before the boundary so the frame at a is the last programme
    frame, not the first room frame."""
    runs = merge_runs(shots)
    on = [i for i, r in enumerate(runs) if r[2] == 1]
    if not on:
        return None
    first, last = on[0], on[-1]
    holds, cuts = [], []
    start = runs[first][0]
    if first > 0 and runs[first - 1][2] == 2:                 # programme audio before the picture
        start = runs[first - 1][0]
        holds.append(f"{hms(start)}-{hms(runs[first][0])}")
    end = runs[last][1]
    if last + 1 < len(runs) and runs[last + 1][2] == 2:       # programme audio after the picture
        end = runs[last + 1][1]
        holds.append(f"{hms(runs[last][1] - 0.5 / fps)}*-{hms(end)}")
    for i, j in zip(on, on[1:]):                              # interior gaps
        gap = runs[i + 1:j]
        a, b = gap[0][0], gap[-1][1]
        prog = sum(r[1] - r[0] for r in gap if r[2] == 2)
        (holds if prog >= (b - a) / 2 else cuts).append(f"{hms(a)}-{hms(b)}")
    holds.sort(); cuts.sort()
    return "; ".join(holds), "; ".join(cuts), start, end


def save_row(rp, rid, e):
    """Re-read review.json and merge just this row (the review page may be saving other rows)."""
    cur = json.load(open(rp, encoding="utf-8")) if os.path.exists(rp) else {}
    cur[rid] = e
    with open(rp, "w", encoding="utf-8") as f:
        json.dump(cur, f, indent=1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True)
    ap.add_argument("--work", default="work")
    ap.add_argument("--ids", nargs="*", help="only these segment ids")
    ap.add_argument("--all", action="store_true", help="every accepted row that has no holds/cuts yet")
    ap.add_argument("--todo", action="store_true", help="also unreviewed rows from segments.csv")
    ap.add_argument("--only-todo", action="store_true", help="with --todo: only rows without a status")
    ap.add_argument("--force", action="store_true", help="redo rows that already have holds/cuts")
    ap.add_argument("--pad", type=float, default=3.0, help="seconds looked at either side of the row")
    ap.add_argument("--model", default="claude-opus-5")
    ap.add_argument("--key-file", help="file holding the API key (default: ANTHROPIC_API_KEY)")
    ap.add_argument("--sheets-only", action="store_true", help="build contact sheets, no API call")
    ap.add_argument("--no-render", action="store_true", help="skip rendering work/preview/<id>.mp4")
    ap.add_argument("--dry-run", action="store_true", help="call the API but write nothing")
    a = ap.parse_args()

    key = open(a.key_file).read().strip() if a.key_file else os.environ.get("ANTHROPIC_API_KEY", "")
    if not key and not a.sheets_only:
        raise SystemExit("set ANTHROPIC_API_KEY (or --key-file), or use --sheets-only")

    rp = os.path.join(a.work, "review.json")
    edits = json.load(open(rp, encoding="utf-8")) if os.path.exists(rp) else {}
    rows = [] if a.only_todo else list(csv.DictReader(open(os.path.join(a.work, "segments_reviewed.csv"), encoding="utf-8")))
    if a.todo:
        seen = {r["id"] for r in rows}
        for r in load_rows(a.work)[0]:
            e = edits.get(r["id"], {})
            if r["id"] in seen or e.get("status") == "reject" or (a.only_todo and e.get("status")):
                continue
            rows.append(dict(r, start=e.get("start") or r["start"], end=e.get("end") or r["end"]))
    if a.ids:
        rows = [r for r in rows if r["id"] in a.ids]
    vdir, pdir = os.path.join(a.work, "vision"), os.path.join(a.work, "preview")
    os.makedirs(vdir, exist_ok=True); os.makedirs(pdir, exist_ok=True)
    fps_cache, subs_cache, changed = {}, {}, 0
    usage = {"input_tokens": 0, "output_tokens": 0}
    for r in rows:
        e = edits.get(r["id"], {})
        had = bool(e.get("holds") or e.get("cuts"))
        if had and not a.force and not a.ids:
            continue
        src = find_source(a.source, int(r["episode"][1:3]), int(r["episode"][4:6]))
        if not src:
            print(f"!! {r['id']}: no episode file"); continue
        fps = fps_cache.setdefault(src, fps_of(src))
        start, end = secs(r["start"]), secs(r["end"])
        t0 = max(0.0, start - a.pad)
        fr = decode(src, t0, end - start + 2 * a.pad)
        bounds = shot_bounds(fr)
        ends = bounds[1:] + [len(fr)]
        times = [(t0 + b / fps, t0 + c / fps) for b, c in zip(bounds, ends)]
        ims = sheets(fr, bounds, os.path.join(vdir, f"{r['id']}.jpg"))
        print(f"{r['id']}: {len(fr)} frames, {len(bounds)} shots, {len(ims)} sheet(s)", end="", flush=True)
        if a.sheets_only:
            print(); continue
        subs = subs_cache.setdefault(r["episode"], load_subs(a.work, r["episode"]))
        cat = e.get("category") or r.get("category") or ""
        cat = f" ({cat.replace('_', ' ')})" if cat and cat != "screen" else ""
        rep, u = ask(key, a.model, ims, PROMPT.format(cat=cat, lines=shot_lines(subs, times)))
        for k in usage:
            usage[k] += u.get(k, 0)
        state = {}
        for s in rep.get("shots", []):
            if "n" not in s:
                continue
            state[int(s["n"])] = 1 if s.get("on") else (2 if str(s.get("audio", "")).lower().startswith("prog") else 0)
        shots = [[round(x, 3), round(y, 3), state.get(i + 1, 0)] for i, (x, y) in enumerate(times)]
        res = edit_from_shots([(x, y, o) for x, y, o in shots], fps)
        if res is None:
            print(" -> model saw no programme shots, skipped"); continue
        holds, cuts, ns, ne = res
        tot = sum(y - x for x, y, o in shots)
        frac = sum(y - x for x, y, o in shots if o == 1) / max(1e-6, tot)
        print(f" -> {frac:.0%} on; {rep.get('category', '?')} {rep.get('title', '')!r}; "
              f"{hms(ns)} → {hms(ne)}; holds [{holds}] cuts [{cuts}]")
        if a.dry_run:
            continue
        e = json.load(open(rp, encoding="utf-8")).get(r["id"], e) if os.path.exists(rp) else e   # freshest copy
        if had and not e.get("auto_cuts") and (holds, cuts) != (e.get("holds", ""), e.get("cuts", "")):
            e["prev_holds"], e["prev_cuts"] = e.get("holds", ""), e.get("cuts", "")
        e.update(holds=holds, cuts=cuts, auto_cuts=True, auto_cue="vision", auto_frac=round(frac, 2),
                 shots=shots, start=hms(ns), end=hms(ne),
                 vision=f"{rep.get('category', '')} {rep.get('title', '')} — {rep.get('note', '')}".strip())
        if not a.no_render:
            try:
                render_clip(src, ns, ne, holds, cuts, os.path.join(pdir, f"{r['id']}.mp4"), fps)
                e["preview"] = True
            except subprocess.CalledProcessError as ex:
                print(f"   !! render failed: {ex}")
        save_row(rp, r["id"], e)
        edits[r["id"]] = e
        changed += 1
    if usage["input_tokens"]:
        print(f"\ntokens: {usage['input_tokens']} in, {usage['output_tokens']} out ({a.model})")
    if changed:
        n = write_reviewed(a.work)
        print(f"{changed} rows updated in {rp}; {n} accepted rows -> segments_reviewed.csv. Reload the review page.")


if __name__ == "__main__":
    main()
