"""Give Infuse something it can pick for the profile favourites' artwork.

Infuse's "Select Artwork" only accepts a playable title (a movie or show), never a folder. So each
profile gets one dedicated title: a library title that sits in none of the Nuvio sections, given
the profile picture as its poster and backdrop and a private tag. The "Saad & Sabrina Avatar" /
"Kids Avatar" collections are narrowed to that one tag, so opening them shows exactly one title.

Safe to rerun: the chosen titles are remembered in ~/.remux_avatars.json.
Usage on the VPS:  python3 profile_art.py        (REMUX_KEY comes from ~/.remux_key)
"""
import base64, json, os, sys, urllib.request, urllib.error

BASE = os.environ.get("REMUX_URL", "http://localhost:3010")
KEY = (os.environ.get("REMUX_KEY") or open(os.path.expanduser("~/.remux_key")).read()).strip()
ART = "https://raw.githubusercontent.com/skhan85/NuvioMobile/cmp-rewrite/tools/remux/art/"
STATE = os.path.expanduser("~/.remux_avatars.json")
PROFILES = [  # collection, private tag, extra tags, poster, backdrop
    ("Saad & Sabrina Avatar", "avatar-saad", [], "avatar-saad-sabrina.png", "favorite-saad-sabrina.png"),
    ("Kids Avatar", "avatar-kids", ["kids"], "avatar-kids.png", "favorite-kids.png"),
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

# 1. Titles shown anywhere in the Nuvio sections (the non-promoted folder collections).
sections = [b for n, b in boxsets.items() if not n.endswith("Avatar") and n not in ("Profile", "Kids Profile")]
shown = set()
for b in sections:
    for it in items(f"ParentId={b}&Recursive=true&IncludeItemTypes=Movie,Series&Fields=Tags&Limit=10000"):
        shown.add(it["Id"])
print(f"{len(shown)} titles appear in the Nuvio sections")

# 2. Spare titles: in the library but in no section. Prefer movies (a single file, no seasons).
library = items("Recursive=true&IncludeItemTypes=Movie,Series&Fields=Tags&SortBy=SortName&Limit=20000")
spare = [i for i in library if i["Id"] not in shown and i["Id"] not in state.values()]
spare.sort(key=lambda i: i.get("Type") != "Movie")
print(f"{len(spare)} spare titles to use as artwork holders")

for coll, tag, extra, poster, backdrop in PROFILES:
    item_id = state.get(coll)
    if not item_id:
        if not spare:
            sys.exit("No spare title left - run this again after the next Refresh Library.")
        pick = spare.pop(0)
        item_id = state[coll] = pick["Id"]
        print(f"{coll}: using '{pick['Name']}' ({pick.get('Type')})")
    current = call("GET", f"/Items/{item_id}") or {}
    tags = sorted(set((current.get("Tags") or []) + [tag] + extra))
    call("PATCH", f"/items/{item_id}", {"Name": coll.replace(" Avatar", ""), "Tags": tags})
    call("POST", f"/Items/{item_id}/Images/Primary", raw=base64.b64encode(fetch(ART + poster)), ctype="image/*")
    call("POST", f"/Items/{item_id}/Images/Backdrop", raw=base64.b64encode(fetch(ART + backdrop)), ctype="image/*")
    check = call("GET", f"/Items/{item_id}") or {}
    print(f"  title now '{check.get('Name')}', tags {check.get('Tags')}")
    if tag not in (check.get("Tags") or []):
        print("  ! Remux didn't keep the tag on this title - send Claude this output.")
    # The avatar collection holds just this one title.
    if coll in boxsets:
        call("PATCH", f"/items/{boxsets[coll]}", {"SmartFilter": {"match_mode": "any", "groups": [
            {"match_mode": "any", "rules": [{"field": "tag", "op": "in", "values": [tag]}]}]}})
json.dump(state, open(STATE, "w"))

for coll in ("Saad & Sabrina Avatar", "Kids Avatar"):
    if coll in boxsets:
        names = [i["Name"] for i in items(f"ParentId={boxsets[coll]}&Recursive=true&Limit=20")]
        print(f"{coll} now contains: {names}")
