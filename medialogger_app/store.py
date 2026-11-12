"""Profiles and the log: one SQLite file per profile, plus the portable JSON form of a profile.

Two import rules keep the log safe. Site data (`diff`/`apply`) never touches a row saved by hand and never deletes.
Profile files are strangers' input until proven otherwise: `read_profile` checks every field before anything is written.
"""
import json
import math
import os
import re
import sqlite3
import time
import unicodedata
import urllib.parse
from datetime import date
from pathlib import Path

KINDS = {"game": "Games", "anime": "Anime", "manga": "Manga", "music": "Music", "film": "Films", "novel": "Light novels", "vn": "Visual novels", "character": "Characters"}
STATUSES = ("planned", "active", "done", "paused", "dropped")
SITES = ("backloggd", "anilist", "rym", "letterboxd", "ranobedb", "vndb", "wikidata", "manual")
SITE_OF = {"game": "backloggd", "anime": "anilist", "manga": "anilist", "music": "rym", "film": "letterboxd", "novel": "ranobedb", "vn": "vndb", "character": "anilist"}
USERNAMES = ("anilist", "vndb", "letterboxd")
# every host a cover may come from, redirects included: a crafted profile must not make the app call anything else
COVER_HOSTS = ("anilist.co", "vndb.org", "ranobedb.org", "steamstatic.com", "coverartarchive.org", "archive.org", "wikimedia.org", "ltrbxd.com")

SITE_FIELDS = ("title", "creator", "year", "url", "cover", "status", "rating", "progress", "started", "finished", "notes", "nsfw")
FIELDS = ("kind", "site", "key", *SITE_FIELDS, "edited", "created", "updated")
FORMAT, VERSION = "medialogger-profile", 1
MAX_FILE, MAX_ENTRIES = 64 << 20, 200_000
SCHEMA = """
create table if not exists entries(
  id integer primary key, kind text not null, site text not null, key text not null, title text not null,
  creator text not null default '', year integer, url text not null default '', cover text not null default '',
  status text not null, rating real, progress integer, started text not null default '', finished text not null default '',
  notes text not null default '', nsfw integer not null default 0, edited integer not null default 0, created integer not null, updated integer not null,
  unique(site, key));
create table if not exists settings(k text primary key, v text not null);
"""


# titles that say what the work is. ponytail: a word list, so it misses some and blurs a few innocent ones
# (Sex Pistols); every cover is one click from shown, and the Log dialog's Adult box overrides it either way
ADULT_WORDS = re.compile(r"(?<![a-z])(hentai|ecchi|eroge|nukige|porno?|pornographic|xxx|nsfw|r-?18|18\+|uncensored|lewd|nude|naked|nudity|"
                         r"sex|sexy|sexual|erotic|erotica|fetish|bdsm|milf|futanari|ahegao|oppai|ntr)(?![a-z])"
                         r"|エロ|エッチ|えっち|18禁|成人向け|淫|陵辱|凌辱|触手|調教|中出し|孕ま|痴漢|痴女|爆乳|催眠|寝取", re.I)


def adult(entry):
    """Blur this cover? nsfw 1: a site or you said adult. -1: you said it's fine, whatever the title. 0: the title decides."""
    nsfw = entry.get("nsfw") or 0
    return nsfw > 0 or nsfw == 0 and bool(ADULT_WORDS.search(entry.get("title", "")))


def norm(s):
    """Same title typed or exported differently -> same string."""
    return re.sub(r"[\W_]+", " ", unicodedata.normalize("NFKC", s).casefold()).strip()


def make_key(kind, title, year=None, creator=""):
    """The key for rows without a stable site id, shared by search, exports and RSS.
    ponytail: differently spelled titles or years off by one between sources give two rows; merge by hand."""
    return f"{norm(creator)}|{norm(title)}" if kind == "music" else f"{norm(title)}|{year or ''}"


def cover_ok(url):
    u = urllib.parse.urlsplit(url)
    host = (u.hostname or "").lower()
    return u.scheme == "https" and any(host == h or host.endswith("." + h) for h in COVER_HOSTS)


def _text(r, field, limit, default=""):
    v = r.get(field, default)
    if not isinstance(v, str) or len(v) > limit:
        raise ValueError(f"{field} must be text of at most {limit} characters")
    return v


def _int(r, field, low, high):
    v = r.get(field)
    if v is not None and (type(v) is not int or not low <= v <= high):
        raise ValueError(f"{field} must be a whole number from {low} to {high}")
    return v


def _date(r, field):
    v = _text(r, field, 10)
    if v:
        try:
            date.fromisoformat(v)
        except ValueError:
            raise ValueError(f"{field} must be YYYY-MM-DD") from None
    return v


def clean(r):
    """A row with exactly FIELDS, all checked, or ValueError. Everything entering the database passes here."""
    if not isinstance(r, dict):
        raise ValueError("not an object")
    out = {"kind": r.get("kind"), "site": r.get("site"), "status": r.get("status")}
    for field, allowed in (("kind", KINDS), ("site", SITES), ("status", STATUSES)):
        if not isinstance(out[field], str) or out[field] not in allowed:
            raise ValueError(f"bad {field}: {out[field]!r}")
    out["key"], out["title"] = _text(r, "key", 1000), _text(r, "title", 1000).strip()
    if not out["key"] or not out["title"]:
        raise ValueError("key and title are required")
    out["creator"] = _text(r, "creator", 1000)
    out["year"], out["progress"] = _int(r, "year", 1000, 3000), _int(r, "progress", 0, 10_000_000)
    out["url"], out["cover"] = _text(r, "url", 2000), _text(r, "cover", 2000)
    if out["url"] and not (out["url"].startswith("https://") and urllib.parse.urlsplit(out["url"]).hostname):
        raise ValueError(f"url must be https: {out['url'][:80]!r}")
    if out["cover"] and not cover_ok(out["cover"]):
        raise ValueError(f"cover host not allowed: {out['cover'][:80]!r}")
    rating = r.get("rating")
    if rating is not None and (type(rating) not in (int, float) or not math.isfinite(rating) or not 0 <= rating <= 10):
        raise ValueError(f"rating must be 0-10: {rating!r}")
    out["rating"] = rating
    out["started"], out["finished"] = _date(r, "started"), _date(r, "finished")
    out["notes"] = _text(r, "notes", 100_000)
    nsfw = r.get("nsfw") or 0  # absent in profiles from before adult covers were blurred
    if nsfw not in (-1, 0, 1):
        raise ValueError(f"nsfw must be -1, 0 or 1: {nsfw!r}")
    out["nsfw"] = int(nsfw)
    out["edited"] = 1 if r.get("edited") else 0
    out["created"] = _int({"created": r.get("created", 0)}, "created", 0, 1 << 40)
    out["updated"] = _int({"updated": r.get("updated", 0)}, "updated", 0, 1 << 40)
    return out


# ---------- files and profiles ----------

def data_dir():
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / "medialogger"


def cache_dir():
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "medialogger"


def write_private(path, text):
    """Atomic and 0600: notes are private and the file names your site accounts."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def load_conf():
    try:
        conf = json.loads((data_dir() / "config.json").read_text(encoding="utf-8"))
        return conf if isinstance(conf, dict) else {}
    except (OSError, ValueError):
        return {}


def save_conf(conf):
    write_private(data_dir() / "config.json", json.dumps(conf, indent=1))


WINDOWS_RESERVED = re.compile(r"(con|prn|aux|nul|com\d|lpt\d)(\..*)?", re.I)  # Windows can't create these, whatever the extension


def safe_name(name):
    """Profile name -> filename stem. Imported names are untrusted: no separators, no leading dots."""
    name = re.sub(r"[^\w ().-]+", "_", name).strip(" .")[:80] or "profile"
    return "_" + name if WINDOWS_RESERVED.fullmatch(name) else name


def db_path(name):
    return data_dir() / "profiles" / f"{safe_name(name)}.db"


def list_profiles():
    return sorted(p.stem for p in (data_dir() / "profiles").glob("*.db"))


def unique_name(name):
    name, n = safe_name(name), 2
    base = name
    while db_path(name).exists():
        name, n = f"{base} ({n})", n + 1
    return name


def open_db(name):
    path = db_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    if "nsfw" not in {c["name"] for c in db.execute("pragma table_info(entries)")}:  # profiles made before covers were blurred
        with db:
            db.execute("alter table entries add column nsfw integer not null default 0")
    return db


def get_setting(db, k):
    row = db.execute("select v from settings where k=?", (k,)).fetchone()
    return row["v"] if row else ""


def set_setting(db, k, v):
    with db:
        db.execute("insert into settings values(?,?) on conflict(k) do update set v=excluded.v", (k, v))


# ---------- entries ----------

def entries(db, kind=None):
    if kind:
        cur = db.execute("select * from entries where kind=? order by title collate nocase", (kind,))
    else:
        cur = db.execute("select * from entries order by title collate nocase")
    return [dict(r) for r in cur]


def find(db, site, key):
    row = db.execute("select * from entries where site=? and key=?", (site, key)).fetchone()
    return dict(row) if row else None


def _insert(db, r):
    db.execute(f"insert into entries({','.join(FIELDS)}) values({','.join('?' * len(FIELDS))})", [r[f] for f in FIELDS])


def save_entry(db, row):
    """Saved by hand: from now on syncs leave this row alone. A new row landing on an existing key takes it over."""
    now = int(time.time())
    r = clean({"created": now, **row, "edited": 1, "updated": now})
    old = find(db, r["site"], r["key"])
    with db:
        if old:
            db.execute(f"update entries set {','.join(f + '=?' for f in SITE_FIELDS)},edited=1,updated=? where id=?",
                       [r[f] for f in SITE_FIELDS] + [now, old["id"]])
        else:
            _insert(db, r)


def delete_entry(db, entry_id):
    with db:
        db.execute("delete from entries where id=?", (entry_id,))


def _known(field, value):
    """Whether a site said something. For nsfw only an adult flag (1) counts: 0 means "no flag seen", and a source
    without adult flags (an RSS feed, a CSV) must not clear one another source set."""
    return value not in ("", None) and not (field == "nsfw" and value != 1)


def diff(db, rows):
    """What a pull or file import would do, without doing it. -> {new, update, kept, same}.
    Rows sharing a key fold together in order, so later files fill in what earlier ones lacked."""
    merged = {}
    for r in rows:
        r = dict(r)
        if not cover_ok(r.get("cover", "")):
            r["cover"] = ""  # a site's unknown CDN costs a picture, not the whole sync
        r = clean(r)
        k = r["site"], r["key"]
        merged[k] = {**merged[k], **{f: r[f] for f in SITE_FIELDS if _known(f, r[f])}} if k in merged else r
    out = {"new": [], "update": [], "kept": 0, "same": 0}
    for (site, key), r in merged.items():
        old = find(db, site, key)
        if old is None:
            out["new"].append(r)
        elif old["edited"]:
            out["kept"] += 1
        else:
            changes = {f: r[f] for f in SITE_FIELDS if _known(f, r[f]) and r[f] != old[f]}
            if changes:
                out["update"].append((old["id"], changes))
            else:
                out["same"] += 1
    return out


def apply(db, d):
    now = int(time.time())
    with db:
        for r in d["new"]:
            _insert(db, {**r, "created": now, "updated": now})
        for entry_id, changes in d["update"]:
            # field names come from SITE_FIELDS, never from the data
            db.execute(f"update entries set {','.join(f + '=?' for f in changes)},updated=? where id=?", [*changes.values(), now, entry_id])


def summary(d):
    return f"+{len(d['new'])} new, {len(d['update'])} updated, {d['kept']} kept local, {d['same']} unchanged"


# ---------- profile files ----------

def export_profile(db, name, path):
    doc = {"format": FORMAT, "version": VERSION, "name": name, "exported": int(time.time()),
           "settings": {k: get_setting(db, k) for k in USERNAMES},
           "entries": [{f: e[f] for f in FIELDS} for e in entries(db)]}
    write_private(path, json.dumps(doc, ensure_ascii=False, indent=1))


def read_profile(path):
    """Parse and fully validate a profile file -> {name, settings, entries}, or ValueError saying what is wrong."""
    path = Path(path)
    if path.stat().st_size > MAX_FILE:
        raise ValueError("file is too large to be a profile")
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeDecodeError) as e:
        raise ValueError(f"not valid JSON: {e}") from None
    if not isinstance(doc, dict) or doc.get("format") != FORMAT:
        raise ValueError("not a medialogger profile")
    if type(doc.get("version")) is not int or doc["version"] < 1:
        raise ValueError("profile has no valid version")
    if doc["version"] > VERSION:
        raise ValueError(f"profile is format {doc['version']}, this app reads up to {VERSION}")
    rows, settings = doc.get("entries"), doc.get("settings", {})
    if not isinstance(doc.get("name"), str) or not isinstance(rows, list) or not isinstance(settings, dict):
        raise ValueError("profile is missing its name or entries")
    if len(rows) > MAX_ENTRIES:
        raise ValueError(f"profile has over {MAX_ENTRIES} entries")
    newest = {}
    for i, row in enumerate(rows, 1):
        try:
            r = clean(row)
        except ValueError as e:
            raise ValueError(f"entry {i}: {e}") from None
        k = r["site"], r["key"]
        if k not in newest or r["updated"] > newest[k]["updated"]:
            newest[k] = r
    keep = {k: v[:200] for k, v in settings.items() if k in USERNAMES and isinstance(v, str)}
    return {"name": doc["name"], "settings": keep, "entries": list(newest.values())}


def import_new(doc):
    """A new profile from a validated file; the name never lands on an existing profile. -> its name"""
    name = unique_name(doc["name"])
    db = open_db(name)
    try:
        with db:
            for r in doc["entries"]:
                _insert(db, r)
            db.executemany("insert into settings values(?,?)", doc["settings"].items())
    finally:
        db.close()
    return name


def merge_profile(db, doc):
    """Merge a validated file into this profile, in one transaction. Newest `updated` wins per entry: this is the
    same log coming back from another machine, so site-sync's edited rule doesn't apply. -> (added, replaced, kept)"""
    added = replaced = kept = 0
    with db:
        for r in doc["entries"]:
            old = find(db, r["site"], r["key"])
            if old is None:
                _insert(db, r)
                added += 1
            elif r["updated"] > old["updated"]:
                db.execute(f"update entries set {','.join(f + '=?' for f in FIELDS)} where id=?", [r[f] for f in FIELDS] + [old["id"]])
                replaced += 1
            else:
                kept += 1
        for k, v in doc["settings"].items():
            db.execute("insert into settings values(?,?) on conflict(k) do nothing", (k, v))
    return added, replaced, kept
