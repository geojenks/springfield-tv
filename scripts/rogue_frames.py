#!/usr/bin/env python3
"""Find and patch rogue frames: 1-3 stray frames of another shot left by a hold or cut that lands a
frame or two off (or a start/end a frame short), which flash by in the finished clip.

  python scripts/rogue_frames.py --source <eps> [--ids ... | --all] [--max 3] [--near 2] [--anywhere]
                                 [--apply] [--render]

Per accepted row the clip's span is decoded at low resolution (with ffmpeg's own timestamps, the
clock the cutter's trims use), the holds/cuts are applied virtually, shot boundaries are found in the
result with vision_cuts.shot_bounds (min_len 1, so a single odd frame is its own shot), and every run
of short shots totalling <= --max frames that starts or ends within --near frames of a hold/cut edge
or the clip's start/end is a rogue run (--anywhere drops the distance test, but a
cartoon has real 1-frame flashes). The fix is always a hold, so the audio is untouched:

  the run is the frame a hold `a-b` froze on   -> b moves past the run (freeze on the next shot)
  the run is the frame a hold `a*-b` froze on  -> a moves back one frame (freeze on the previous one)
  run at the clip's end or right before a cut  -> hold `prev*-b` (the frame before covers it; the
                                                  frame after would be the room)
  otherwise                                    -> hold over the run, frozen on the frame after it
                                                  (merged into a hold that starts where the run ends)

Rows are reported with the run's time, length and the fix. With --apply the holds are written to
work/review.json with prev_holds/prev_cuts stashed and auto_cuts=true / auto_cue=rogue, so the review
page's "auto-cut, to check" filter lists them (k keep, r revert); `rogue` on the row is the list of
runs. --render also re-encodes work/preview/<id>.mp4 so the result can be played there.
"""
import argparse, json, os, re, subprocess, sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from extract_clips import secs, find_source, fps_of, render_clip, ranges  # noqa: E402
from review_server import load_rows, write_reviewed  # noqa: E402
from vision_cuts import shot_bounds, TW, TH  # noqa: E402
from auto_cuts import hms  # noqa: E402


def hold_str(a, b, at_start):
    return f"{hms(a)}{'*' if at_start else ''}-{hms(b)}"


def decode_pts(path, start, dur, w=TW, h=TH):
    """(frames, pts): low-res RGB frames of [start, start+dur) and each frame's timestamp on the
    clock edit_filter's trims use (ffmpeg re-bases at the -ss point; showinfo reports that clock)."""
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "info", "-nostats", "-ss", f"{start:.3f}", "-t", f"{dur:.3f}",
           "-i", path, "-vf", f"scale={w}:{h},showinfo", "-fps_mode", "passthrough", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    r = subprocess.run(cmd, capture_output=True, check=True)
    n = len(r.stdout) // (w * h * 3)
    frames = np.frombuffer(r.stdout, np.uint8)[: n * w * h * 3].reshape(n, h, w, 3)
    pts = [float(m) for m in re.findall(r"pts_time:\s*([\d.]+)", r.stderr.decode("utf-8", "replace"))]
    if len(pts) != n:                                   # showinfo went missing: fall back to the frame grid
        print(f"  (showinfo gave {len(pts)} timestamps for {n} frames; using a 30 fps grid)", file=sys.stderr)
        pts = [i / 30.0 for i in range(n)]
    return frames, start + np.array(pts[:n])


def analyse(src, fps, start, end, holds, cuts, mx, near, anywhere):
    """-> (rogues, new_holds): rogues = [(t_first, n_frames, fix)], new_holds = hold string."""
    fr = 1.0 / fps
    frames, pts = decode_pts(src, start, end - start)
    m = pts < end - 1e-4                                          # trim drops pts >= end
    frames, pts = frames[m], pts[m]
    n = len(frames)
    if n < 3:
        return [], holds
    eps = 1e-4

    hs = [(a + start, b + start, s) for a, b, s in ranges(holds, start, end)]      # absolute again
    cs = [(a + start, b + start) for a, b, _ in ranges(cuts, start, end)]
    idx = lambda t: int(np.searchsorted(pts, t - eps, side="left"))               # first frame with pts >= t (trim=start)

    def frozen_idx(a, b, s):
        """frame index a hold freezes on: first frame with pts >= b, or last with pts < a + fr/2"""
        k = int(np.searchsorted(pts, a + fr / 2 - eps, side="left")) - 1 if s else idx(b)
        return int(min(max(k, 0), n - 1))

    # the output as a sequence of source frame indices: holds repeat their frozen frame over their
    # slots (applied on the source clock first), then the cuts drop slots
    disp = np.arange(n)
    for a, b, s in hs:
        disp[idx(a):idx(b)] = frozen_idx(a, b, s)
    keep = np.ones(n, bool)
    for a, b in cs:
        keep[idx(a):idx(b)] = False
    seq, slot = disp[keep], pts[keep]
    if len(seq) < 3:
        return [], holds
    bounds = shot_bounds(frames[seq], min_len=1)
    shots = list(zip(bounds, bounds[1:] + [len(seq)]))          # [i, j) in output order
    runs, k = [], 0                                             # neighbouring short shots are one run
    while k < len(shots):
        i, j = shots[k]
        if j - i > mx:
            k += 1; continue
        while k + 1 < len(shots) and shots[k + 1][1] - shots[k + 1][0] <= mx:
            k += 1; j = shots[k][1]
        runs.append((i, j)); k += 1

    slack = near * fr + 1e-3
    edges = [start, end] + [t for a, b in cs for t in (a, b)] + [t for a, b, s in hs for t in (a, b)]
    rogues, new = [], list(hs)
    for i, j in runs:
        if j - i > mx:
            continue
        si, sj = int(seq[i]), int(seq[j - 1]) + 1                 # source frames shown in the run
        t_i = pts[si]                                             # source time of the run's first frame
        t_j = pts[sj] if sj < n else end                          # ... of the frame after it
        if not anywhere and min(abs(t - x) for t in (t_i, t_j) for x in edges) > slack:
            continue
        fix = None
        for k, (a, b, s) in enumerate(new):
            f = frozen_idx(a, b, s)
            if si <= f < sj:                                      # a hold froze on the rogue frame
                if s:
                    if si == 0:
                        break
                    new[k] = (pts[si - 1] + fr / 4, b, s); fix = "a*-b hold now freezes on the frame before"
                else:
                    new[k] = (a, pts[sj] - fr / 2 if sj < n else end, s); fix = "a-b hold extended over the run"
                break
        if fix is None:
            at_cut = next((c for c, d in cs if abs(c - t_j) <= slack), None)
            if sj >= n or at_cut is not None:                     # at the clip's end / just before a cut: the
                if si == 0:                                       # frame before covers the run (the frame after
                    continue                                      # is the room)
                a, b = pts[si - 1] + fr / 4, end if sj >= n else at_cut
                k = next((k for k, (x, y, s) in enumerate(new) if s and abs(y - b) <= slack), None)
                if k is not None:                                 # an a*-b hold already ends there: start it earlier
                    new[k] = (min(a, new[k][0]), new[k][1], True); fix = f"hold {hold_str(*new[k])} (started earlier)"
                else:
                    new.append((a, b, True)); fix = f"hold {hold_str(a, b, True)}"
            else:                                                 # the frame after covers the run
                a, b = max(start, pts[si] - fr / 2), pts[sj] - fr / 2
                k = next((k for k, (x, y, s) in enumerate(new) if not s and abs(x - b) <= fr), None)
                if k is not None:                                 # abuts a hold: stretch it back
                    new[k] = (a, new[k][1], False); fix = f"hold {hold_str(*new[k])} (stretched back)"
                else:
                    new.append((a, b, False)); fix = f"hold {hold_str(a, b, False)}"
        rogues.append((hms(t_i), j - i, fix))
    new.sort()
    return rogues, "; ".join(hold_str(a, b, s) for a, b, s in new)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True)
    ap.add_argument("--work", default="work")
    ap.add_argument("--ids", nargs="*", default=[])
    ap.add_argument("--all", action="store_true", help="every accepted row (default: accepted rows with holds/cuts)")
    ap.add_argument("--max", type=int, default=3, help="longest run of frames that counts as rogue")
    ap.add_argument("--near", type=float, default=2, help="frames: a run must start or end this close to a hold/cut edge or the clip's ends")
    ap.add_argument("--anywhere", action="store_true")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--render", action="store_true", help="with --apply: re-render work/preview/<id>.mp4")
    a = ap.parse_args()

    rows, edits = load_rows(a.work)
    rp = os.path.join(a.work, "review.json")
    todo = []
    for r in rows:
        e = edits.get(r["id"], {})
        if a.ids:
            if r["id"] in a.ids:
                todo.append(r)
        elif e.get("status") == "accept" and (a.all or e.get("holds") or e.get("cuts")):
            todo.append(r)
    print(f"{len(todo)} rows to scan")
    fps_cache, hit_rows, n_rogue = {}, 0, 0
    for r in todo:
        e = edits.get(r["id"], {})
        ep = r["episode"]
        src = find_source(a.source, ep[1:3], ep[4:6])
        if not src:
            print(r["id"], "no source file"); continue
        if src not in fps_cache:
            fps_cache[src] = fps_of(src)
        fps = fps_cache[src]
        start, end = secs(e.get("start") or r["start"]), secs(e.get("end") or r["end"])
        holds, cuts = e.get("holds", ""), e.get("cuts", "")
        rogues, new_holds = analyse(src, fps, start, end, holds, cuts, a.max, a.near, a.anywhere)
        if not rogues:
            continue
        hit_rows += 1; n_rogue += len(rogues)
        print(f"\n{r['id']}  {hms(start)}-{hms(end)}  holds: {holds or '-'}  cuts: {cuts or '-'}")
        for t, k, fix in rogues:
            print(f"   {t}  {k} frame{'s' if k > 1 else ''}  -> {fix}")
        print(f"   holds become: {new_holds}")
        if a.apply:
            cur = json.load(open(rp, encoding="utf-8")) if os.path.exists(rp) else {}
            row = cur.setdefault(r["id"], {})
            if row.get("prev_holds") is None:
                row["prev_holds"], row["prev_cuts"] = row.get("holds", ""), row.get("cuts", "")
            row.update(holds=new_holds, auto_cuts=True, auto_cue="rogue",
                       rogue=[[t, k, fix] for t, k, fix in rogues])
            if a.render:
                pdir = os.path.join(a.work, "preview"); os.makedirs(pdir, exist_ok=True)
                render_clip(src, start, end, new_holds, cuts, os.path.join(pdir, r["id"] + ".mp4"), fps)
                row["preview"] = True
            with open(rp, "w", encoding="utf-8") as f:
                json.dump(cur, f, indent=1)
    print(f"\n{n_rogue} rogue runs in {hit_rows} rows" + ("" if a.apply else " (dry run: add --apply)"))
    if a.apply and hit_rows:
        write_reviewed(a.work)


if __name__ == "__main__":
    main()
