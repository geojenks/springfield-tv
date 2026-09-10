#!/usr/bin/env python3
"""Vision-model cuts for segments the pixel cues can't see (projector, other TVs, full screen, stage).

auto_cuts.py needs the purple bezel or a screen mask in every frame. This tool instead

  1. decodes the segment (plus --pad seconds either side) and finds every shot change locally,
     frame-exact, from the frame-to-frame difference (animation cuts are hard cuts);
  2. tiles one frame per shot into a numbered contact sheet (work/vision/<id>.jpg, kept so you
     can see what the model saw);
  3. asks a Claude model, once per segment, which numbered shots are the programme itself
     (what the TV / projector / cinema screen / stage is showing, bezel included) and which are
     the room or audience; the reply also suggests a category and a title;
  4. turns the answer into the same holds/cuts auto_cuts.py writes: leading room shots -> a hold
     (audio from start, picture frozen on the first programme frame), interior and trailing room
     shots -> cuts. If the programme starts or ends inside the pad, start/end move out to it.

Rows get auto_cuts=true, auto_cue="vision", auto_frac, plus `shots` ([t0, t1, on] per shot)
and `vision` (the model's note) so the review page can show the shot strip. Hand-made holds/cuts
are left alone unless --force (stashed in prev_holds/prev_cuts, `r` on the page reverts).

  python scripts/vision_cuts.py --source <eps> --ids ID ...          # named rows
  python scripts/vision_cuts.py --source <eps> --todo [--only-todo]  # unreviewed rows
  python scripts/vision_cuts.py --source <eps> --all                 # every accepted row without cue cuts
       [--model claude-opus-5] [--pad 3] [--sheets-only] [--dry-run] [--force] [--work work]

Needs ANTHROPIC_API_KEY in the environment (or --key-file). No SDK: plain HTTPS to the Messages
API. Roughly one image (~1500 tokens) + a short JSON reply per segment. --sheets-only builds
the contact sheets and stops (no key needed) so you can check the shot detection first.
RELOAD the review page after running (the page posts only rows it edited, server merges per row).
"""
import argparse, base64, csv, io, json, os, re, subprocess, sys, time, urllib.request
import numpy as np
from PIL import Image, ImageDraw

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from extract_clips import secs, find_source, fps_of
from review_server import write_reviewed, load_rows
from auto_cuts import hms

TW, TH = 200, 150          # decoded frame / tile size
COLS = 6                   # tiles per sheet row
PER_SHEET = 36             # tiles per sheet (6x6 at 200x150 = 1200x900 + labels)
API = "https://api.anthropic.com/v1/messages"

PROMPT = """These numbered tiles are one frame from each shot of a clip from The Simpsons, in order. The clip is meant to be an in-universe programme{cat}: something the characters watch on a television, a projector, a cinema screen, or a stage performance in a theatre. Some shots are the programme itself; others are the room or audience (people watching, the sofa, the street, reaction shots, the cinema seats).

For every tile decide:
  on = true  -> the tile shows the programme: the picture on the screen (with or without a TV bezel or rounded screen mask around it, and including the screen filling the whole frame), or the stage/performers of the show being watched.
  on = false -> the tile is the world around the screen: characters watching, the room, the outside of the TV set seen small in a wide shot, reaction shots, a title of the episode itself.

A frame that is mostly the TV set from the room (a small screen inside a big room) is off; a frame where the screen's picture fills most of the tile is on. Be decisive; every tile needs an answer.

Reply with JSON only, no prose:
{{"shots": [{{"n": 1, "on": true}}, ...], "category": "<one of itchy_scratchy, krusty, kent_brockman, news, troy_mcclure, mcbain, advert, bumper, film, play, other>", "title": "<programme name if you can tell, else empty>", "note": "<one short sentence about what the programme is>"}}"""


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


def ask(key, model, images, prompt, retries=3):
    content = []
    for i, im in enumerate(images):
        if len(images) > 1:
            content.append({"type": "text", "text": f"Sheet {i + 1} of {len(images)}:"})
        content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                    "data": base64.b64encode(im).decode()}})
    content.append({"type": "text", "text": prompt})
    body = json.dumps({"model": model, "max_tokens": 1500,
                       "messages": [{"role": "user", "content": content}]}).encode()
    req = urllib.request.Request(API, data=body, headers={
        "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
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


def edit_from_shots(shots, start, end):
    """[(t0, t1, on)] absolute times -> (holds, cuts, new_start, new_end); the leading off run
    inside [start, end] becomes a hold, other off runs cuts, programme in the pad extends the row."""
    on = [(a, b) for a, b, o in shots if o]
    if not on:
        return None
    # merge adjacent programme shots
    m = [list(on[0])]
    for a, b in on[1:]:
        if abs(a - m[-1][1]) < 1e-3:
            m[-1][1] = b
        else:
            m.append([a, b])
    first, last = m[0][0], m[-1][1]
    ns, ne = min(start, first), max(end, last)
    holds, cuts = [], []
    if first > ns:
        holds.append(f"{hms(ns)}-{hms(first)}")
    for (a0, a1), (b0, b1) in zip(m, m[1:]):
        cuts.append(f"{hms(a1)}-{hms(b0)}")
    if last < ne:
        cuts.append(f"{hms(last)}-{hms(ne)}")
    return "; ".join(holds), "; ".join(cuts), ns, ne


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
    vdir = os.path.join(a.work, "vision"); os.makedirs(vdir, exist_ok=True)
    fps_cache, changed, usage = {}, 0, {"input_tokens": 0, "output_tokens": 0}
    for r in rows:
        e = edits.setdefault(r["id"], {})
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
        cat = e.get("category") or r.get("category") or ""
        cat = f" ({cat.replace('_', ' ')})" if cat and cat != "screen" else ""
        rep, u = ask(key, a.model, ims, PROMPT.format(cat=cat))
        for k in usage:
            usage[k] += u.get(k, 0)
        onmap = {int(s["n"]): bool(s["on"]) for s in rep.get("shots", []) if "n" in s}
        shots = [[round(x, 3), round(y, 3), int(onmap.get(i + 1, False))] for i, (x, y) in enumerate(times)]
        res = edit_from_shots([(x, y, o) for x, y, o in shots], start, end)
        if res is None:
            print(" -> model saw no programme shots, skipped"); continue
        holds, cuts, ns, ne = res
        frac = sum(y - x for x, y, o in shots if o) / max(1e-6, sum(y - x for x, y, o in shots))
        print(f" -> {frac:.0%} on; {rep.get('category', '?')} {rep.get('title', '')!r}; "
              f"start {hms(ns)} end {hms(ne)}; holds [{holds}] cuts [{cuts}]")
        if a.dry_run:
            continue
        if had and not e.get("auto_cuts") and (holds, cuts) != (e.get("holds", ""), e.get("cuts", "")):
            e["prev_holds"], e["prev_cuts"] = e.get("holds", ""), e.get("cuts", "")
        e.update(holds=holds, cuts=cuts, auto_cuts=True, auto_cue="vision", auto_frac=round(frac, 2),
                 shots=shots, vision=f"{rep.get('category', '')} {rep.get('title', '')} — {rep.get('note', '')}".strip())
        if ns < start - 1e-3:
            e["start"] = hms(ns)
        if ne > end + 1e-3:
            e["end"] = hms(ne)
        changed += 1
    if usage["input_tokens"]:
        print(f"\ntokens: {usage['input_tokens']} in, {usage['output_tokens']} out ({a.model})")
    if changed:
        with open(rp, "w", encoding="utf-8") as f:
            json.dump(edits, f, indent=1)
        n = write_reviewed(a.work)
        print(f"{changed} rows updated in {rp}; {n} accepted rows -> segments_reviewed.csv. Reload the review page.")


if __name__ == "__main__":
    main()
