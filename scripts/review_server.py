#!/usr/bin/env python3
"""Serve the segment review page with real video scrubbing.

  python scripts/review_server.py --source <episodes dir> [--work work] [--port 8765]

then open http://localhost:8765/ . The page (scripts/review.html) loads work/segments.csv,
plays the episode file for each row (range requests, so seeking works), lets you step
frames, set start/end (and an optional later "video from" point: audio starts at start, the
picture freezes on the video_from frame until the clock catches up), accept/reject, flag
cutaways, and saves edits to work/review.json
and a merged work/segments_reviewed.csv on every change.
"""
import argparse, csv, glob, json, os, re, sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
TAG = re.compile(r"S(\d{2})E(\d{2})", re.I)


def find_videos(source):
    vids = {}
    for p in glob.glob(os.path.join(source, "**", "*"), recursive=True):
        m = TAG.search(os.path.basename(p))
        if m and p.lower().endswith((".mkv", ".mp4", ".avi", ".m4v")):
            vids[f"S{m.group(1)}E{m.group(2)}".upper()] = p
    return vids


def write_reviewed(work):
    """Merge work/review.json into work/segments.csv -> work/segments_reviewed.csv."""
    rp = os.path.join(work, "review.json")
    edits = json.load(open(rp, encoding="utf-8")) if os.path.exists(rp) else {}
    src = os.path.join(work, "segments.csv")
    if not os.path.exists(src):
        return 0
    rows = list(csv.DictReader(open(src, encoding="utf-8")))
    out = []
    for r in rows:
        e = edits.get(r["id"], {})
        if e.get("status") != "accept":
            continue
        r = dict(r)
        r["start"] = e.get("start") or r["start"]
        r["end"] = e.get("end") or r["end"]
        r["category"] = e.get("category") or r["category"]
        r["cutaways"] = "1" if e.get("cutaways") else "0"
        r["note"] = e.get("note", "")
        r["video_from"] = e.get("video_from", "")
        out.append(r)
    fields = list(rows[0].keys()) + ["cutaways", "note", "video_from"] if rows else ["id"]
    with open(os.path.join(work, "segments_reviewed.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(out)
    return len(out)


class Handler(SimpleHTTPRequestHandler):
    work = "work"
    vids = {}

    def log_message(self, *a):
        pass

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/":
            return self.send_file(os.path.join(HERE, "review.html"), "text/html; charset=utf-8")
        if path.startswith("/work/"):
            fp = os.path.normpath(os.path.join(self.work, path[6:].lstrip("/")))
            if not os.path.abspath(fp).startswith(os.path.abspath(self.work)):
                return self.send_error(403)
            if not os.path.exists(fp):
                return self.send_error(404)
            return self.send_file(fp, self.guess_type(fp))
        if path.startswith("/video/"):
            tag = path[7:].upper()[:6]
            if tag not in self.vids:
                return self.send_error(404, "no episode file for " + tag)
            return self.send_file(self.vids[tag], "video/mp4")
        self.send_error(404)

    def do_POST(self):
        if self.path != "/save":
            return self.send_error(404)
        n = int(self.headers.get("Content-Length", 0))
        data = json.loads(self.rfile.read(n).decode("utf-8"))
        with open(os.path.join(self.work, "review.json"), "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1)
        k = write_reviewed(self.work)
        body = json.dumps({"ok": True, "accepted": k}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, fp, ctype):
        """Static file with HTTP Range support (needed for video seeking)."""
        size = os.path.getsize(fp)
        rng = self.headers.get("Range")
        start, end = 0, size - 1
        status = 200
        if rng and rng.startswith("bytes="):
            a, _, b = rng[6:].partition("-")
            start = int(a) if a else max(size - int(b), 0)
            end = int(b) if b and a else size - 1
            end = min(end, size - 1)
            status = 206
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Cache-Control", "no-cache")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        with open(fp, "rb") as f:
            f.seek(start)
            left = end - start + 1
            while left > 0:
                chunk = f.read(min(1 << 20, left))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
                    return
                left -= len(chunk)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True)
    ap.add_argument("--work", default="work")
    ap.add_argument("--port", type=int, default=8765)
    a = ap.parse_args()
    Handler.work = a.work
    Handler.vids = find_videos(a.source)
    print(f"{len(Handler.vids)} episode files; serving {a.work} at http://localhost:{a.port}/")
    ThreadingHTTPServer(("127.0.0.1", a.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
