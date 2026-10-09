"""subsync: lines up a subtitle with the video's audio (ffsubsync) for Kuiper's /subs relay.

POST /sync?v=<video url>&wait=<seconds>   body: the subtitle as SRT text
  200 + synced SRT   when it is ready (cached, or finished within `wait` seconds)
  202                when it is still working (the caller serves the raw subtitle meanwhile)
  204                when the subtitle needs no change, or could not be synced (serve raw)

v3: the offset is measured in three 6-minute windows (in parallel), plus four 4-minute windows
between any two that disagree, to place a cut spread over the episode (ffmpeg seeks, so only
those parts of the file are read) and applied as one value, a straight-line drift, or point to point
between the windows when the releases differ by a cut. Results are kept in CACHE_DIR.
"""
import hashlib, json, os, re, subprocess, threading, time, urllib.parse
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CACHE = os.environ.get("CACHE_DIR", "/cache")
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


SRT_TIME = re.compile(r"(\d+):(\d{2}):(\d{2})[,.](\d{3})")
WINDOW = int(os.environ.get("WINDOW_SECONDS", "360"))


def parse_srt(text):
    cues = []
    for block in re.split(r"\r?\n\s*\r?\n", text.strip()):
        lines = block.strip().splitlines()
        at = next((i for i, l in enumerate(lines) if "-->" in l), None)
        if at is None:
            continue
        times = SRT_TIME.findall(lines[at])
        if len(times) < 2:
            continue
        a, b = [int(h) * 3600 + int(m) * 60 + int(sec) + int(ms) / 1000 for h, m, sec, ms in times[:2]]
        body = "\n".join(lines[at + 1:]).strip()
        if body:
            cues.append([a, b, body])
    return cues


def fmt(t):
    t = max(t, 0)
    ms = int(round(t * 1000))
    return f"{ms // 3600000:02}:{ms // 60000 % 60:02}:{ms // 1000 % 60:02},{ms % 1000:03}"


def write_srt(cues):
    return "\n\n".join(f"{i}\n{fmt(a)} --> {fmt(b)}\n{body}" for i, (a, b, body) in enumerate(cues, 1)) + "\n"


def duration_of(video):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", video],
                         capture_output=True, text=True, timeout=60).stdout.strip()
    return float(out) if out else 0.0


def window_audio(video, start, length, path):
    if not os.path.exists(path):
        subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-ss", str(start), "-i", video,
                        "-map", "0:a:0", "-vn", "-ac", "1", "-ar", "16000", "-t", str(length), path + ".part.wav"],
                       check=True, timeout=180)
        os.replace(path + ".part.wav", path)


def measure(video, vkey, cues, start, tag, length=None):
    """Offset (seconds to add to the subtitle) around [start, start+length), or None."""
    length = length or WINDOW
    wav = os.path.join(CACHE, f"{vkey}-{int(start)}-{length}.wav")
    try:
        window_audio(video, start, length, wav)
    except Exception:  # noqa: BLE001
        return None
    inside = [[a - start, b - start, body] for a, b, body in cues if start - 30 <= a < start + length + 30]
    if len(inside) < 8:
        return None
    src, out = os.path.join(CACHE, f"{tag}.in.srt"), os.path.join(CACHE, f"{tag}.out.srt")
    with open(src, "w", encoding="utf-8") as f:
        f.write(write_srt(inside))
    res = subprocess.run(["ffsubsync", wav, "-i", src, "-o", out, "--no-fix-framerate", "--max-offset-seconds", "20"],
                         capture_output=True, text=True, timeout=180)
    for p in (src, out):
        if os.path.exists(p):
            os.remove(p)
    m = re.search(r"offset seconds: (-?[\d.]+)", res.stdout + res.stderr)
    return float(m.group(1)) if res.returncode == 0 and m else None


JUMP = 0.6  # seconds: a bigger change between neighbouring measurements is a cut, not drift


def drop_spikes(points):
    """A measurement far off both its neighbours in the same direction is a misread window (little
    dialogue, music), not a cut that undoes itself a few minutes later: leave it out."""
    points = sorted(points)
    keep = [points[0]] if points else []
    for prev, cur, nxt in zip(points, points[1:], points[2:]):
        a, b = cur[1] - prev[1], cur[1] - nxt[1]
        if not (abs(a) > JUMP and abs(b) > JUMP and (a > 0) == (b > 0)):
            keep.append(cur)
    if len(points) > 1:
        keep.append(points[-1])
    return keep


def offset_curve(points):
    """Offset as a function of time. Neighbouring measurements that agree are joined by a straight
    line (drift); where they jump, the change is a cut between the releases and is applied as a step
    halfway between them."""
    points = sorted(points)
    offs = [o for _, o in points]
    if max(offs) - min(offs) <= 0.25:
        c = sorted(offs)[len(offs) // 2]
        return (lambda t: c), f"constant {c:+.2f}s"

    def curve(t):
        if t <= points[0][0]:
            return points[0][1]
        for (t0, o0), (t1, o1) in zip(points, points[1:]):
            if t <= t1:
                if abs(o1 - o0) > JUMP:
                    return o0 if t < (t0 + t1) / 2 else o1
                return o0 + (o1 - o0) * (t - t0) / (t1 - t0)
        return points[-1][1]
    jumps = sum(1 for (_, a), (_, b) in zip(points, points[1:]) if abs(b - a) > JUMP)
    kind = f"{jumps} cut{'s' if jumps != 1 else ''}" if jumps else "drift"
    return curve, kind + " " + " ".join(f"{t / 60:.0f}m:{o:+.2f}s" for t, o in points)


def publish(key, cues, points, started, final):
    out, meta = os.path.join(CACHE, f"{key}.srt"), os.path.join(CACHE, f"{key}.json")
    points = drop_spikes(points)
    curve, desc = offset_curve(points)
    useful = bool(cues) and max(abs(curve(a)) for a, _, _ in cues) >= 0.1
    if useful:
        fixed = [[a + curve(a), b + curve(a), body] for a, b, body in cues]
        with open(out + ".part.srt", "w", encoding="utf-8") as f:
            f.write(write_srt(fixed))
        os.replace(out + ".part.srt", out)
    if final:
        with open(meta, "w") as f:
            json.dump({"points": points, "fit": desc, "synced": useful}, f)
    log(f"{key[:10]} {'final' if final else 'first'}: {desc} ({len(points)} windows) "
        f"{'synced' if useful else 'kept as is'} in {time.time() - started:.0f}s")
    with lock:
        if key in running:
            running[key].set()  # wakes requests waiting for the first result


def sync(key, video, srt):
    out, meta = os.path.join(CACHE, f"{key}.srt"), os.path.join(CACHE, f"{key}.json")
    try:
        with slots:
            prune()
            started = time.time()
            cues = parse_srt(srt)
            vkey = hashlib.sha256(video.encode()).hexdigest()[:24]
            dur = duration_of(video)
            # Three windows spread over the episode (away from the very start and the credits), so a
            # drift or a cut between releases is measured where it happens, not guessed from the start.
            starts = [dur * f for f in (0.06, 0.42, 0.78)] if dur >= 30 * 60 else [0.0]
            # All windows at once: each is a separate seek + ffsubsync run.
            with ThreadPoolExecutor(max_workers=4) as pool:
                found = pool.map(lambda a: (a[1] + WINDOW / 2, measure(video, vkey, cues, a[1], f"{key[:16]}-{a[0]}")),
                                 list(enumerate(starts)))
            points = sorted((t, o) for t, o in found if o is not None and abs(o) <= 20)
            if not points:
                raise RuntimeError("no window could be measured")
            # The three-window result goes out straight away (the iPhone waits ~35 s for it); a finer
            # pass below may then replace it for later fetches.
            publish(key, cues, points, started, final=False)
            # Where two measurements jump, sample the stretch between them more finely to place the cut.
            extra = []
            for (t0, o0), (t1, o1) in zip(points, points[1:]):
                if abs(o1 - o0) > JUMP and t1 - t0 > 8 * 60:
                    extra += [t0 + (t1 - t0) * k / 5 - 120 for k in range(1, 5)]
            if extra:
                with ThreadPoolExecutor(max_workers=4) as pool:
                    found = pool.map(lambda a: (a[1] + 120, measure(video, vkey, cues, a[1], f"{key[:16]}-x{a[0]}", 240)),
                                     list(enumerate(extra)))
                points = sorted(points + [(t, o) for t, o in found if o is not None and abs(o) <= 20])
            publish(key, cues, points, started, final=True)
    except Exception as e:  # noqa: BLE001
        # Not cached: the next request for this subtitle tries again (the caller serves raw meanwhile).
        log(f"{key[:10]} failed: {str(e)[:200]}")
    finally:
        with lock:
            running.pop(key, threading.Event()).set()


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
        key = hashlib.sha256(("v4\n" + video + "\n" + srt).encode()).hexdigest()
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


log(f"subsync v4 listening on :8080, cache {CACHE}, 3 windows of {WINDOW // 60} min per video")
ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
