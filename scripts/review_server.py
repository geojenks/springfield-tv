#!/usr/bin/env python3
"""Serve the segment review page with real video scrubbing.

  python scripts/review_server.py --source <episodes dir> [--work work] [--port 8765]

then open http://localhost:8765/ (review), http://localhost:8765/tags/ (tag pass over the finished
rows) or http://localhost:8765/player/ (the channel-hopping
player, fed by clips/index.csv). The page (scripts/review.html) loads work/segments.csv,
plays the episode file for each row (range requests, so seeking works), lets you step
frames, set start/end, mark "holds" (a-b ranges where the picture freezes on the frame at b
while the audio continues: the I&S theme starting over the sofa, sofa cutaways mid-cartoon;
a*-b freezes on the frame at a instead), add rows by hand (review.json entries with new=true,
merged into the row list by load_rows),
"cuts" (a-b ranges removed entirely, video and audio, where the show is asynchronous and joins
up without the sofa shot), accept/reject, flag cutaways, and saves edits to work/review.json
and a merged work/segments_reviewed.csv on every change.
"""
import argparse, csv, glob, json, os, re, sys, threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
TAG = re.compile(r"S(\d{2})E(\d{2})", re.I)
SAVE_LOCK = threading.Lock()


def find_videos(source):
    vids = {}
    for p in glob.glob(os.path.join(source, "**", "*"), recursive=True):
        m = TAG.search(os.path.basename(p))
        if m and p.lower().endswith((".mkv", ".mp4", ".avi", ".m4v")):
            vids[f"S{m.group(1)}E{m.group(2)}".upper()] = p
    return vids


def _sec(h):
    p = [float(x) for x in h.split(":")]
    return p[0] * 3600 + p[1] * 60 + p[2] if len(p) == 3 else p[0] * 60 + p[1] if len(p) == 2 else p[0]


def load_rows(work):
    """(rows, edits): work/segments.csv plus rows added by hand on the review page (review.json
    entries with new=true carry their own episode/category/start/end; method 'manual')."""
    rp = os.path.join(work, "review.json")
    edits = json.load(open(rp, encoding="utf-8")) if os.path.exists(rp) else {}
    src = os.path.join(work, "segments.csv")
    rows = list(csv.DictReader(open(src, encoding="utf-8"))) if os.path.exists(src) else []
    fields = list(rows[0].keys()) if rows else ["id", "episode", "category", "anchor", "start", "end", "dur",
                                                 "method", "confidence", "screen_spans", "anchor_text"]
    for id, e in edits.items():
        if e.get("new"):
            r = {k: "" for k in fields}
            r.update(id=id, episode=e["episode"], category=e.get("category", ""), start=e["start"], end=e["end"],
                     method=e.get("method", "manual"), confidence="", anchor_text=e.get("lyrics") or e.get("note", ""))
            rows.append(r)
    rows.sort(key=lambda r: (r["episode"], _sec(r["start"])))
    return rows, edits


def default_tags(category):
    """a row with no tag list yet is tagged with its category tokens (`krusty+advert` -> both)"""
    return sorted({t for t in re.split(r"[+&]", category or "") if t})


def write_reviewed(work):
    """Merge work/review.json into work/segments.csv (+ hand-added rows) -> work/segments_reviewed.csv."""
    rows, edits = load_rows(work)
    if not rows:
        return 0
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
        r["cuts"] = e.get("cuts", "")
        r["holds"] = e.get("holds") or (f"{r['start']}-{e['video_from']}" if e.get("video_from") else "")
        r["tags"] = "+".join(e["tags"] if "tags" in e else default_tags(r["category"]))
        out.append(r)
    fields = list(rows[0].keys()) + ["cutaways", "note", "holds", "cuts", "tags"] if rows else ["id"]
    with open(os.path.join(work, "segments_reviewed.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(out)
    return len(out)


class Handler(SimpleHTTPRequestHandler):
    work = "work"
    clips = "clips"
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
        if path in ("/player", "/player/"):
            return self.send_file(os.path.join(HERE, "..", "player", "index.html"), "text/html; charset=utf-8")
        if path in ("/tags", "/tags/"):
            return self.send_file(os.path.join(HERE, "tags.html"), "text/html; charset=utf-8")
        if path.startswith("/clips/"):
            fp = os.path.normpath(os.path.join(self.clips, path[7:].lstrip("/")))
            if not os.path.abspath(fp).startswith(os.path.abspath(self.clips)) or not os.path.isfile(fp):
                return self.send_error(404)
            return self.send_file(fp, "video/mp4" if fp.endswith(".mp4") else self.guess_type(fp))
        if path.startswith("/video/"):
            tag = path[7:].upper()[:6]
            if tag not in self.vids:
                return self.send_error(404, "no episode file for " + tag)
            return self.send_file(self.vids[tag], "video/mp4")
        self.send_error(404)

    def do_POST(self):
        if self.path == "/render":
            return self.render()
        if self.path == "/tag":
            return self.tag()
        if self.path != "/save":
            return self.send_error(404)
        n = int(self.headers.get("Content-Length", 0))
        data = json.loads(self.rfile.read(n).decode("utf-8"))
        rp = os.path.join(self.work, "review.json")
        with SAVE_LOCK:
            cur = json.load(open(rp, encoding="utf-8")) if os.path.exists(rp) else {}
            cur.update(data)                      # per-row merge so two open tabs don't clobber each other
            with open(rp, "w", encoding="utf-8") as f:
                json.dump(cur, f, indent=1)
        k = write_reviewed(self.work)
        self.reply({"ok": True, "accepted": k})

    def tag(self):
        """Body {id, tags: [...]}: replace that row's tag list only (the tags page never touches
        the other fields, so it can stay open next to the review page)."""
        n = int(self.headers.get("Content-Length", 0))
        d = json.loads(self.rfile.read(n).decode("utf-8"))
        rid = d.get("id", "")
        tags = sorted({re.sub(r"[^\w-]", "_", t.strip().lower()) for t in d.get("tags", []) if t.strip()})
        rp = os.path.join(self.work, "review.json")
        with SAVE_LOCK:
            cur = json.load(open(rp, encoding="utf-8")) if os.path.exists(rp) else {}
            if rid not in cur:
                return self.reply({"ok": False, "error": "unknown row " + rid}, 404)
            cur[rid]["tags"] = tags                       # [] is real: every category tag switched off
            with open(rp, "w", encoding="utf-8") as f:
                json.dump(cur, f, indent=1)
        write_reviewed(self.work)
        self.reply({"ok": True, "tags": tags})

    def render(self):
        """Body {id, start, end, holds, cuts}: encode that row as it is now (the page sends the
        unsaved field values) to work/preview/<id>.mp4 and mark preview=true in review.json."""
        import importlib, extract_clips
        importlib.reload(extract_clips)               # pick up edits to the cutter without restarting the server
        render_clip, secs, fps_of = extract_clips.render_clip, extract_clips.secs, extract_clips.fps_of
        n = int(self.headers.get("Content-Length", 0))
        d = json.loads(self.rfile.read(n).decode("utf-8"))
        rid = re.sub(r"[^\w.-]", "", d.get("id", ""))
        tag = rid[:6].upper()
        if not rid or tag not in self.vids:
            return self.reply({"ok": False, "error": "no episode file for " + tag}, 404)
        pdir = os.path.join(self.work, "preview")
        os.makedirs(pdir, exist_ok=True)
        src = self.vids[tag]
        try:
            render_clip(src, secs(d["start"]), secs(d["end"]), d.get("holds", ""), d.get("cuts", ""),
                        os.path.join(pdir, rid + ".mp4"), fps_of(src))
        except Exception as ex:
            return self.reply({"ok": False, "error": str(ex)[:300]}, 500)
        rp = os.path.join(self.work, "review.json")
        with SAVE_LOCK:
            cur = json.load(open(rp, encoding="utf-8")) if os.path.exists(rp) else {}
            cur.setdefault(rid, {})["preview"] = True
            with open(rp, "w", encoding="utf-8") as f:
                json.dump(cur, f, indent=1)
        self.reply({"ok": True})

    def reply(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
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
    ap.add_argument("--clips", default="clips", help="extracted clips dir, served at /clips/ for /player/")
    ap.add_argument("--port", type=int, default=8765)
    a = ap.parse_args()
    Handler.work = a.work
    Handler.clips = a.clips
    Handler.vids = find_videos(a.source)
    print(f"{len(Handler.vids)} episode files; serving {a.work} at http://localhost:{a.port}/")
    ThreadingHTTPServer(("127.0.0.1", a.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
