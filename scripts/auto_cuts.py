#!/usr/bin/env python3
"""Frame-accurate bezel cuts for accepted segments.

For each accepted row in work/segments_reviewed.csv whose boundary method is `bezel` (or every
accepted row with --all), decode every frame of the segment at 160x120 and test the purple
bezel cue frame by frame (find_segments.bezel_flags). Then:

  * frames before the first bezel frame -> a hold: audio from `start`, picture frozen on the
    first bezel frame (the I&S theme playing over the sofa)
  * interior runs of non-bezel frames   -> cuts: video and audio removed
  * frames after the last bezel frame   -> a cut to `end`

Runs shorter than --min-gap frames (either way) are ignored as flicker. Results are written to
work/review.json (rows that already have holds or cuts are left alone unless --force; the row
gets auto_cuts=true) and work/segments_reviewed.csv is rebuilt. RELOAD the review page
afterwards: an open tab still holds the old edits and would save over these.

  python scripts/auto_cuts.py --source <eps> [--work work] [--all] [--force] [--min-gap 4] [--min-frac 0.5] [--todo] [--ids ID ...]

Rows where fewer than --min-frac of the frames pass the bezel test are skipped (a full-screen
programme would otherwise get most of itself cut). On the review page the "auto-cut, to check"
status filter lists the rows this touched; "keep cuts" (k) or "undo all cuts" (u) clears the flag.
"""
import argparse, csv, json, os, subprocess, sys
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from find_segments import bezel_flags, W, H
from extract_clips import secs, find_source
from review_server import write_reviewed


def hms(s):
    s = max(0.0, s)
    return f"{int(s // 3600):02d}:{int(s % 3600 // 60):02d}:{s % 60:06.3f}"


def fps_of(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                          "stream=r_frame_rate", "-of", "csv=p=0", path],
                         capture_output=True, text=True, check=True).stdout.strip()
    n, d = out.split("/")
    return float(n) / float(d)


def frames(path, start, dur):
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", f"{start:.3f}", "-t", f"{dur:.3f}",
           "-i", path, "-vf", f"scale={W}:{H}", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    raw = subprocess.run(cmd, capture_output=True, check=True).stdout
    n = len(raw) // (W * H * 3)
    return np.frombuffer(raw, np.uint8)[: n * W * H * 3].reshape(n, H, W, 3)


def runs(flags):
    """Half-open (i0, i1) runs where flags is True."""
    out, i, n = [], 0, len(flags)
    while i < n:
        if flags[i]:
            j = i
            while j < n and flags[j]:
                j += 1
            out.append((i, j)); i = j
        else:
            i += 1
    return out


def despeckle(flags, k):
    """Flip runs shorter than k frames, first the True runs then the False runs."""
    f = flags.copy()
    for a, b in runs(f):
        if b - a < k:
            f[a:b] = False
    for a, b in runs(~f):
        if b - a < k:
            f[a:b] = True
    return f


def auto_edit(bz, start, fps):
    """bezel flags per frame -> (holds, cuts) strings in absolute episode time."""
    t = lambda i: start + i / fps
    on = runs(bz)
    if not on:
        return None, None
    holds, cuts = [], []
    first, last = on[0][0], on[-1][1]
    if first > 0:
        holds.append(f"{hms(start)}-{hms(t(first))}")
    for (a0, a1), (b0, b1) in zip(on, on[1:]):
        cuts.append(f"{hms(t(a1))}-{hms(t(b0))}")
    if last < len(bz):
        cuts.append(f"{hms(t(last))}-{hms(t(len(bz)))}")
    return "; ".join(holds), "; ".join(cuts)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True)
    ap.add_argument("--work", default="work")
    ap.add_argument("--all", action="store_true", help="every accepted row, not just method=bezel")
    ap.add_argument("--force", action="store_true", help="overwrite existing holds/cuts")
    ap.add_argument("--min-gap", type=int, default=4, help="ignore runs shorter than this many frames")
    ap.add_argument("--ids", nargs="*", help="only these segment ids")
    ap.add_argument("--min-frac", type=float, default=0.5,
                    help="skip a row unless at least this fraction of its frames pass the bezel test")
    ap.add_argument("--todo", action="store_true",
                    help="also unreviewed rows from work/segments.csv, so a fresh row opens with its cuts prefilled")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    rp = os.path.join(a.work, "review.json")
    edits = json.load(open(rp, encoding="utf-8")) if os.path.exists(rp) else {}
    rows = list(csv.DictReader(open(os.path.join(a.work, "segments_reviewed.csv"), encoding="utf-8")))
    if a.todo:
        seen = {r["id"] for r in rows}
        for r in csv.DictReader(open(os.path.join(a.work, "segments.csv"), encoding="utf-8")):
            e = edits.get(r["id"], {})
            if r["id"] in seen or e.get("status") == "reject":
                continue
            r = dict(r, start=e.get("start") or r["start"], end=e.get("end") or r["end"])
            rows.append(r)
    fps_cache, changed = {}, 0
    for r in rows:
        if a.ids and r["id"] not in a.ids:
            continue
        if not a.all and r.get("method") != "bezel":
            continue
        e = edits.setdefault(r["id"], {})
        if (e.get("holds") or e.get("cuts")) and not a.force:
            print(f"-- {r['id']}: has holds/cuts already, skipped (use --force)"); continue
        src = find_source(a.source, int(r["episode"][1:3]), int(r["episode"][4:6]))
        if not src:
            print(f"!! {r['id']}: no episode file"); continue
        fps = fps_cache.setdefault(src, fps_of(src))
        start, end = secs(r["start"]), secs(r["end"])
        fr = frames(src, start, end - start)
        bz = despeckle(bezel_flags(fr), a.min_gap)
        holds, cuts = auto_edit(bz, start, fps)
        frac = bz.mean() if len(bz) else 0
        if holds is None or frac < a.min_frac:
            print(f"-- {r['id']}: only {frac:.0%} bezel frames ({len(fr)} frames), skipped"); continue
        if not holds and not cuts:
            print(f"-- {r['id']}: {frac:.0%} bezel, nothing to cut"); continue
        print(f"{r['id']}: {len(fr)} frames, {frac:.0%} bezel; holds [{holds}] cuts [{cuts}]")
        if a.dry_run:
            continue
        e["holds"], e["cuts"], e["auto_cuts"] = holds, cuts, True
        changed += 1
    if changed:
        with open(rp, "w", encoding="utf-8") as f:
            json.dump(edits, f, indent=1)
        n = write_reviewed(a.work)
        print(f"\n{changed} rows updated in {rp}; {n} accepted rows -> segments_reviewed.csv. Reload the review page.")


if __name__ == "__main__":
    main()
