#!/usr/bin/env python3
"""Kuiper: give Infuse YouTube HLS links without a query string.

Infuse refuses an HLS link whose URL carries a ?query when it is the first URL it is handed
(confirmed 2026-10-05: the same playlist plays from a clean .m3u8 URL, and fails as soon as
?i=...&t=...&k=... is appended; it plays fine as a variant inside a master playlist).

This patch:
  1. accepts a path form /yt/hls/<k|->/<movie|series>/<tt...[-S-E]>/<videoId>.m3u8 and rewrites it
     to the existing /yt/hls/<videoId>.m3u8?i=&t=&k= before scrobbling and relaying to yt-resolver;
  2. rewrites the YouTube scraper's stream URLs into that path form.
Old ?query links keep working. Idempotent; backs up index.js first.

Usage: python3 patch_yt_path_urls.py /path/to/index.js
"""
import re, shutil, sys, time

path = sys.argv[1]
src = open(path).read()
MARK = "// [yt-path-urls]"
if MARK in src:
    print(f"{path}: already patched, nothing to do")
    sys.exit(0)

helpers = MARK + r""" Infuse won't open an HLS link whose URL has a ?query (2026-10-05), so YouTube
// playlist links carry i/t/k in the path: /yt/hls/<k|->/<movie|series>/<tt..[-S-E]>/<videoId>.m3u8.
function ytPathUrl(u) {
    const m = /^(.*\/yt\/hls\/)([\w-]{11})\.m3u8\?(.*)$/.exec(String(u || ''));
    if (!m) return u;
    const q = new URLSearchParams(m[3]);
    const i = q.get('i'), t = q.get('t') || 'series', k = q.get('k') || '-';
    if (!i || !/^tt\d+(:\d+:\d+)?$/.test(i) || !/^(movie|series)$/.test(t) || !(k === '-' || /^[a-f0-9]{64}$/.test(k))) return u;
    return `${m[1]}${k}/${t}/${i.replace(/:/g, '-')}/${m[2]}.m3u8`;
}
function ytPathUrls(r) {
    try { for (const s of (r?.data?.streams || [])) { if (s && s.url) s.url = ytPathUrl(s.url); } } catch (_) {}
    return r;
}
"""

# 1. Helpers + path-form rewrite at the top of the /yt handler.
anchor = "app.use('/yt', async (req, res) => {\n"
if src.count(anchor) != 1:
    sys.exit(f"{path}: /yt handler anchor not found exactly once ({src.count(anchor)}) -- not patched")
handler_head = anchor + r"""    // [yt-path-urls] path form -> the query form yt-resolver and the scrobble below expect.
    let ytq = req.query;
    { const pm = /^\/hls\/([a-f0-9]{64}|-)\/(movie|series)\/(tt\d+(?:-\d+-\d+)?)\/([\w-]{11})\.m3u8$/.exec(req.path);
      if (pm) {
          const i = pm[3].replace(/-/g, ':');
          ytq = { i, t: pm[2], ...(pm[1] !== '-' ? { k: pm[1] } : {}) };
          req.url = `/hls/${pm[4]}.m3u8?i=${encodeURIComponent(i)}&t=${pm[2]}${pm[1] !== '-' ? '&k=' + pm[1] : ''}`;
      } }
"""
src = src.replace(anchor, helpers + handler_head)

# The scrobble line reads req.query; point it at ytq (same values for old links).
old_scrobble = "req.query.k && req.query.i && !/axios|node|undici/i.test(String(req.headers['user-agent'] || ''))) ytScrobble(String(req.query.k), String(req.query.t || 'series'), String(req.query.i));"
if src.count(old_scrobble) != 1:
    sys.exit(f"{path}: scrobble line not found exactly once ({src.count(old_scrobble)}) -- not patched")
src = src.replace(old_scrobble, old_scrobble.replace("req.query.", "ytq."))

# 2. YouTube scraper results -> path-form URLs (every occurrence of the scraper call).
call_end = "crypto.createHash('sha256').update(key).digest('hex') : ''}` } }));"
n = src.count(call_end)
if n < 1:
    sys.exit(f"{path}: YouTube scraper call not found -- not patched")
src = src.replace(call_end, "crypto.createHash('sha256').update(key).digest('hex') : ''}` } }).then(ytPathUrls));")

# 3. Safety net for answers served from cache (Redis / saved results made before this patch):
#    rewrite YouTube playlist links in every stream response on its way out.
old_json = "    res.json = body => { try { rec(body); } catch (_) {} return origJson(body); };"
old_send = "    res.send = body => { if (!res._kuiperGap && typeof body === 'string' && body.startsWith('{\"streams\"')) { try { rec(JSON.parse(body)); } catch (_) {} } return origSend(body); };"
if src.count(old_json) == 1 and src.count(old_send) == 1:
    src = src.replace(old_json, "    res.json = body => { try { for (const s of (body?.streams || [])) { if (s && s.url) s.url = ytPathUrl(s.url); } } catch (_) {} try { rec(body); } catch (_) {} return origJson(body); };")
    src = src.replace(old_send, "    res.send = body => { if (typeof body === 'string' && body.includes('/yt/hls/')) { try { body = body.replace(/https?:\\/\\/[^\"\\s]*\\/yt\\/hls\\/[\\w-]{11}\\.m3u8\\?[^\"\\s]*/g, m => ytPathUrl(m)); } catch (_) {} } " + old_send.split("res.send = body => { ", 1)[1])
    print(f"{path}: cached-answer safety net added")
else:
    print(f"{path}: WARNING stream-response middleware not found ({src.count(old_json)}/{src.count(old_send)}); only fresh results get clean links")

backup = f"{path}.bak-ytpath-{time.strftime('%Y%m%d-%H%M%S')}"
shutil.copy2(path, backup)
open(path, "w").write(src)
print(f"{path}: patched (YouTube scraper call sites: {n}); backup at {backup}")
