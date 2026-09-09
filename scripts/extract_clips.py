#!/usr/bin/env python3
"""Cut clips from your own episode files using the catalog's start/end timestamps.

Only rows with both `start` and `end` filled (HH:MM:SS or MM:SS) are processed.
Episode files are found by globbing for SxxEyy anywhere under --source.

Usage:
  python scripts/extract_clips.py --source ~/Videos/Simpsons --out clips
  python scripts/extract_clips.py --source ... --out clips --precise   # re-encode, frame-accurate
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
    for r in rows:
        if not (r["start"] and r["end"] and r["episode"] and r["season"].isdigit()):
            skipped += 1; continue
        src = find_source(a.source, r["season"], r["episode"])
        if not src:
            print(f"!! no file for S{int(r['season']):02d}E{int(r['episode']):02d} ({r['id']})"); continue
        name = f"S{int(r['season']):02d}E{int(r['episode']):02d}_{r['id']}_{safe(r['title'])}.mp4"
        dst = os.path.join(a.out, name)
        if os.path.exists(dst):
            continue
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
               "-ss", r["start"], "-to", r["end"], "-i", src]
        if a.precise:
            cmd += ["-c:v", "libx264", "-crf", "20", "-preset", "fast", "-c:a", "aac", "-b:a", "128k"]
        else:
            cmd += ["-c", "copy"]  # fast, cuts land on nearest keyframe
        cmd += ["-movflags", "+faststart", dst]
        print(("DRY " if a.dry_run else "") + " ".join(cmd))
        if not a.dry_run:
            subprocess.run(cmd, check=True)
        done += 1
    print(f"\n{done} clips cut, {skipped} rows still need timestamps")

if __name__ == "__main__":
    main()
