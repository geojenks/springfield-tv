#!/usr/bin/env python3
"""Anchor-plus-boundary segment finder.

1. Anchors: subtitle lines in work/subs/SxxEyy.json (from fetch_frinkiac_subs.py) that
   match a category regex. Nearby anchors of one category merge into one.
2. Boundaries, per anchor, from 1 fps thumbnails of the episode file:
     bezel  - all four corners are the in-show TV's purple bezel.                Primary.
     mask   - screen-POV: each corner diagonal is a flat plateau (bezel or rounded-corner
              mask of any colour) then a step into the picture. Catches season-1 grey masks,
              wooden TVs, full-screen news/adverts drawn with rounded corners.
              Both are bridged over gaps <= BRIDGE s (cutaways to the sofa).
     yellow - run with no Simpsons-skin yellow (full-screen inserts).      Fallback.
   The run nearest the anchor (within SEARCH s) becomes the segment; else anchor +-FALLBACK.
   --anchorless also reports screen runs >= MIN_SCREEN s that no anchor claimed (category
   "screen", method screen_only) with the dialogue heard inside them.
3. Writes work/segments.csv and work/segments.html (frames just outside and inside each
   boundary, plus a bezel/yellow timeline strip, and a tick box + CSV export).
   screen_spans lists the on-screen sub-runs inside a segment (gaps = cutaways to the sofa),
   for splicing at extraction time.

Usage: python scripts/find_segments.py --source <episodes dir> [--seasons 5-7] [--cats troy_mcclure,kent_brockman]
Requires ffmpeg, numpy, Pillow.
"""
import argparse, csv, glob, html, json, os, re, sys
import numpy as np
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from scan_episodes import frames_1fps, yellow_ratio, dhash, hamming, runs_below, hms, tag_of

W, H = 160, 120   # decode size; the mask cue needs ~24 px along each corner diagonal

CATS = {
    "troy_mcclure":   r"troy mcclure|you (may|might) remember me",
    "kent_brockman":  r"i'm kent brockman|this is kent brockman|kent brockman (reporting|here|with)|over to kent brockman|eye on springfield|channel (6|six) news",
    "krusty":         r"krusty the (clown|klown) show|hey,? hey,? kids",
    "itchy_scratchy": r"♪.*itchy|itchy.*♪|the itchy .{0,3}scratchy show",   # sung title line, not mentions
    "mcbain":         r"mcbain",
    "bumblebee_man":  r"bumblebee man|bumblebee guy",
    "bumper":         r"we now return to|and now,? back to|we'll be right back|coming up next|stay tuned|brought to you by",
    "advert":         r"order now|call now|operators are standing by|1-800|not available in stores|act now|but wait,? there's more",
}
MERGE = 20        # s: anchors of one category closer than this merge
SEARCH = 45       # s: how far from an anchor a boundary run may start
BRIDGE = 8        # s: bezel gaps shorter than this are bridged (sofa cutaways)
MIN_BEZEL = 6     # s: minimum bezel run
MIN_SCREEN = 8    # s: minimum unanchored screen run (--anchorless)
MIN_YELLOW = 8
FALLBACK = 25     # s: half-width when no run is found
PAD = 1           # s: padding on each side of a found run
YELLOW = 0.02


# ---------- per-second features ----------
def bezel_flags(fr, c=10):
    """All four corners are the in-show TV's purple bezel (~RGB 55,38,88) and the centre is lit."""
    f = fr.astype(int)
    corners = np.stack([f[:, :c, :c * 2], f[:, :c, -c * 2:], f[:, -c:, :c * 2], f[:, -c:, -c * 2:]], 1)  # n,4,c,2c,3
    m = corners.mean(axis=(2, 3))                                      # n,4,3
    r, g, b = m[..., 0], m[..., 1], m[..., 2]
    purple = (r > 30) & (r < 85) & (g > 15) & (g < 65) & (b > 60) & (b < 115) & (b > r) & (r > g)
    flat = corners.reshape(len(f), 4, -1, 3).std(axis=2).max(axis=2) < 45
    centre = f[:, H // 4:3 * H // 4, W // 4:3 * W // 4].mean(axis=(1, 2, 3)) > 50
    return (purple & flat).all(axis=1) & centre


def _mask(f, plat, stp, flat, step):
    n, H_, W_ = f.shape[:3]
    K = list(range(plat[0], stp[1]))
    diags = [np.stack([f[:, k, k] for k in K], 1), np.stack([f[:, k, W_ - 1 - k] for k in K], 1),
             np.stack([f[:, H_ - 1 - k, k] for k in K], 1), np.stack([f[:, H_ - 1 - k, W_ - 1 - k] for k in K], 1)]
    npl = plat[1] - plat[0]
    means = [d[:, :npl].mean(1) for d in diags]
    flats = [d[:, :npl].std(1).max(1) < flat for d in diags]
    steps = [np.abs(d[:, npl:] - m[:, None]).mean(2).max(1) > step for d, m in zip(diags, means)]
    pairs = (np.abs(means[0] - means[2]).mean(1) < 25) & (np.abs(means[1] - means[3]).mean(1) < 25)
    return np.all(flats, 0) & np.all(steps, 0) & pairs


def mask_flags(fr):
    """Screen-POV mask: along each corner diagonal (border pixels 0-2 skipped, rips have a dark
    edge) a flat plateau, then a step of >30 grey levels into the picture; vertically paired corners
    share the plateau colour. Two plateau widths cover thin rounded masks and thick bezels."""
    f = fr.astype(int)
    centre = f[:, H // 4:3 * H // 4, W // 4:3 * W // 4].mean(axis=(1, 2, 3)) > 50
    return (_mask(f, (3, 9), (10, 23), 12, 35) | _mask(f, (5, 10), (10, 24), 12, 30)) & centre


def runs_true(flags, min_len, bridge=0):
    idx = np.where(flags)[0]
    runs = []
    for t in idx:
        if runs and t - runs[-1][1] <= bridge + 1:
            runs[-1][1] = t
        else:
            runs.append([t, t])
    return [(a, b + 1) for a, b in runs if b + 1 - a >= min_len]


# ---------- anchors ----------
def anchors_for(subs, cats):
    hits = []
    for s in subs:
        c = s["Content"].lower()
        for name in cats:
            if re.search(CATS[name], c):
                hits.append((s["StartTimestamp"] // 1000, name, s["Content"]))
    hits.sort()
    merged = []
    for t, name, txt in hits:
        if merged and merged[-1]["cat"] == name and t - merged[-1]["last"] <= MERGE:
            merged[-1]["last"] = t
            merged[-1]["lines"].append(txt)
        else:
            merged.append(dict(t=t, last=t, cat=name, lines=[txt]))
    return merged


def pick_run(runs, t0, t1):
    """closest run to the anchor span [t0,t1]; distance 0 if it overlaps."""
    best, bd = None, None
    for a, b in runs:
        d = 0 if (a <= t1 and b >= t0) else min(abs(a - t1), abs(t0 - b))
        if d <= SEARCH and (bd is None or d < bd):
            best, bd = (a, b), d
    return best, bd


def _sec(h):
    a, b, c = map(int, h.split(":")); return a * 3600 + b * 60 + c


def dedupe(rows, cards, key):
    """Merge this episode's rows whose spans overlap by > 50% of the shorter one; keep the
    higher-confidence cut, join categories and anchor text."""
    mine = [r for r in rows if r["episode"] == key]
    mine.sort(key=lambda r: -r["confidence"])
    kept = []
    for r in mine:
        s0, e0 = _sec(r["start"]), _sec(r["end"])
        for k in kept:
            s1, e1 = _sec(k["start"]), _sec(k["end"])
            ov = min(e0, e1) - max(s0, s1)
            if ov > 0.5 * min(e0 - s0, e1 - s1):
                if r["category"] not in k["category"].split("+"):
                    k["category"] += "+" + r["category"]
                k["anchor_text"] = (k["anchor_text"] + " / " + r["anchor_text"])[:200]
                break
        else:
            kept.append(r)
    drop = {id(r) for r in mine} - {id(r) for r in kept}
    rows[:] = [r for r in rows if id(r) not in drop]
    cards[:] = [c for c in cards if id(c[0]) not in drop]


# ---------- output ----------
def thumb(fr, t, path):
    t = max(0, min(t, len(fr) - 1))
    Image.fromarray(fr[t]).resize((320, 240)).save(path, quality=80)


def strip(bz, yl, a, b, path):
    """timeline strip for [a,b): green = bezel, blue = no-yellow (top half)."""
    n = max(b - a, 1)
    img = np.zeros((12, n, 3), np.uint8) + 40
    for i, t in enumerate(range(a, b)):
        if 0 <= t < len(bz):
            if bz[t]:
                img[:, i] = (60, 200, 60)
            if yl[t]:
                img[:6, i] = (60, 120, 220)
    Image.fromarray(img).resize((n * 4, 12), Image.NEAREST).save(path)


HEAD = """<meta charset=utf-8><title>segments</title><style>
body{font:13px system-ui;background:#111;color:#eee;margin:0;padding:10px}
.c{display:grid;grid-template-columns:300px repeat(5,196px);gap:4px;align-items:start;padding:8px 4px;border-bottom:1px solid #333}
.c img.t{width:192px;border:2px solid #333}.c .pre img,.c .post img{opacity:.55}
.c .start img,.c .end img{border-color:#4c4}.lab{font-size:11px;color:#aaa}
.strip{grid-column:1/-1;overflow-x:auto}.strip img{height:12px;image-rendering:pixelated}
.m{color:#8f8}.m.mask{color:#6cf}.m.screen_only{color:#c9f}.m.no_yellow{color:#fc6}.m.anchor_only{color:#f66}
button{font:13px system-ui;padding:6px 10px}
textarea{width:100%;height:90px;background:#000;color:#ccc}
.hdr{position:sticky;top:0;background:#111;padding:6px 0;z-index:1}
</style><div class=hdr><button onclick=exp()>export ticked rows as CSV</button>
<span class=lab> green strip = inside TV bezel, blue = no yellow; strip spans 30 s either side of the cut. Faded thumbs are 2 s outside the cut.</span>
<textarea id=out></textarea></div>
<script>function exp(){const r=[...document.querySelectorAll('input:checked')].map(c=>c.dataset.row);
document.getElementById('out').value='id,episode,category,start,end\\n'+r.join('\\n')}</script>
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True)
    ap.add_argument("--subs", default="work/subs")
    ap.add_argument("--out", default="work")
    ap.add_argument("--refs", default="refs")
    ap.add_argument("--seasons", default="1-99")
    ap.add_argument("--cats", default=",".join(CATS))
    ap.add_argument("--anchorless", action="store_true",
                    help="also report screen runs >= MIN_SCREEN s that no anchor claimed (category 'screen')")
    ap.add_argument("--merge", action="store_true",
                    help="keep the rows already in <out>/segments.csv and skip episodes that have any; "
                         "new episodes are appended (review.json and existing thumbs are never touched)")
    a = ap.parse_args()
    lo, hi = map(int, a.seasons.split("-"))
    cats = a.cats.split(",")

    refs = {os.path.splitext(os.path.basename(p))[0]:
            dhash(np.asarray(Image.open(p).convert("RGB").resize((W, H))))
            for p in glob.glob(os.path.join(a.refs, "*.png"))}

    vids = {}
    for p in glob.glob(os.path.join(a.source, "**", "*"), recursive=True):
        s, e = tag_of(p)
        if s and p.lower().endswith((".mkv", ".mp4", ".avi", ".m4v")):
            vids[(s, e)] = p

    os.makedirs(a.out, exist_ok=True)
    seg_csv = os.path.join(a.out, "segments.csv")
    old, done = [], set()
    if a.merge and os.path.exists(seg_csv):
        old = list(csv.DictReader(open(seg_csv, encoding="utf-8")))
        done = {r["episode"] for r in old}
        print(f"merge: keeping {len(old)} rows over {len(done)} episodes already scanned")
    rows, cards = [], []
    for sp in sorted(glob.glob(os.path.join(a.subs, "S??E??.json"))):
        key = os.path.basename(sp)[:6]
        s, e = int(key[1:3]), int(key[4:6])
        if not lo <= s <= hi or (s, e) not in vids or key in done:
            continue
        subs = json.load(open(sp, encoding="utf-8")).get("Subtitles") or []
        anchors = anchors_for(subs, cats)
        if not anchors and not a.anchorless:
            continue
        print(key, len(anchors), "anchors ...", end=" ", flush=True)
        fr = frames_1fps(vids[(s, e)], W, H)
        yr = yellow_ratio(fr)
        pz = bezel_flags(fr)
        bz = pz | mask_flags(fr)
        yl = yr < YELLOW
        bruns = runs_true(bz, MIN_BEZEL, BRIDGE)
        yruns = runs_below(yr, YELLOW, MIN_YELLOW)
        if refs:
            rf = np.array([min(hamming(dhash(f), r) for r in refs.values()) <= 10 for f in fr])
        else:
            rf = np.zeros(len(fr), bool)
        tdir = os.path.join(a.out, "seg", key)
        os.makedirs(tdir, exist_ok=True)
        if a.anchorless:                                   # screen runs nobody anchored
            for r0, r1 in bruns:
                if r1 - r0 < MIN_SCREEN or any(min(r1, x["last"] + SEARCH) - max(r0, x["t"] - SEARCH) > 0 for x in anchors):
                    continue
                said = [l["Content"].replace(chr(10), " ") for l in subs if r0 <= l["StartTimestamp"] / 1000 < r1]
                anchors.append(dict(cat="screen", t=r0, last=r1 - 1, lines=said[:6] or ["(no dialogue)"], free=True))
        for i, an in enumerate(anchors):
            t0, t1 = an["t"], an["last"]
            run, d = pick_run(bruns, t0, t1)
            method = "bezel" if run and pz[run[0]:run[1]].mean() > 0.5 else "mask"
            if an.get("free"):
                method = "screen_only"
            if run is None:
                run, d = pick_run(yruns, t0, t1)
                method = "no_yellow"
            if run is None:
                run, d, method = (max(t0 - FALLBACK, 0), min(t1 + FALLBACK, len(fr))), None, "anchor_only"
            st, en = max(run[0] - PAD, 0), min(run[1] + PAD, len(fr))
            for t in range(max(st - 10, 0), st):        # title card just before the run
                if rf[t]:
                    st = t
                    method += "+ref"
                    break
            conf = {"bezel": 0.9, "mask": 0.7, "no_yellow": 0.5, "anchor_only": 0.2, "screen_only": 0.4}[method.split("+")[0]]
            if d:
                conf *= max(0.3, 1 - d / SEARCH)
            cid = f"{key}-{an['cat']}-{i:02d}"
            spans = ";".join(f"{hms(x)}-{hms(y)}" for x, y in runs_true(bz[st:en], 2) for x, y in [(x + st, y + st)])
            rows.append(dict(id=cid, episode=key, category=an["cat"], anchor=hms(t0),
                             start=hms(st), end=hms(en), dur=en - st, method=method,
                             confidence=round(conf, 2), screen_spans=spans,
                             anchor_text=" / ".join(an["lines"])[:200]))
            thumbs = []
            for lab, t in (("pre", st - 2), ("start", st), ("mid", (st + en) // 2), ("end", en - 1), ("post", en + 1)):
                tp = os.path.join(tdir, f"{cid}_{lab}.jpg")
                thumb(fr, t, tp)
                thumbs.append((lab, os.path.relpath(tp, a.out).replace(os.sep, "/")))
            spath = os.path.join(tdir, f"{cid}_strip.png")
            strip(bz, yl, st - 30, en + 30, spath)
            cards.append((rows[-1], thumbs, os.path.relpath(spath, a.out).replace(os.sep, "/")))
        dedupe(rows, cards, key)
        print(f"{len(bruns)} bezel runs, {len(yruns)} yellow runs")

    allrows = sorted(old + rows, key=lambda r: (r["episode"], r["start"]))
    fields = list(rows[0].keys()) if rows else (list(old[0].keys()) if old else ["id"])
    with open(seg_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(allrows)

    with open(os.path.join(a.out, "segments.html"), "w", encoding="utf-8") as f:
        f.write(HEAD)
        for r, thumbs, spath in cards:
            row = f"{r['id']},{r['episode']},{r['category']},{r['start']},{r['end']}"
            f.write(f"<div class=c><div><input type=checkbox data-row='{row}'> <b>{r['id']}</b><br>"
                    f"<span class='m {r['method'].split('+')[0]}'>{r['method']}</span> conf {r['confidence']}<br>"
                    f"{r['start']} → {r['end']} ({r['dur']} s) &nbsp; anchor {r['anchor']}<br>"
                    f"<span class=lab>{html.escape(r['anchor_text'][:160])}</span></div>")
            for lab, tp in thumbs:
                f.write(f"<div class={lab}><img class=t src='{tp}'><div class=lab>{lab}</div></div>")
            f.write(f"<div class=strip><img src='{spath}'></div></div>\n")
    print(f"\n{len(rows)} new segments ({len(allrows)} total) -> {seg_csv}; {a.out}/segments.html lists the new ones")


if __name__ == "__main__":
    main()
