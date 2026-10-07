"""subsync: lines up a subtitle with the video's audio (ffsubsync) for Kuiper's /subs relay.

POST /sync?v=<video url>&wait=<seconds>   body: the subtitle as SRT text
  200 + synced SRT   when it is ready (cached, or finished within `wait` seconds)
  202                when it is still working (the caller serves the raw subtitle meanwhile)
  204                when the subtitle needs no change, or could not be synced (serve raw)

Each video's first AUDIO_MINUTES minutes of audio are pulled once with ffmpeg (only that part of
the file is read) and reused for every subtitle of that video. Results are kept in CACHE_DIR.
"""
import hashlib, json, os, re, subprocess, threading, time, urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CACHE = os.environ.get("CACHE_DIR", "/cache")
MINUTES = int(os.environ.get("AUDIO_MINUTES", "20"))
os.makedirs(CACHE, exist_ok=True)
slots = threading.BoundedSemaphore(int(os.environ.get("MAX_JOBS", "2")))
lock = threading.Lock()
running = {}  # key -> threading.Event


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def prune():
    # Audio is only needed while a video's subtitles are being synced; results stay 60 days.
    now = time.time()
    for name in os.listdir(CACHE):
        path = os.path.join(CACHE, name)
        age = now - os.path.getmtime(path)
        if (name.endswith(".wav") and age > 6 * 3600) or age > 60 * 86400:
            try:
                os.remove(path)
            except OSError:
                pass


def audio_for(video):
    vkey = hashlib.sha256(video.encode()).hexdigest()[:24]
    wav = os.path.join(CACHE, f"{vkey}.wav")
    if not os.path.exists(wav):
        tmp = wav + ".part.wav"
        subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-i", video, "-map", "0:a:0", "-vn",
                        "-ac", "1", "-ar", "16000", "-t", str(MINUTES * 60), tmp], check=True, timeout=300)
        os.replace(tmp, wav)
    return wav


def sync(key, video, srt):
    out, meta = os.path.join(CACHE, f"{key}.srt"), os.path.join(CACHE, f"{key}.json")
    try:
        with slots:
            prune()
            started = time.time()
            wav = audio_for(video)
            src = os.path.join(CACHE, f"{key}.in.srt")
            with open(src, "w", encoding="utf-8") as f:
                f.write(srt)
            res = subprocess.run(["ffsubsync", wav, "-i", src, "-o", out + ".part.srt"],
                                 capture_output=True, text=True, timeout=300)
            text = res.stdout + res.stderr
            off = re.search(r"offset seconds: (-?[\d.]+)", text)
            fps = re.search(r"framerate scale factor: ([\d.]+)", text)
            offset = float(off.group(1)) if off else None
            scale = float(fps.group(1)) if fps else None
            # Keep the synced copy only for a plausible, real correction.
            useful = (res.returncode == 0 and offset is not None and abs(offset) <= 60
                      and (abs(offset) >= 0.15 or (scale and abs(scale - 1) > 0.0001)))
            if useful:
                os.replace(out + ".part.srt", out)
            for p in (src, out + ".part.srt"):
                if os.path.exists(p):
                    os.remove(p)
            with open(meta, "w") as f:
                json.dump({"offset": offset, "scale": scale, "synced": bool(useful)}, f)
            log(f"{key[:10]} offset={offset} scale={scale} {'synced' if useful else 'kept as is'} "
                f"in {time.time() - started:.0f}s")
    except Exception as e:  # noqa: BLE001
        # Not cached: the next request for this subtitle tries again (the caller serves raw meanwhile).
        log(f"{key[:10]} failed: {str(e)[:200]}")
    finally:
        with lock:
            running.pop(key).set()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_POST(self):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        video = (q.get("v") or [""])[0]
        wait = min(float((q.get("wait") or ["0"])[0] or 0), 60)
        srt = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode("utf-8", "replace")
        if not video.startswith("http") or "-->" not in srt:
            return self.reply(400)
        key = hashlib.sha256((video + "\n" + srt).encode()).hexdigest()
        out, meta = os.path.join(CACHE, f"{key}.srt"), os.path.join(CACHE, f"{key}.json")
        with lock:
            done = os.path.exists(meta)
            event = running.get(key)
            if not done and event is None:
                event = running[key] = threading.Event()
                threading.Thread(target=sync, args=(key, video, srt), daemon=True).start()
        if not done and event is not None and wait > 0:
            event.wait(wait)
        if os.path.exists(out):
            with open(out, "rb") as f:
                return self.reply(200, f.read())
        return self.reply(204 if os.path.exists(meta) else 202)

    def reply(self, code, body=b""):
        self.send_response(code)
        self.send_header("Content-Type", "application/x-subrip; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


log(f"subsync listening on :8080, cache {CACHE}, {MINUTES} min of audio per video")
ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
