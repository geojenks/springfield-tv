#!/usr/bin/env python3
"""Cut clips from your own episode files using start/end timestamps.

Input is either a catalog CSV (season, episode, title columns) or work/segments_reviewed.csv
from the review page (episode = SxxEyy, category, cutaways, note, video_from). Only rows with both
`start` and `end` (HH:MM:SS[.ss] or MM:SS) are processed. Episode files are found by
globbing for SxxEyy anywhere under --source. Writes <out>/index.csv describing every clip.

`video_from` (later than start): audio runs from `start`, but the picture holds the frame at
`video_from` until the clock reaches it (the I&S theme starting over a sofa shot). Forces a
re-encode for that row.

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
                          video_from=r.get("video_from", "")))
        if os.path.exists(dst):
            continue
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
               "-ss", r["start"], "-t", f"{secs(r['end']) - secs(r['start']):.3f}", "-i", src]
        hold = secs(r["video_from"]) - secs(r["start"]) if r.get("video_from") else 0
        if hold > 0:
            # drop the first `hold` s of video, then pad the front with clones of the new first frame
            cmd += ["-filter_complex",
                    f"[0:v]trim=start={hold:.3f},setpts=PTS-STARTPTS,"
                    f"tpad=start_duration={hold:.3f}:start_mode=clone[v]",
                    "-map", "[v]", "-map", "0:a?"]
        if a.precise or hold > 0:
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
