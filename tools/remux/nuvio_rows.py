"""Recreate Saad's Nuvio home rows (Streaming Services, Genres, Studios, ...) as Remux collections.

Each Nuvio folder becomes a smart Remux collection filled from the matching Xperience catalogs
(or, for studios Xperience has no catalog for, from the Studio field), with the folder's Nuvio
cover art. Each Nuvio row becomes a promoted group (a library in Infuse) holding its folders.
Safe to rerun: existing collections are updated in place, never duplicated.

Usage on the VPS:  REMUX_KEY=... python3 nuvio_rows.py
"""
import base64, json, os, sys, urllib.request, urllib.error

BASE = os.environ.get("REMUX_URL", "http://localhost:3010")
KEY = os.environ.get("REMUX_KEY", "").strip() or sys.exit("Set REMUX_KEY first.")
ROWS_URL = "https://raw.githubusercontent.com/skhan85/NuvioMobile/cmp-rewrite/tools/remux/nuvio_rows.json"
NEW_CATALOG_MAX = 60   # titles imported per newly enabled catalog

# Folders Xperience has no catalog for, filled from the library's Studio field instead.
STUDIO_RULES = {
    "20th Century Studios": ["20th Century Studios", "20th Century Fox"],
    "Focus Features": ["Focus Features"],
    "Illumination": ["Illumination", "Illumination Entertainment"],
    "Lionsgate": ["Lionsgate", "Lions Gate Films", "Lionsgate Films"],
    "MGM": ["Metro-Goldwyn-Mayer", "MGM"],
    "New Line Cinema": ["New Line Cinema"],
    "Searchlight Pictures": ["Searchlight Pictures", "Fox Searchlight Pictures"],
}


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


def fetch(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


rows = json.loads(fetch(os.environ.get("ROWS_URL", ROWS_URL)))

# 1. Turn on every Xperience catalog the rows need (leave the ones already on alone).
addons = {a["name"]: a for a in call("GET", "/addons")}
cat_ids = {}   # "Xperience — Netflix — Movie" (or "kids:...") -> catalog collection UUID
for addon_name, prefix in (("Xperience", ""), ("Xperience Kids", "kids:")):
    addon = addons.get(addon_name)
    if not addon:
        sys.exit(f"Addon '{addon_name}' not found - run remux_setup.py first.")
    wanted = {c[len(prefix):] for r in rows for f in r["folders"] for c in f["catalogs"]
              if (c.startswith("kids:") if prefix else not c.startswith("kids:"))}
    cats = call("GET", f"/addons/{addon['id']}/catalogs")
    updates, turned_on = [], 0
    for c in cats:
        local = c["catalogId"].split(":", 2)[2] if c["catalogId"].startswith("addon:") else c["catalogId"]
        on = c["enabled"] or c["name"] in wanted
        turned_on += on and not c["enabled"]
        updates.append({"catalogId": local, "enabled": on,
                        "maxItems": c.get("maxItems") or NEW_CATALOG_MAX, "tags": c.get("tags") or []})
        if c["name"] in wanted and c.get("collectionId"):
            cat_ids.setdefault(prefix + c["name"], []).append(c["collectionId"])
    call("POST", f"/addons/{addon['id']}/catalogs", updates)
    missing = wanted - {c["name"] for c in cats}
    print(f"{addon_name}: {turned_on} more catalogs switched on")
    for m in sorted(missing):
        print(f"  not found in {addon_name}: {m}")

# 2. One smart collection per Nuvio folder, with its Nuvio cover art.
existing = {i["Name"]: i["Id"] for i in call(
    "GET", "/items?IncludeItemTypes=BoxSet&IncludeChildless=true&Recursive=true&Limit=5000")["Items"]}


def upsert(name, ctype, smart_filter, promoted, sort_order, tags=None):
    item_id = existing.get(name)
    if not item_id:
        item_id = call("POST", "/library/virtualfolders",
                       {"Name": name, "CollectionType": ctype, "CollectionKind": "smart",
                        "Promoted": promoted, "SortOrder": sort_order})["ItemId"]
    patch = {"Name": name, "CollectionType": ctype, "CollectionKind": "smart",
             "SmartFilter": smart_filter, "Promoted": promoted, "SortOrder": sort_order}
    if tags:
        patch["Tags"] = tags   # the Kids user only sees items carrying its allowed tag
    call("PATCH", f"/items/{item_id}", patch)
    new = name not in existing
    existing[name] = item_id
    return item_id, new


def any_of(rules):
    return {"match_mode": "any", "groups": [{"match_mode": "any", "rules": rules}]}


made, updated, skipped, art_failed = 0, 0, [], []
for ri, row in enumerate(rows):
    child_ids = []
    for fi, folder in enumerate(row["folders"]):
        rules = []
        ids = [i for c in folder["catalogs"] for i in cat_ids.get(c, [])]
        if ids:
            rules.append({"field": "catalog", "op": "in", "catalog_ids": ids})
        if folder["title"] in STUDIO_RULES:
            rules.append({"field": "studio", "op": "in", "values": STUDIO_RULES[folder["title"]]})
        if not rules:
            skipped.append(f"{row['title']} / {folder['title']}")
            continue
        try:
            item_id, new = upsert(folder["title"], "mixed", any_of(rules), False, ri * 100 + fi,
                                  row.get("tags"))
        except RuntimeError as e:
            print(f"  ! {folder['title']}: {e}")
            continue
        made += new
        updated += not new
        child_ids.append(item_id)
        if folder["image"] and new:
            try:
                img = fetch(folder["image"])
                call("POST", f"/Items/{item_id}/Images/Primary",
                     raw=base64.b64encode(img), ctype="image/*")
            except Exception as e:
                art_failed.append(f"{folder['title']} ({e.__class__.__name__})")
    # 3. The row itself: a promoted group (shows as its own library in Infuse) of those collections.
    if child_ids:
        upsert(row["title"], "collections",
               any_of([{"field": "collection_id", "op": "in", "ids": child_ids}]), True, 10_000 + ri,
               row.get("tags"))
        print(f"{row['title']}: {len(child_ids)} collections")

print(f"\nCreated {made}, updated {updated}.")
if skipped:
    print("No source for these (left out):", *skipped, sep="\n  ")
if art_failed:
    print("Cover art couldn't be downloaded for:", *art_failed, sep="\n  ")

# 4. Fill everything.
task = next(t for t in call("GET", "/scheduledtasks") if (t.get("Name") or t.get("name")) == "Refresh Library")
try:
    call("POST", f"/scheduledtasks/running/{task.get('Id') or task.get('id')}")
    print("\nRefresh Library started - the new catalogs take a while to fill.")
except RuntimeError as e:
    print(f"\nStart Refresh Library from Tasks yourself ({e}).")
