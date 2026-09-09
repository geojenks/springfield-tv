#!/usr/bin/env python3
"""Download per-episode subtitle tracks from Frinkiac into work/subs/SxxEyy.json.

Frinkiac's /api/episode/<key>/<start_ms>/<end_ms> returns every subtitle line in the
range with StartTimestamp/EndTimestamp in ms. Its frames are 480x360 and its clock
matched a local 360p rip of S05E02 to within ~1.5 s, so the timings are usable directly.

Usage: python scripts/fetch_frinkiac_subs.py --seasons 1-9 [--out work/subs]
Also writes work/subs/SxxEyy.srt so the existing scan_episodes.py srt cue can use them.
"""
import argparse, json, os, sys, time, urllib.request, urllib.error

API = "https://frinkiac.com/api/episode/{key}/0/3000000"
EP_COUNTS = {1:13,2:22,3:24,4:22,5:22,6:25,7:25,8:25,9:25,10:23,11:22,12:21,13:22,14:22,
             15:22,16:21,17:22,18:22,19:20,20:21,21:23,22:22,23:22,24:22,25:22,26:22,27:22,
             28:22,29:21,30:23,31:22,32:22,33:22,34:22,35:18,36:22}

def ms(t):
    h, r = divmod(t, 3600000); m, r = divmod(r, 60000); s, msr = divmod(r, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{msr:03d}"

def fetch(key):
    req = urllib.request.Request(API.format(key=key), headers={"User-Agent": "springfield-tv/0.1"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seasons", default="1-9")
    ap.add_argument("--out", default="work/subs")
    ap.add_argument("--delay", type=float, default=0.4)
    a = ap.parse_args()
    lo, hi = map(int, a.seasons.split("-"))
    os.makedirs(a.out, exist_ok=True)
    for s in range(lo, hi + 1):
        for e in range(1, EP_COUNTS.get(s, 25) + 1):
            key = f"S{s:02d}E{e:02d}"
            jp = os.path.join(a.out, key + ".json")
            if os.path.exists(jp): continue
            try:
                d = fetch(key)
            except (urllib.error.HTTPError, urllib.error.URLError) as ex:
                print(key, "failed:", ex); time.sleep(a.delay); continue
            subs = d.get("Subtitles") or []
            json.dump(d, open(jp, "w", encoding="utf-8"), ensure_ascii=False)
            with open(os.path.join(a.out, key + ".srt"), "w", encoding="utf-8") as f:
                for i, sub in enumerate(subs, 1):
                    f.write(f"{i}\n{ms(sub['StartTimestamp'])} --> {ms(sub['EndTimestamp'])}\n{sub['Content']}\n\n")
            print(key, len(subs), "lines", flush=True)
            time.sleep(a.delay)

if __name__ == "__main__":
    main()
