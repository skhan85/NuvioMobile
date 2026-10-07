#!/usr/bin/env python3
"""Kuiper: relay add-on subtitles to Infuse as plain .srt links.

Nuvio hands Infuse the subtitle links it gets from subtitle add-ons (OpenSubtitles and the like).
Those links have no file ending, often redirect, and are sometimes gzipped or WebVTT, and Infuse
showed none of them (2026-10-06). The Nuvio forks now send each one as
    https://<kuiper>/subs/<base64url of the original link>/<language>.srt
and this route fetches the original, unzips it, turns WebVTT into SRT and serves it as
application/x-subrip. Every request is logged as [SUBS] with the caller's user agent, so the
container log shows whether Nuvio sent a subtitle and whether Infuse fetched it.

Idempotent; backs up index.js first.
Usage: python3 patch_subs_proxy.py /path/to/index.js
"""
import shutil, sys, time

path = sys.argv[1]
src = open(path).read()
MARK = "// [subs-proxy]"
if MARK in src:
    print(f"{path}: already patched, nothing to do")
    sys.exit(0)

route = MARK + r""" Infuse subtitle relay: /subs/<base64url of the add-on's subtitle link>/<name>.srt
function subsVttToSrt(text) {
    const blocks = text.replace(/\r/g, '').split(/\n{2,}/);
    const out = [];
    for (const block of blocks) {
        const lines = block.split('\n');
        const at = lines.findIndex(l => l.includes('-->'));
        if (at < 0) continue;
        const time = lines[at].replace(/(\d{2}:)?(\d{2}):(\d{2})\.(\d{3})/g, (m, h, mm, ss, ms) => `${h || '00:'}${mm}:${ss},${ms}`)
            .replace(/\s+(align|position|line|size|vertical|region):\S+/g, '');
        const body = lines.slice(at + 1).join('\n').trim();
        if (body) out.push(`${out.length + 1}\n${time}\n${body}`);
    }
    return out.join('\n\n') + '\n';
}
app.get(/^\/subs\/([A-Za-z0-9_-]+)\/[^/]*$/, async (req, res) => {
    const ua = String(req.headers['user-agent'] || '').slice(0, 60);
    let source = '';
    try { source = Buffer.from(req.params[0], 'base64url').toString('utf8'); } catch (_) {}
    if (!/^https?:\/\//.test(source)) { console.log(`[SUBS] bad link ua=${ua}`); return res.status(400).end(); }
    try {
        const r = await axios.get(source, { responseType: 'arraybuffer', timeout: 15000, maxRedirects: 5, proxy: false,
            headers: { 'User-Agent': 'Mozilla/5.0', 'Accept-Encoding': 'gzip' }, decompress: true });
        let buf = Buffer.from(r.data);
        if (buf[0] === 0x1f && buf[1] === 0x8b) buf = require('zlib').gunzipSync(buf);
        let text = buf.toString('utf8').replace(/^﻿/, '');
        if (/^WEBVTT/.test(text)) text = subsVttToSrt(text);
        console.log(`[SUBS] ok ${text.length}B ua=${ua} src=${source.slice(0, 120)}`);
        res.set('Content-Type', 'application/x-subrip; charset=utf-8');
        res.set('Cache-Control', 'public, max-age=86400');
        return res.send(text);
    } catch (e) {
        console.log(`[SUBS] fail ${e.response ? e.response.status : e.message} ua=${ua} src=${source.slice(0, 120)}`);
        return res.status(502).end();
    }
});
"""

# Register before every other route so no catch-all can swallow /subs/...
anchor = None
for candidate in ("const app = express();\n", "const app = express()\n"):
    if src.count(candidate) == 1:
        anchor = candidate
        break
if anchor is None:
    sys.exit(f"{path}: 'const app = express();' not found exactly once -- not patched")
src = src.replace(anchor, anchor + route)

backup = f"{path}.bak-subsproxy-{time.strftime('%Y%m%d-%H%M%S')}"
shutil.copy2(path, backup)
open(path, "w").write(src)
print(f"{path}: patched; backup at {backup}")
