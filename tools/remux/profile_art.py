"""Give Infuse something it can pick for the profile favourites' artwork.

Infuse's "Select Artwork" only accepts a playable title (a movie or show), never a folder. So each
profile borrows one library title (the lowest-rated movie that isn't already used), renamed to the
profile name, given the profile picture as poster and backdrop, tagged privately and locked so a
library refresh doesn't undo it. "Saad & Sabrina Avatar" / "Kids Avatar" hold just that title, and
nuvio_rows.py keeps both titles out of every section (run it after this one).

Safe to rerun: the chosen titles are remembered in ~/.remux_avatars.json.
Usage on the VPS:  python3 profile_art.py        (REMUX_KEY comes from ~/.remux_key)
"""
import base64, json, os, urllib.request, urllib.error

BASE = os.environ.get("REMUX_URL", "http://localhost:3010")
KEY = (os.environ.get("REMUX_KEY") or open(os.path.expanduser("~/.remux_key")).read()).strip()
ART = "https://raw.githubusercontent.com/skhan85/NuvioMobile/cmp-rewrite/tools/remux/art/"
STATE = os.path.expanduser("~/.remux_avatars.json")
PROFILES = [  # collection, title name, private tag, extra tags, poster, backdrop
    ("Saad & Sabrina Avatar", "Saad & Sabrina", "avatar-saad", [], "avatar-saad-sabrina.png",
     "favorite-saad-sabrina.png"),
    ("Kids Avatar", "Kids", "avatar-kids", ["kids"], "avatar-kids.png", "favorite-kids.png"),
]


def call(method, path, body=None, raw=None, ctype="application/json"):
    data = raw if raw is not None else (None if body is None else json.dumps(body).encode())
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"X-Emby-Token": KEY, "Content-Type": ctype})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            out = r.read()
            return json.loads(out) if out.strip() else None
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{method} {path} -> HTTP {e.code}: {e.read()[:200]!r}")


def fetch(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=30) as r:
        return r.read()


def items(query):
    return call("GET", "/items?" + query)["Items"]


boxsets = {i["Name"]: i["Id"] for i in items("IncludeItemTypes=BoxSet&IncludeChildless=true&Recursive=true&Limit=5000")}
state = json.load(open(STATE)) if os.path.exists(STATE) else {}

# Candidates: movies without the kids tag, lowest rated first (nobody will miss them in a section).
movies = items("Recursive=true&IncludeItemTypes=Movie&Fields=Tags,CommunityRating&Limit=20000")
movies = [m for m in movies if "kids" not in (m.get("Tags") or []) and m["Id"] not in state.values()]
movies.sort(key=lambda m: (m.get("CommunityRating") or 0, m.get("ProductionYear") or 0))

for coll, title, tag, extra, poster, backdrop in PROFILES:
    item_id = state.get(coll)
    if not item_id:
        pick = movies.pop(0)
        item_id = state[coll] = pick["Id"]
        json.dump(state, open(STATE, "w"))
        print(f"{coll}: borrowing '{pick['Name']}' ({pick.get('ProductionYear')}, rating {pick.get('CommunityRating')})")
    current = call("GET", f"/Items/{item_id}") or {}
    tags = sorted(set((current.get("Tags") or []) + [tag] + extra))
    # LockData stops a library refresh from renaming it back or changing its tags.
    call("POST", f"/items/{item_id}", {"Name": title, "Tags": tags, "LockedFields": ["Name", "Tags"],
                                       "LockData": True})
    call("POST", f"/Items/{item_id}/Images/Primary", raw=base64.b64encode(fetch(ART + poster)), ctype="image/*")
    call("POST", f"/Items/{item_id}/Images/Backdrop", raw=base64.b64encode(fetch(ART + backdrop)), ctype="image/*")
    check = call("GET", f"/Items/{item_id}") or {}
    ok = check.get("Name") == title and tag in (check.get("Tags") or [])
    print(f"  {'ok' if ok else '!'} title '{check.get('Name')}', tags {check.get('Tags')}")
    if coll in boxsets:
        call("PATCH", f"/items/{boxsets[coll]}", {"SmartFilter": {"match_mode": "any", "groups": [
            {"match_mode": "any", "rules": [{"field": "tag", "op": "in", "values": [tag]}]}]}})

for coll, *_ in PROFILES:
    if coll in boxsets:
        names = [i["Name"] for i in items(f"ParentId={boxsets[coll]}&Recursive=true&Limit=20")]
        print(f"{coll} now contains: {names}")
