"""subsync: lines up a subtitle with the video's audio (ffsubsync) for Kuiper's /subs relay.

POST /sync?v=<video url>&wait=<seconds>   body: the subtitle as SRT text
  200 + synced SRT   when it is ready (cached, or finished within `wait` seconds)
  202                when it is still working (the caller serves the raw subtitle meanwhile)
  204                when the subtitle needs no change, or could not be synced (serve raw)

v8.1: nothing waits for the sync any more (the apps open Infuse at once and start the sync ahead of
time), so the first answer has to be quick on any file: the three first windows are sized by data,
not minutes (a 90 Mbps remux gets 60 s windows, a web release the full 6 minutes); only the quick pass
takes a job slot, so a long line-by-line pass can never hold up the next title; and files over
FULL_MAX_GB skip the whole-file read and use spaced windows instead.

v3: the offset is measured in three 6-minute windows (in parallel), then (in the background) overlapping
6-minute windows every 4 minutes, median-smoothed; cuts are applied as steps, drift as lines spread over the episode (ffmpeg seeks, so only
those parts of the file are read) and applied as one value, a straight-line drift, or point to point
between the windows when the releases differ by a cut. Results are kept in CACHE_DIR.
"""
import hashlib, json, os, re, subprocess, threading, time, urllib.parse
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CACHE = os.environ.get("CACHE_DIR", "/cache")
os.makedirs(CACHE, exist_ok=True)
slots = threading.BoundedSemaphore(int(os.environ.get("MAX_JOBS", "2")))      # quick passes
full_slots = threading.BoundedSemaphore(int(os.environ.get("MAX_FULL_JOBS", "1")))  # background passes
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
MIN_WINDOW = int(os.environ.get("MIN_WINDOW_SECONDS", "60"))
WINDOW_MB = float(os.environ.get("WINDOW_MB", "200"))       # data read per quick window
FULL_MAX_GB = float(os.environ.get("FULL_MAX_GB", "25"))    # bigger files skip the whole-file read


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


def probe(video):
    """(duration s, size bytes, bit rate bits/s); 0 for anything the file does not report."""
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration,size,bit_rate",
                          "-of", "json", video], capture_output=True, text=True, timeout=60).stdout
    fmt = (json.loads(out or "{}").get("format") or {})

    def num(k):
        try:
            return float(fmt.get(k))
        except (TypeError, ValueError):  # missing or "N/A"
            return 0.0
    dur, size, rate = num("duration"), num("size"), num("bit_rate")
    if not rate and dur and size:
        rate = size * 8 / dur
    return dur, size, rate


def window_length(rate):
    """Seconds per window so each reads about WINDOW_MB of the file (video included)."""
    if not rate:
        return WINDOW
    return int(min(WINDOW, max(MIN_WINDOW, WINDOW_MB * 8e6 / rate)))


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
    try:
        res = subprocess.run(["ffsubsync", wav, "-i", src, "-o", out, "--no-fix-framerate", "--max-offset-seconds", "20"],
                             capture_output=True, text=True, timeout=180)
    except Exception:  # noqa: BLE001
        return None
    for p in (src, out):
        if os.path.exists(p):
            os.remove(p)
    m = re.search(r"offset seconds: (-?[\d.]+)", res.stdout + res.stderr)
    return float(m.group(1)) if res.returncode == 0 and m else None


JUMP = 0.6  # seconds: a bigger change between neighbouring measurements is a cut, not drift


def smooth(points):
    """Running median of three over the measurements in time order. A single misread window (little
    dialogue, music, credits) is replaced by its neighbours' value, while a real cut, which shows up
    in every window after it, survives."""
    points = sorted(points)
    if len(points) < 3:
        return points
    out = [points[0]]
    for prev, cur, nxt in zip(points, points[1:], points[2:]):
        out.append((cur[0], sorted((prev[1], cur[1], nxt[1]))[1]))
    # The last point has no right neighbour: keep it only if it agrees with the one before.
    last, before = points[-1], out[-1]
    out.append(last if abs(last[1] - before[1]) <= JUMP else (last[0], before[1]))
    return out


def offset_curve(points, cues=None):
    """Offset as a function of time. Neighbouring measurements that agree are joined by a straight
    line (drift); where they jump, the change is a cut between the releases and is applied as a step
    halfway between them."""
    points = sorted(points)
    offs = [o for _, o in points]
    if max(offs) - min(offs) <= 0.25:
        c = sorted(offs)[len(offs) // 2]
        return (lambda t: c), f"constant {c:+.2f}s"

    # A cut between releases sits at a scene change, which in the subtitle is the longest silence
    # (gap between two lines) in the stretch between the two measurements. Put the step there.
    steps = {}
    for (t0, o0), (t1, o1) in zip(points, points[1:]):
        if abs(o1 - o0) > JUMP:
            at = (t0 + t1) / 2
            if cues:
                gaps = [(cues[i + 1][0] - cues[i][1], (cues[i][1] + cues[i + 1][0]) / 2)
                        for i in range(len(cues) - 1) if t0 <= cues[i][1] and cues[i + 1][0] <= t1]
                if gaps:
                    at = max(gaps)[1]
            steps[(t0, t1)] = at

    def curve(t):
        if t <= points[0][0]:
            return points[0][1]
        for (t0, o0), (t1, o1) in zip(points, points[1:]):
            if t <= t1:
                if abs(o1 - o0) > JUMP:
                    return o0 if t < steps[(t0, t1)] else o1
                return o0 + (o1 - o0) * (t - t0) / (t1 - t0)
        return points[-1][1]
    jumps = sum(1 for (_, a), (_, b) in zip(points, points[1:]) if abs(b - a) > JUMP)
    kind = f"{jumps} cut{'s' if jumps != 1 else ''}" if jumps else "drift"
    where = "".join(f" cut@{v / 60:.1f}m" for v in steps.values())
    return curve, kind + " " + " ".join(f"{t / 60:.0f}m:{o:+.2f}s" for t, o in points) + where


#  --- full-episode aligner (v8) -------------------------------------------------------------
# The whole episode's audio is read once (one sequential ffmpeg pass, mono 16 kHz) and turned into
# a speech/no-speech track in 10 ms frames. Every subtitle line then gets its own offset, chosen by
# dynamic programming: a line scores the speech it overlaps, and changing the offset from one line
# to the next costs a penalty, so the offset only changes where the releases really differ (a cut)
# and slowly drifts where they run at slightly different speeds. This is the method alass uses.
import array

FRAME = 0.01                      # seconds per speech frame
STEP = 5                          # candidate offsets every 5 frames (50 ms)
MAX_SHIFT = 15.0                  # seconds either way
PENALTY = 300                     # frames of overlap a change of offset has to win back
FULL_TIMEOUT = int(os.environ.get("FULL_TIMEOUT", "900"))


def speech_track(video):
    """1 byte per 10 ms frame: 1 where someone is speaking."""
    proc = subprocess.Popen(["ffmpeg", "-nostdin", "-loglevel", "error", "-i", video, "-map", "0:a:0", "-vn",
                             "-ac", "1", "-ar", "16000", "-f", "s16le", "-"], stdout=subprocess.PIPE)
    try:
        import webrtcvad
        vad = webrtcvad.Vad(2)
        detect = lambda frame: vad.is_speech(frame, 16000)  # noqa: E731
    except Exception:  # noqa: BLE001
        vad = None
    frames = bytearray()
    energies = []
    started = time.time()
    while True:
        chunk = proc.stdout.read(320 * 1000)
        if not chunk:
            break
        if time.time() - started > FULL_TIMEOUT:
            proc.kill()
            raise RuntimeError("full audio read timed out")
        for i in range(0, len(chunk) - 319, 320):
            frame = chunk[i:i + 320]
            if vad is not None:
                frames.append(1 if detect(frame) else 0)
        if vad is None:
            import numpy as np
            x = np.frombuffer(chunk[: len(chunk) // 320 * 320], dtype=np.int16).astype(np.float32).reshape(-1, 160)
            energies.extend((x * x).mean(axis=1).tolist())
    proc.wait()
    if vad is None:
        import numpy as np
        e = np.asarray(energies)
        thr = np.percentile(e, 60)
        frames = bytearray((e > thr).astype("uint8").tobytes())
    if len(frames) < 6000:
        raise RuntimeError("audio too short or unreadable")
    return frames


def _matrix(cues, speech):
    import numpy as np
    sp = np.frombuffer(bytes(speech), dtype=np.uint8).astype(np.int32)
    prefix = np.concatenate([[0], np.cumsum(sp)])
    n = len(sp)
    shifts = np.arange(-int(MAX_SHIFT / FRAME), int(MAX_SHIFT / FRAME) + 1, STEP)
    starts = np.array([int(round(a / FRAME)) for a, _, _ in cues])
    ends = np.array([max(int(round(b / FRAME)), int(round(a / FRAME)) + 1) for a, b, _ in cues])
    s = np.clip(starts[:, None] + shifts[None, :], 0, n)
    e = np.clip(ends[:, None] + shifts[None, :], 0, n)
    return shifts, prefix[e] - prefix[s]


def align_dp(cues, speech):
    """Per-line offsets in seconds (to add to each line), or None."""
    import numpy as np
    if len(cues) < 20:
        return None
    shifts, score = _matrix(cues, speech)
    n, d = score.shape
    best = score[0].astype(np.float64)
    back = np.zeros((n, d), dtype=np.int32)
    back[0] = np.arange(d)
    for i in range(1, n):
        j = int(np.argmax(best))
        stay = best
        jump = best[j] - PENALTY
        take_jump = jump > stay
        back[i] = np.where(take_jump, j, np.arange(d))
        best = np.where(take_jump, jump, stay) + score[i]
    k = int(np.argmax(best))
    path = [0] * n
    for i in range(n - 1, -1, -1):
        path[i] = k
        k = int(back[i][k])
    offs = shifts[np.array(path)] * FRAME
    # Remove single-line wobbles: running median over 7 lines.
    pad = np.pad(offs, 3, mode="edge")
    offs = np.array([np.median(pad[i:i + 7]) for i in range(n)])
    return [float(o) for o in offs]


def overlap_score(cues, speech, offsets):
    """Speech frames covered by the lines with the given per-line offsets (higher = better fit)."""
    n = len(speech)
    prefix = [0]
    total = 0
    for b in speech:
        total += b
        prefix.append(total)
    score = 0
    for (a, b, _), o in zip(cues, offsets):
        s = min(max(int(round((a + o) / FRAME)), 0), n)
        e = min(max(int(round((b + o) / FRAME)), 0), n)
        score += prefix[e] - prefix[s]
    return score


def describe(cues, offs):
    """'+1.40s, +2.60s from 13.4m, +4.50s from 27.0m' style summary of per-line offsets."""
    parts, cur = [], None
    for (a, _, _), o in zip(cues, offs):
        if cur is None or abs(o - cur) > 0.3:
            parts.append(f"{o:+.2f}s" + (f" from {a / 60:.1f}m" if cur is not None else ""))
            cur = o
    return ", ".join(parts[:12]) + (" ..." if len(parts) > 12 else "")


def publish_offsets(key, cues, offs, desc, started):
    out, meta = os.path.join(CACHE, f"{key}.srt"), os.path.join(CACHE, f"{key}.json")
    useful = any(abs(o) >= 0.1 for o in offs)
    if useful:
        with open(out + ".part.srt", "w", encoding="utf-8") as f:
            f.write(write_srt([[a + o, b + o, body] for (a, b, body), o in zip(cues, offs)]))
        os.replace(out + ".part.srt", out)
    with open(meta, "w") as f:
        json.dump({"fit": desc, "synced": useful}, f)
    log(f"{key[:10]} final: {desc} {'synced' if useful else 'kept as is'} in {time.time() - started:.0f}s")


def publish(key, cues, points, started, final):
    out, meta = os.path.join(CACHE, f"{key}.srt"), os.path.join(CACHE, f"{key}.json")
    # The quick result is smoothed too: with three windows, one that disagrees with the other two
    # (a loud, dialogue-light stretch) is outvoted instead of being applied as a cut. A real cut is
    # picked up by the background pass, where neighbouring windows confirm it.
    points = smooth(points)
    curve, desc = offset_curve(points, cues)
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
    try:
        with slots:  # the quick pass only: the next title never waits behind a background pass
            prune()
            started = time.time()
            cues = parse_srt(srt)
            vkey = hashlib.sha256(video.encode()).hexdigest()[:24]
            dur, size, rate = probe(video)
            win = window_length(rate)
            # Three windows spread over the episode (away from the very start and the credits), so a
            # drift or a cut between releases is measured where it happens, not guessed from the start.
            starts = [dur * f for f in (0.06, 0.42, 0.78)] if dur >= 30 * 60 else [0.0]
            # All windows at once: each is a separate seek + ffsubsync run.
            with ThreadPoolExecutor(max_workers=4) as pool:
                found = pool.map(lambda a: (a[1] + win / 2, measure(video, vkey, cues, a[1], f"{key[:16]}-{a[0]}", win)),
                                 list(enumerate(starts)))
            points = sorted((t, o) for t, o in found if o is not None and abs(o) <= 20)
            log(f"{key[:10]} {size / 1e9:.1f} GB at {rate / 1e6:.0f} Mbps: {len(starts)} windows of {win}s")
            # The three-window result goes out straight away; the final pass below then replaces it for
            # later fetches.
            if points:
                publish(key, cues, points, started, final=False)
            else:
                with lock:
                    if key in running:
                        running[key].set()
        with full_slots:
            # Final pass (in the background; the first result is already out): the whole episode,
            # line by line. The best of the original, the quick result and the line-by-line result
            # (by how much speech the lines land on) is kept, so this can never make it worse.
            # Files over FULL_MAX_GB (remuxes) are not read end to end; spaced windows instead.
            if not size or size <= FULL_MAX_GB * 1e9:
                try:
                    speech = speech_track(video)
                    cands = {"original": [0.0] * len(cues)}
                    if points:
                        quick, _ = offset_curve(sorted(points), cues)
                        cands["quick"] = [quick(a) for a, _, _ in cues]
                    full = align_dp(cues, speech)
                    if full:
                        cands["line-by-line"] = full
                    scores = {k: overlap_score(cues, speech, v) for k, v in cands.items()}
                    pick = max(scores, key=scores.get)
                    ranking = " ".join(f"{k}={v}" for k, v in sorted(scores.items(), key=lambda kv: -kv[1]))
                    publish_offsets(key, cues, cands[pick], f"{pick}: {describe(cues, cands[pick])} [{ranking}]", started)
                    return
                except Exception as e:  # noqa: BLE001
                    log(f"{key[:10]} line-by-line pass failed ({str(e)[:120]}); measuring windows instead")
            # Fallback: overlapping windows over the whole episode (every 4 minutes, or spaced out to at
            # most ~20 on big files), so every cut and drift is measured where it happens and one bad
            # reading can be outvoted by its neighbours.
            spacing = max(240, int(dur / 20)) if size > FULL_MAX_GB * 1e9 else 240
            extra = [x for x in range(60, max(int(dur) - win - 60, 61), spacing)
                     if all(abs(x + win / 2 - t) > 90 for t, _ in points)] if dur >= 30 * 60 else []
            if extra:
                with ThreadPoolExecutor(max_workers=4) as pool:
                    found = pool.map(lambda a: (a[1] + win / 2, measure(video, vkey, cues, a[1], f"{key[:16]}-x{a[0]}", win)),
                                     list(enumerate(extra)))
                points = sorted(points + [(t, o) for t, o in found if o is not None and abs(o) <= 20])
            if points:
                publish(key, cues, points, started, final=True)
            else:
                # Not cached: the next request for this subtitle tries again.
                log(f"{key[:10]} final: no window could be read")
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
        key = hashlib.sha256(("v8\n" + video + "\n" + srt).encode()).hexdigest()
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


log(f"subsync v8.1 listening on :8080, cache {CACHE}, 3 windows of {MIN_WINDOW}-{WINDOW}s (~{WINDOW_MB:.0f} MB each), "
    f"whole-file pass up to {FULL_MAX_GB:.0f} GB")
ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
