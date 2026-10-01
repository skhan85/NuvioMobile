"""Infuse -> Nuvio Sync bridge.

Infuse talks to Trakt at api.trakt.tv / apiz.trakt.tv. Control D points those names at this
server, which holds a certificate the user's devices trust. Every request is passed through
unchanged to the real Trakt (so Infuse's Trakt keeps working), and scrobble start/stop reports
are also turned into an exact playback position and saved to the user's Nuvio Sync profile.
"""

import datetime
import http.client
import json
import os
import queue
import re
import socket
import ssl
import sys
import threading
import time
import unicodedata
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DATA_DIR = os.environ.get("DATA_DIR", "/data")
CERT_FILE = os.environ.get("CERT_FILE", "/certs/server.crt")
KEY_FILE = os.environ.get("KEY_FILE", "/certs/server.key")
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "443"))

NUVIO_URL = os.environ.get("NUVIO_URL", "https://api.nuvio.tv").rstrip("/")
NUVIO_ANON_KEY = os.environ.get(
    "NUVIO_ANON_KEY",
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJyb2xlIjoiYW5vbiIsImlzcyI6InN1cGFiYXNlIiwiaWF0IjoxNzgxNTIxMzQ2LCJleHAiOjE5MzkyMDEzNDZ9.tmQaj682pwzehpqlgCDMnySOqiUvpgRbrE43T4VJpDI",
)
NUVIO_EMAIL = os.environ.get("NUVIO_EMAIL", "")
NUVIO_PASSWORD = os.environ.get("NUVIO_PASSWORD", "")
NUVIO_PROFILE_NAME = os.environ.get("NUVIO_PROFILE_NAME", "")
# Log every request the bridge receives (set LOG_REQUESTS=0 to turn off).
LOG_REQUESTS = os.environ.get("LOG_REQUESTS", "1") != "0"
TMDB_API_KEY = os.environ.get("TMDB_API_KEY", "")
WATCHED_AT_PERCENT = float(os.environ.get("WATCHED_AT_PERCENT", "90"))

TRAKT_HOSTS = {"api.trakt.tv", "apiz.trakt.tv"}
HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te",
    "trailers", "transfer-encoding", "upgrade", "content-length", "host",
}


def log(msg):
    print(f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S} {msg}", flush=True)


def now_ms():
    return int(time.time() * 1000)


# ---------------------------------------------------------------------------
# Real Trakt address, looked up over DNS-over-HTTPS so our own Control D redirect
# (which points these names at us) is never used for the upstream connection.
# ---------------------------------------------------------------------------
_dns_cache = {}
_dns_lock = threading.Lock()


def resolve_real(host):
    with _dns_lock:
        hit = _dns_cache.get(host)
        if hit and hit[1] > time.time():
            return hit[0]
    ctx = ssl.create_default_context()
    conn = http.client.HTTPSConnection("1.1.1.1", timeout=10, context=ctx)
    conn.request("GET", f"/dns-query?name={host}&type=A", headers={"accept": "application/dns-json"})
    data = json.loads(conn.getresponse().read())
    conn.close()
    ips = [a["data"] for a in data.get("Answer", []) if a.get("type") == 1]
    if not ips:
        raise OSError(f"no A record for {host}")
    with _dns_lock:
        _dns_cache[host] = (ips[0], time.time() + 300)
    return ips[0]


def forward(host, method, path, headers, body):
    """Send the request to the real Trakt and return (status, reason, headers, body)."""
    ip = resolve_real(host)
    raw = socket.create_connection((ip, 443), timeout=20)
    tls = ssl.create_default_context().wrap_socket(raw, server_hostname=host)
    conn = http.client.HTTPSConnection(host, timeout=20)
    conn.sock = tls
    out = {k: v for k, v in headers.items() if k.lower() not in HOP_BY_HOP}
    out["Host"] = host
    if body:
        out["Content-Length"] = str(len(body))
    conn.request(method, path, body=body or None, headers=out)
    resp = conn.getresponse()
    data = resp.read()
    result = (resp.status, resp.reason, resp.getheaders(), data)
    conn.close()
    return result


# ---------------------------------------------------------------------------
# Small JSON helpers for TMDB and Nuvio.
# ---------------------------------------------------------------------------
def http_json(url, method="GET", headers=None, payload=None, timeout=20):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    return json.loads(raw) if raw else None


def tmdb(path, **params):
    params["api_key"] = TMDB_API_KEY
    return http_json(f"https://api.themoviedb.org/3{path}?{urllib.parse.urlencode(params)}")


def norm(text):
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", text.lower())


# ---------------------------------------------------------------------------
# Nuvio Sync (Supabase) session.
# ---------------------------------------------------------------------------
class Nuvio:
    def __init__(self):
        self.lock = threading.Lock()
        self.state_file = os.path.join(DATA_DIR, "nuvio_session.json")
        self.state = {}
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file) as f:
                    self.state = json.load(f)
            except Exception:
                self.state = {}
        self.state.setdefault("client_id", str(uuid.uuid4()))
        self.profile_index = None
        self._profiles = None
        self._profiles_at = 0.0

    def _save(self):
        tmp = self.state_file + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.state, f)
        os.replace(tmp, self.state_file)

    def _auth(self, grant, payload):
        data = http_json(
            f"{NUVIO_URL}/auth/v1/token?grant_type={grant}",
            method="POST",
            headers={"apikey": NUVIO_ANON_KEY},
            payload=payload,
        )
        self.state["access_token"] = data["access_token"]
        self.state["refresh_token"] = data["refresh_token"]
        self.state["expires_at"] = time.time() + int(data.get("expires_in", 3600)) - 120
        self._save()

    def token(self):
        with self.lock:
            if self.state.get("access_token") and self.state.get("expires_at", 0) > time.time():
                return self.state["access_token"]
            if self.state.get("refresh_token"):
                try:
                    self._auth("refresh_token", {"refresh_token": self.state["refresh_token"]})
                    return self.state["access_token"]
                except Exception as e:
                    log(f"Nuvio token refresh failed ({e}); signing in again")
            self._auth("password", {"email": NUVIO_EMAIL, "password": NUVIO_PASSWORD})
            log("Signed in to Nuvio")
            return self.state["access_token"]

    def rpc(self, name, params=None):
        return http_json(
            f"{NUVIO_URL}/rest/v1/rpc/{name}",
            method="POST",
            headers={"apikey": NUVIO_ANON_KEY, "Authorization": f"Bearer {self.token()}"},
            payload=params or {},
        )

    def profiles(self):
        """All Nuvio profiles, refreshed every 10 minutes."""
        if self._profiles is None or self._profiles_at < time.time() - 600:
            self._profiles = self.rpc("sync_pull_profiles") or []
            self._profiles_at = time.time()
        return self._profiles

    def profile(self):
        """The default profile: NUVIO_PROFILE_NAME, else the first one."""
        if self.profile_index is None:
            profiles = self.profiles()
            names = ", ".join(f"{p.get('name')} (#{p.get('profile_index')})" for p in profiles)
            log(f"Nuvio profiles: {names}")
            wanted = norm(NUVIO_PROFILE_NAME)
            match = [p for p in profiles if norm(p.get("name")) == wanted] if wanted else []
            chosen = match[0] if match else (profiles[0] if profiles else {"profile_index": 1, "name": "?"})
            if wanted and not match:
                log(f"WARNING: no profile named '{NUVIO_PROFILE_NAME}', using '{chosen.get('name')}'")
            self.profile_index = int(chosen.get("profile_index", 1))
            log(f"Default Nuvio profile '{chosen.get('name')}' (#{self.profile_index}); a play goes to "
                f"another profile when that profile started this show in Nuvio")
        return self.profile_index

    def profile_name(self, index):
        for p in self.profiles():
            if int(p.get("profile_index", 0)) == index:
                return p.get("name") or f"#{index}"
        return f"#{index}"

    def progress_entries(self, profile):
        return self.rpc("sync_pull_watch_progress", {"p_profile_id": profile}) or []

    def push_progress(self, profile, entry):
        self.rpc("sync_push_watch_progress", {
            "p_profile_id": profile,
            "p_entries": [entry],
            "p_origin_client_id": self.state["client_id"],
        })

    def push_watched(self, profile, item):
        self.rpc("sync_push_watched_items", {
            "p_profile_id": profile,
            "p_items": [item],
            "p_origin_client_id": self.state["client_id"],
        })

NUVIO = Nuvio()


# ---------------------------------------------------------------------------
# Turning an Infuse scrobble into "which title, which episode, how long".
# ---------------------------------------------------------------------------
_show_cache = {}


def tv_details(tv_id):
    if tv_id not in _show_cache:
        d = tmdb(f"/tv/{tv_id}", append_to_response="external_ids")
        _show_cache[tv_id] = {"detail": d, "seasons": {}}
    return _show_cache[tv_id]


def tv_season(tv_id, season):
    show = tv_details(tv_id)
    if season not in show["seasons"]:
        try:
            show["seasons"][season] = tmdb(f"/tv/{tv_id}/season/{season}")
        except Exception:
            show["seasons"][season] = {"episodes": []}
    return show["seasons"][season]


def tv_id_from_ids(ids):
    if ids.get("tmdb"):
        return int(ids["tmdb"])
    for key, source in (("tvdb", "tvdb_id"), ("imdb", "imdb_id")):
        if ids.get(key):
            found = tmdb(f"/find/{ids[key]}", external_source=source).get("tv_results", [])
            if found:
                return found[0]["id"]
    return None


def find_episode_by_title(tv_id, title):
    want = norm(title)
    detail = tv_details(tv_id)["detail"]
    seasons = sorted((s["season_number"] for s in detail.get("seasons", [])), key=lambda n: (n == 0, -n))
    for season in seasons:
        for ep in tv_season(tv_id, season).get("episodes", []):
            if norm(ep.get("name")) == want:
                return season, ep["episode_number"], ep.get("runtime")
    return None


def identify(body):
    """Returns a dict describing the title, or None when it cannot be identified."""
    ep = body.get("episode")
    show = body.get("show")
    movie = body.get("movie")

    if ep and show:
        tv_id = tv_id_from_ids(show.get("ids", {}))
        if not tv_id:
            return None
        season, number = int(ep["season"]), int(ep["number"])
        runtime = None
        for e in tv_season(tv_id, season).get("episodes", []):
            if e.get("episode_number") == number:
                runtime = e.get("runtime")
        return _series(tv_id, season, number, runtime)

    if movie:
        ids = movie.get("ids", {})
        title = movie.get("title", "")
        if ids.get("tmdb"):
            tid = int(ids["tmdb"])
            try:
                m = tmdb(f"/movie/{tid}", append_to_response="external_ids")
                if norm(m.get("title")) == norm(title) or norm(m.get("original_title")) == norm(title):
                    return _movie(m)
            except Exception:
                pass
            # Infuse labels episodes started from Nuvio as a "movie" carrying the SHOW's TMDB id
            # and the episode's title: look the title up among that show's episodes.
            try:
                hit = find_episode_by_title(tid, title)
            except Exception:
                hit = None
            if hit:
                season, number, runtime = hit
                return _series(tid, season, number, runtime)
        if ids.get("imdb"):
            found = tmdb(f"/find/{ids['imdb']}", external_source="imdb_id").get("movie_results", [])
            if found:
                return _movie(tmdb(f"/movie/{found[0]['id']}", append_to_response="external_ids"))
    return None


def _series(tv_id, season, number, runtime):
    detail = tv_details(tv_id)["detail"]
    if not runtime:
        runs = detail.get("episode_run_time") or []
        runtime = runs[0] if runs else None
    return {
        "kind": "series",
        "tmdb": tv_id,
        "imdb": (detail.get("external_ids") or {}).get("imdb_id"),
        "title": detail.get("name", ""),
        "season": season,
        "episode": number,
        "runtime_ms": int(runtime) * 60_000 if runtime else None,
    }


def _movie(m):
    return {
        "kind": "movie",
        "tmdb": m["id"],
        "imdb": m.get("imdb_id") or (m.get("external_ids") or {}).get("imdb_id"),
        "title": m.get("title", ""),
        "season": None,
        "episode": None,
        "runtime_ms": int(m["runtime"]) * 60_000 if m.get("runtime") else None,
    }


def build_entry(info, progress, entries):
    """Matches the title to the entry Nuvio already uses where possible."""
    candidates = [c for c in (info["imdb"], f"tmdb:{info['tmdb']}") if c]
    season, episode = info["season"], info["episode"]
    same_title = [e for e in entries if e.get("content_id") in candidates]
    exact = [e for e in same_title if e.get("season") == season and e.get("episode") == episode]

    content_id = (exact or same_title or [{"content_id": candidates[0]}])[0]["content_id"]
    content_type = (exact or same_title or [{}])[0].get("content_type") or info["kind"]
    duration = (exact[0].get("duration") if exact else 0) or info["runtime_ms"] or 0
    if not duration:
        return None
    if season is not None:
        video_id = f"{content_id}:{season}:{episode}"
        key = f"{content_id}_s{season}e{episode}"
    else:
        video_id = content_id
        key = content_id
    if exact:
        video_id = exact[0].get("video_id") or video_id
        key = exact[0].get("progress_key") or key
    position = int(duration * max(0.0, min(progress, 100.0)) / 100.0)
    return {
        "content_id": content_id,
        "content_type": content_type,
        "video_id": video_id,
        "season": season,
        "episode": episode,
        "position": position,
        "duration": int(duration),
        "last_watched": now_ms(),
        "progress_key": key,
    }


# A show belongs to whichever profile touched it most recently in this window: Nuvio writes a
# progress entry for the active profile when it hands a stream to Infuse, and the bridge's own
# saves keep the same profile for the rest of that session (pauses, stops, the next episode).
ROUTE_WINDOW_MS = 6 * 3600 * 1000


def stamp_ms(value):
    """Nuvio's last_watched as epoch ms, whether it comes back as a number or an ISO string."""
    if isinstance(value, (int, float)):
        return int(value if value > 1e11 else value * 1000)
    if isinstance(value, str) and value:
        try:
            return int(datetime.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)
        except ValueError:
            try:
                return int(float(value))
            except ValueError:
                return 0
    return 0


def route(info):
    """(profile, that profile's progress entries) for this title."""
    candidates = {c for c in (info["imdb"], f"tmdb:{info['tmdb']}") if c}
    default = NUVIO.profile()
    best, best_at, best_entries, default_entries = default, 0, None, None
    cutoff = now_ms() - ROUTE_WINDOW_MS
    for p in NUVIO.profiles():
        index = int(p.get("profile_index", 0))
        entries = NUVIO.progress_entries(index)
        if index == default:
            default_entries = entries
        touched = max((stamp_ms(e.get("last_watched")) for e in entries
                       if e.get("content_id") in candidates), default=0)
        if touched >= cutoff and touched > best_at:
            best, best_at, best_entries = index, touched, entries
    if best_entries is None:
        best_entries = default_entries if default_entries is not None else NUVIO.progress_entries(default)
    return best, best_entries


def clock(ms):
    s = ms // 1000
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"


def handle_scrobble(action, body):
    try:
        progress = float(body.get("progress", 0))
        info = identify(body)
        if not info:
            log(f"  could not identify {json.dumps(body)[:200]} - not saved")
            return
        label = info["title"] + (f" S{info['season']:02d}E{info['episode']:02d}" if info["season"] is not None else "")
        if action == "start":
            # Give Nuvio's handoff entry a moment to reach the server before choosing a profile.
            time.sleep(3)
        profile, entries = route(info)
        entry = build_entry(info, progress, entries)
        if not entry:
            log(f"  {label}: length unknown - not saved")
            return
        NUVIO.push_progress(profile, entry)
        HEALTH["last_error"] = None
        log(f"  saved {label} at {clock(entry['position'])} of {clock(entry['duration'])} ({progress:.1f}%, {action}) to {NUVIO.profile_name(profile)}")
        if action == "stop" and progress >= WATCHED_AT_PERCENT:
            NUVIO.push_watched(profile, {
                "content_id": entry["content_id"],
                "content_type": entry["content_type"],
                "title": info["title"],
                "season": info["season"],
                "episode": info["episode"],
                "watched_at": now_ms(),
            })
            log(f"  marked {label} watched")
    except Exception as e:
        HEALTH["last_error"] = str(e)[:200]
        log(f"  Nuvio update failed: {e}")


# One worker saves reports strictly in arrival order, so a slow "start" can never overwrite
# the "stop" that followed it.
SCROBBLES = queue.Queue()


# For the Uptime Kuma check (GET https://<pi>/bridge-health): the worker thread must be alive
# and the last Nuvio write must not have failed.
HEALTH = {"worker": None, "last_error": None}


def health_status():
    worker = HEALTH["worker"]
    if worker is None or not worker.is_alive():
        return 503, "scrobble worker stopped"
    if HEALTH["last_error"]:
        return 503, f"last Nuvio write failed: {HEALTH['last_error']}"
    return 200, f"ok, {SCROBBLES.qsize()} queued"


def scrobble_worker():
    while True:
        action, body = SCROBBLES.get()
        handle_scrobble(action, body)


# ---------------------------------------------------------------------------
# The HTTPS server Infuse connects to.
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _serve(self):
        host = (self.headers.get("Host") or "").split(":")[0].lower()
        if LOG_REQUESTS and self.path.split("?")[0] != "/bridge-health":
            log(f"{self.client_address[0]} {self.command} {host}{self.path.split('?')[0]} "
                f"({self.headers.get('User-Agent', '?')[:60]})")
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        if host not in TRAKT_HOSTS:
            if self.path.split("?")[0] == "/bridge-health":
                status, text = health_status()
                data = (text + "\n").encode()
                self.send_response(status)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(data)
                return
            self.send_error(421, "Misdirected Request")
            return

        m = re.fullmatch(r"/scrobble/(start|pause|stop)", self.path.split("?")[0])
        if m and self.command == "POST":
            try:
                parsed = json.loads(body or b"{}")
                log(f"Infuse {m.group(1)} ({self.headers.get('User-Agent', '?')}): {json.dumps(parsed)[:220]}")
                SCROBBLES.put((m.group(1), parsed))
            except ValueError:
                pass

        try:
            status, reason, headers, data = forward(host, self.command, self.path, dict(self.headers), body)
        except Exception as e:
            log(f"Trakt unreachable for {self.command} {self.path}: {e}")
            self.send_error(502, "Trakt unreachable")
            return
        self.send_response(status, reason)
        for k, v in headers:
            if k.lower() not in HOP_BY_HOP:
                self.send_header(k, v)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = _serve


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, addr, handler, ctx):
        super().__init__(addr, handler)
        self.ctx = ctx

    def finish_request(self, request, client_address):
        try:
            request.settimeout(30)
            conn = self.ctx.wrap_socket(request, server_side=True)
        except Exception as e:
            # A device that does not trust the certificate fails here, before any request.
            log(f"{client_address[0]} TLS handshake failed: {type(e).__name__}: {str(e)[:160]}")
            return
        self.RequestHandlerClass(conn, client_address, self)

    def handle_error(self, request, client_address):
        # A device closing the connection first (Infuse swiped away, the Apple TV sleeping) is
        # routine; log it in one line instead of a traceback.
        err = sys.exc_info()[1]
        if isinstance(err, (BrokenPipeError, ConnectionResetError, TimeoutError, ssl.SSLError)):
            log(f"{client_address[0]} closed the connection early ({type(err).__name__})")
            return
        super().handle_error(request, client_address)


def main():
    missing = [n for n in ("NUVIO_EMAIL", "NUVIO_PASSWORD", "TMDB_API_KEY") if not os.environ.get(n)]
    if missing:
        log(f"Missing settings: {', '.join(missing)}")
        sys.exit(1)
    os.makedirs(DATA_DIR, exist_ok=True)
    try:
        NUVIO.profile()
    except Exception as e:
        log(f"Could not reach Nuvio yet ({e}); will retry on the first scrobble")
    HEALTH["worker"] = threading.Thread(target=scrobble_worker, daemon=True)
    HEALTH["worker"].start()
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(CERT_FILE, KEY_FILE)
    log(f"Trakt bridge listening on :{LISTEN_PORT}")
    Server(("0.0.0.0", LISTEN_PORT), Handler, ctx).serve_forever()


if __name__ == "__main__":
    main()
