#!/usr/bin/env python3
"""Kuiper: line add-on subtitles up with the video before Infuse gets them.

OpenSubtitles' files are often timed for a different release (2026-10-07: all five English files
for a Strange New Worlds episode were 0.8-1.2 s early and drifted 0.1%). The Nuvio forks now put
the video link in the relay path:
    /subs/<base64url subtitle link>/v/<base64url video link>/<name>.srt
and this patch hands such subtitles to the `subsync` container (ffsubsync against the video's
audio). It does not wait by default (?wait=N, max 45, is how Nuvio warms it up ahead of time). It
serves the synced file when ready, otherwise the original; the synced copy is cached,
so it is used from the next fetch on. Links without /v/ behave exactly as before.

Needs patch_subs_proxy.py applied first. Idempotent; backs up index.js first.
Usage: python3 patch_subs_sync.py /path/to/index.js
"""
import shutil, sys, time

path = sys.argv[1]
src = open(path).read()
MARK = "// [subs-sync]"
if MARK in src:
    print(f"{path}: already patched, nothing to do")
    sys.exit(0)
if "// [subs-proxy]" not in src:
    sys.exit(f"{path}: run patch_subs_proxy.py first -- not patched")

old_route = "app.get(/^\\/subs\\/([A-Za-z0-9_-]+)\\/[^/]*$/, async (req, res) => {"
new_route = ("app.get(/^\\/subs\\/([A-Za-z0-9_-]+)(?:\\/v\\/([A-Za-z0-9_-]+))?\\/[^/]*$/, async (req, res) => { "
             + MARK + " optional /v/<video> segment")
old_log = "        console.log(`[SUBS] ok ${text.length}B ua=${ua} src=${source.slice(0, 120)}`);"
new_log = r"""        // [subs-sync] Line it up with the video's audio when the link names the video.
        let synced = '';
        if (req.params[1]) {
            try {
                const video = Buffer.from(req.params[1], 'base64url').toString('utf8');
                const wait = Math.min(Math.max(parseInt(req.query.wait, 10) || 0, 0), 45);
                const s = await axios.post(`${process.env.SUBSYNC_URL || 'http://subsync:8080'}/sync?v=${encodeURIComponent(video)}&wait=${wait}`,
                    text, { headers: { 'Content-Type': 'text/plain; charset=utf-8' }, timeout: (wait + 10) * 1000,
                        proxy: false, responseType: 'text', transformResponse: x => x, validateStatus: () => true });
                synced = s.status === 200 && String(s.data).includes('-->') ? ' synced' : (s.status === 202 ? ' syncing' : ' as-is');
                if (s.status === 200 && String(s.data).includes('-->')) text = String(s.data);
            } catch (e) { synced = ` sync-error(${e.message})`; }
        }
        console.log(`[SUBS] ok${synced} ${text.length}B ua=${ua} src=${source.slice(0, 120)}`);"""
for old in (old_route, old_log):
    if src.count(old) != 1:
        sys.exit(f"{path}: expected relay code not found exactly once ({src.count(old)}) -- not patched")
src = src.replace(old_route, new_route).replace(old_log, new_log)

backup = f"{path}.bak-subssync-{time.strftime('%Y%m%d-%H%M%S')}"
shutil.copy2(path, backup)
open(path, "w").write(src)
print(f"{path}: patched; backup at {backup}")
