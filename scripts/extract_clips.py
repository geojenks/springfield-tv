#!/usr/bin/env python3
"""Cut clips from your own episode files using start/end timestamps.

Input is either a catalog CSV (season, episode, title columns) or work/segments_reviewed.csv
from the review page (episode = SxxEyy, category, cutaways, note, holds, cuts). Only rows with both
`start` and `end` (HH:MM:SS[.ss] or MM:SS) are processed. Episode files are found by
globbing for SxxEyy anywhere under --source. Writes <out>/index.csv describing every clip.

`holds` ("a-b; a-b", absolute episode times): during each a..b the picture is frozen on the
frame at b while the audio continues (I&S theme starting over the sofa; sofa cutaways in the
middle of a cartoon). `cuts` ("a-b; a-b"): video and audio removed, for cutaways where the
show is asynchronous and joins up cleanly without them. Either forces a re-encode for that row.

Usage:
  python scripts/extract_clips.py --source ~/Videos/Simpsons --out clips
  python scripts/extract_clips.py --source ... --catalog work/segments_reviewed.csv --precise
  python scripts/extract_clips.py --source ... --out clips --dry-run
"""
import argparse, csv, glob, os, re, subprocess, sys

def safe(s):
    return re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_")

def find_source(root, season, episode):
    tag = f"S{int(season):02d}E{int(episode):02d}"
    hits = [p for p in glob.glob(os.path.join(root, "**", "*"), recursive=True)
            if tag.lower() in os.path.basename(p).lower()
            and p.lower().endswith((".mkv", ".mp4", ".avi", ".m4v", ".mov"))]
    return hits[0] if hits else None

def secs(h):
    p = [float(x) for x in h.split(":")]
    return p[0] * 3600 + p[1] * 60 + p[2] if len(p) == 3 else p[0] * 60 + p[1] if len(p) == 2 else p[0]

def ranges(txt, start, end):
    """Parse 'a-b; a-b' (absolute times) into sorted (a, b) relative to start, clipped to the clip."""
    out = []
    for h in (txt or "").split(";"):
        if "-" not in h:
            continue
        a, b = (secs(x.strip()) for x in h.split("-", 1))
        a, b = max(a, start) - start, min(b, end) - start
        if b > a:
            out.append((a, b))
    return sorted(out)

def edit_filter(holds, cuts, start, end):
    """ffmpeg filter graph applying cuts (drop video+audio) and holds (freeze picture), or ''.

    Kept intervals are the complement of the cuts. Inside each kept interval the video is
    split into pieces that begin at a hold's end b and run to the next hold's start; each is
    front-padded with clones of its first frame for the hold's length. Audio is just the kept
    intervals. Everything is concatenated into [v] and [a].
    """
    dur = end - start
    cs, hs = ranges(cuts, start, end), ranges(holds, start, end)
    if not cs and not hs:
        return ""
    keep, t = [], 0.0
    for a, b in cs:
        if a > t:
            keep.append((t, a))
        t = max(t, b)
    if dur > t:
        keep.append((t, dur))
    g, vl, al = [], [], []
    for k0, k1 in keep:
        al.append(f"[a{len(al)}]"); g.append(f"[0:a]atrim=start={k0:.3f}:end={k1:.3f},asetpts=PTS-STARTPTS{al[-1]}")
        inner = [(max(a, k0), min(b, k1 - 0.2)) for a, b in hs if b > k0 and a < k1]
        inner = [(a, b) for a, b in inner if b > a]
        pos = k0
        for i, (a, b) in enumerate(inner):
            if a > pos:
                vl.append(f"[v{len(vl)}]"); g.append(f"[0:v]trim=start={pos:.3f}:end={a:.3f},setpts=PTS-STARTPTS{vl[-1]}")
            nxt = inner[i + 1][0] if i + 1 < len(inner) else k1
            vl.append(f"[v{len(vl)}]")
            g.append(f"[0:v]trim=start={b:.3f}:end={nxt:.3f},setpts=PTS-STARTPTS,"
                     f"tpad=start_duration={b - a:.3f}:start_mode=clone{vl[-1]}")
            pos = nxt
        if pos < k1:
            vl.append(f"[v{len(vl)}]"); g.append(f"[0:v]trim=start={pos:.3f}:end={k1:.3f},setpts=PTS-STARTPTS{vl[-1]}")
    g.append(f"{''.join(vl)}concat=n={len(vl)}:v=1:a=0[v]")
    g.append(f"{''.join(al)}concat=n={len(al)}:v=0:a=1[a]")
    return ";".join(g)

def norm(r):
    """Return (season, episode, label) for a catalog row or a reviewed-segments row."""
    m = re.match(r"S(\d{2})E(\d{2})", r.get("episode", ""), re.I)
    if m:
        return int(m.group(1)), int(m.group(2)), r.get("category", "")
    if r.get("season", "").isdigit() and r.get("episode", "").isdigit():
        return int(r["season"]), int(r["episode"]), r.get("title", "")
    return None

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--catalog", default="catalog/itchy_scratchy.csv")
    ap.add_argument("--source", required=True, help="folder containing episode files")
    ap.add_argument("--out", default="clips")
    ap.add_argument("--precise", action="store_true", help="re-encode for frame-accurate cuts")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    os.makedirs(a.out, exist_ok=True)
    rows = csv.DictReader(open(a.catalog, newline="", encoding="utf-8"))
    done = skipped = 0
    index = []
    for r in rows:
        n = norm(r)
        if not (n and r["start"] and r["end"]):
            skipped += 1; continue
        season, episode, label = n
        src = find_source(a.source, season, episode)
        if not src:
            print(f"!! no file for S{season:02d}E{episode:02d} ({r['id']})"); continue
        name = f"S{season:02d}E{episode:02d}_{r['id']}_{safe(label)}.mp4"
        dst = os.path.join(a.out, name)
        index.append(dict(file=name, id=r["id"], episode=f"S{season:02d}E{episode:02d}", category=label,
                          start=r["start"], end=r["end"], cutaways=r.get("cutaways", "0"), note=r.get("note", ""),
                          holds=r.get("holds", ""), cuts=r.get("cuts", "")))
        if os.path.exists(dst):
            continue
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
               "-ss", r["start"], "-t", f"{secs(r['end']) - secs(r['start']):.3f}", "-i", src]
        fc = edit_filter(r.get("holds", ""), r.get("cuts", ""), secs(r["start"]), secs(r["end"]))
        if fc:
            cmd += ["-filter_complex", fc, "-map", "[v]", "-map", "[a]"]
        if a.precise or fc:
            cmd += ["-c:v", "libx264", "-crf", "20", "-preset", "fast", "-c:a", "aac", "-b:a", "128k"]
        else:
            cmd += ["-c", "copy"]  # fast, cuts land on nearest keyframe
        cmd += ["-movflags", "+faststart", dst]
        print(("DRY " if a.dry_run else "") + " ".join(cmd))
        if not a.dry_run:
            subprocess.run(cmd, check=True)
        done += 1
    if index and not a.dry_run:
        with open(os.path.join(a.out, "index.csv"), "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(index[0].keys()))
            w.writeheader(); w.writerows(index)
    print(f"\n{done} clips cut, {skipped} rows still need timestamps, {len(index)} in {a.out}/index.csv")

if __name__ == "__main__":
    main()
