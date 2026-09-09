#!/usr/bin/env python3
"""Scan episode files for TV-within-the-show segments and write candidates for review.

Signals (cheapest first):
  * subtitles (.srt beside the video)      -> keyword hits (Troy McClure, Kent Brockman, I&S theme)
  * yellow ratio at 1 fps                  -> runs with no Simpsons-skin yellow = probably a cartoon/insert
  * perceptual hash vs refs/*.png          -> frames that look like known title cards / news desk

Outputs:
  work/candidates.csv    id, file, season, episode, signal, start, end, score
  work/review.html       contact sheet (start / mid / end thumbs) for each candidate
  work/<episode>/        thumbs used by the review page

Usage:
  python scripts/scan_episodes.py --source ~/Videos/Simpsons --out work [--seasons 1-10]
Requires: ffmpeg on PATH, numpy, Pillow.  (pip install numpy pillow)
"""
import argparse, csv, glob, html, os, re, subprocess, sys
import numpy as np
from PIL import Image

W, H = 96, 54                     # analysis resolution
MIN_RUN = 8                       # seconds without yellow to count as a candidate
YELLOW_THRESH = 0.02              # fraction of frame that is skin-yellow
HASH_DIST = 10                    # dHash Hamming distance for a reference match
SUB_PATTERNS = {
    "troy_mcclure": r"troy mcclure|you may remember me",
    "kent_brockman": r"kent brockman|channel 6 news|eye on springfield",
    "itchy_scratchy": r"itchy|scratchy|they fight|bite and fight",
}

# ---------- frame decoding ----------
def frames_1fps(path, w=W, h=H):
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", path,
           "-vf", f"fps=1,scale={w}:{h}", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    raw = subprocess.run(cmd, capture_output=True, check=True).stdout
    n = len(raw) // (w * h * 3)
    return np.frombuffer(raw, np.uint8)[: n * w * h * 3].reshape(n, h, w, 3)

def yellow_ratio(f):
    r, g, b = f[..., 0].astype(int), f[..., 1].astype(int), f[..., 2].astype(int)
    m = (r > 180) & (g > 150) & (b < 120) & (r - b > 90) & (abs(r - g) < 70)
    return m.mean(axis=(1, 2))

def dhash(img):                   # img: (H,W,3) uint8 -> 64-bit int
    g = Image.fromarray(img).convert("L").resize((9, 8), Image.BILINEAR)
    a = np.asarray(g, dtype=int)
    bits = (a[:, 1:] > a[:, :-1]).flatten()
    return int("".join("1" if b else "0" for b in bits), 2)

def hamming(a, b): return bin(a ^ b).count("1")

# ---------- signals ----------
def runs_below(y, thresh, min_len):
    low = y < thresh
    out, start = [], None
    for i, v in enumerate(np.append(low, False)):
        if v and start is None: start = i
        elif not v and start is not None:
            if i - start >= min_len: out.append((start, i))
            start = None
    return out

def hash_hits(frames, refs):
    hits = []
    for t in range(len(frames)):
        h = dhash(frames[t])
        for name, rh in refs.items():
            if hamming(h, rh) <= HASH_DIST:
                hits.append((t, name)); break
    # merge consecutive seconds
    merged = []
    for t, name in hits:
        if merged and merged[-1][1] == name and t - merged[-1][0] <= 3:
            merged[-1] = (merged[-1][0], name, t)
        else:
            merged.append((t, name, t))
    return [(s, e + 1, n) for s, n, e in merged]

def srt_hits(srt_path):
    if not os.path.exists(srt_path): return []
    txt = open(srt_path, encoding="utf-8", errors="ignore").read()
    hits = []
    for block in re.split(r"\n\s*\n", txt):
        m = re.search(r"(\d+):(\d+):(\d+)[,.](\d+)\s*-->", block)
        if not m: continue
        t = int(m[1]) * 3600 + int(m[2]) * 60 + int(m[3])
        body = " ".join(block.splitlines()[2:]).lower()
        for name, pat in SUB_PATTERNS.items():
            if re.search(pat, body):
                hits.append((t, name))
    return hits

# ---------- helpers ----------
def hms(s): return f"{s//3600:02d}:{(s%3600)//60:02d}:{s%60:02d}"

def tag_of(path):
    m = re.search(r"S(\d{1,2})E(\d{1,2})", os.path.basename(path), re.I)
    return (int(m[1]), int(m[2])) if m else (None, None)

def save_thumb(frame, path):
    Image.fromarray(frame).resize((W * 2, H * 2)).save(path)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True)
    ap.add_argument("--out", default="work")
    ap.add_argument("--refs", default="refs", help="folder of reference PNGs (name = label)")
    ap.add_argument("--seasons", default=None, help="e.g. 1-10")
    a = ap.parse_args()

    lo, hi = (map(int, a.seasons.split("-")) if a.seasons else (0, 99))
    refs = {os.path.splitext(os.path.basename(p))[0]:
            dhash(np.asarray(Image.open(p).convert("RGB").resize((W, H))))
            for p in glob.glob(os.path.join(a.refs, "*.png"))}
    print(f"{len(refs)} reference images")

    vids = sorted(p for p in glob.glob(os.path.join(a.source, "**", "*"), recursive=True)
                  if p.lower().endswith((".mkv", ".mp4", ".avi", ".m4v"))
                  and tag_of(p)[0] and lo <= tag_of(p)[0] <= hi)
    os.makedirs(a.out, exist_ok=True)
    rows, cards = [], []

    for path in vids:
        s, e = tag_of(path)
        ep = f"S{s:02d}E{e:02d}"
        print(ep, "...", end=" ", flush=True)
        try:
            fr = frames_1fps(path)
        except subprocess.CalledProcessError:
            print("decode failed"); continue
        thumbdir = os.path.join(a.out, ep); os.makedirs(thumbdir, exist_ok=True)
        cands = []
        for st, en in runs_below(yellow_ratio(fr), YELLOW_THRESH, MIN_RUN):
            cands.append((st, en, "no_yellow", en - st))
        for st, en, name in hash_hits(fr, refs):
            cands.append((st, min(en + 60, len(fr)), f"ref:{name}", 100))
        for t, name in srt_hits(os.path.splitext(path)[0] + ".srt"):
            cands.append((max(t - 5, 0), min(t + 60, len(fr)), f"srt:{name}", 100))
        cands.sort()
        for i, (st, en, sig, sc) in enumerate(cands):
            cid = f"{ep}-{i:02d}"
            rows.append(dict(id=cid, file=os.path.basename(path), season=s, episode=e,
                             signal=sig, start=hms(st), end=hms(en), score=sc))
            thumbs = []
            for k, t in enumerate((st, (st + en) // 2, max(en - 1, st))):
                tp = os.path.join(thumbdir, f"{cid}_{k}.jpg"); save_thumb(fr[t], tp)
                thumbs.append(os.path.relpath(tp, a.out))
            cards.append((cid, sig, hms(st), hms(en), thumbs))
        print(f"{len(cands)} candidates")

    with open(os.path.join(a.out, "candidates.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=rows[0].keys() if rows else ["id"])
        w.writeheader(); w.writerows(rows)

    with open(os.path.join(a.out, "review.html"), "w") as f:
        f.write("<meta charset=utf-8><style>body{font:14px system-ui;background:#111;color:#eee}"
                ".c{display:flex;gap:6px;align-items:center;padding:6px;border-bottom:1px solid #333}"
                "img{width:192px}.l{width:260px}</style><h2>Candidates</h2>")
        for cid, sig, st, en, thumbs in cards:
            f.write(f"<div class=c><div class=l><b>{cid}</b><br>{html.escape(sig)}<br>{st} → {en}</div>"
                    + "".join(f"<img src='{t}'>" for t in thumbs) + "</div>")
    print(f"\n{len(rows)} candidates -> {a.out}/candidates.csv and review.html")

if __name__ == "__main__":
    main()
