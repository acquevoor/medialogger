"""The sites' own export files: the only way to bring in Letterboxd and RYM history.

Columns follow the documented layouts, but neither was checked against a real export, so a missing column fails
loudly and names what the file does have. Rows come back in fold order for store.diff: later rows win field by field.
"""
import csv
import html
import io
import re
import zipfile

from .sources import _year, rym_link
from .store import make_key

MAX_MEMBER = 64 << 20


def _rows(text, required, name):
    """CSV text -> dicts with casefolded, trimmed headers (RYM's have stray spaces)."""
    reader = csv.reader(io.StringIO(text))
    header = [h.strip().casefold() for h in next(reader, [])]
    missing = [c for c in required if c not in header]
    if missing:
        raise ValueError(f"{name}: no column {', '.join(missing)}; found: {', '.join(header) or 'nothing'}")
    return [dict(zip(header, row)) for row in reader if any(row)]


def _number(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def parse_letterboxd(path):
    """Export zip -> rows. Order matters: the watchlist first so anything also watched ends up done, and the
    diary last, oldest to newest, so rewatches collapse into one row with the latest date."""
    try:
        zf = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as e:
        raise ValueError(f"not a Letterboxd export zip: {e}") from None
    with zf:
        names = {i.filename: i for i in zf.infolist() if i.file_size <= MAX_MEMBER}
        files = ("watchlist.csv", "watched.csv", "ratings.csv", "reviews.csv", "diary.csv")
        if not any(f in names for f in files):
            raise ValueError(f"no Letterboxd CSVs in this zip; found: {', '.join(sorted(names)[:10]) or 'nothing'}")
        out = []
        for name in files:
            if name not in names:
                continue
            rows = _rows(zf.read(name).decode("utf-8-sig"), ("name", "year"), name)
            if name == "diary.csv":
                rows.sort(key=lambda r: r.get("watched date") or r.get("date") or "")
            for r in rows:
                year, stars = _year(r["year"]), _number(r.get("rating"))
                out.append({"kind": "film", "site": "letterboxd", "key": make_key("film", r["name"], year), "title": r["name"], "year": year,
                            "status": "planned" if name == "watchlist.csv" else "done", "rating": stars * 2 if stars else None,
                            "url": r.get("letterboxd uri") or "", "finished": r.get("watched date") or "", "notes": r.get("review") or ""})
        return out


def _decode(raw):
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("cp1252", errors="replace")


def parse_rym(path):
    """RYM's CSV export -> rows. Unrated rows are owned or tagged albums, so they land as planned."""
    with open(path, "rb") as f:
        raw = f.read(MAX_MEMBER + 1)
    if len(raw) > MAX_MEMBER:
        raise ValueError("file is too large for a RYM export")
    out = []
    for r in _rows(_decode(raw), ("last name", "title", "rating"), "RYM export"):
        artist = html.unescape(" ".join(x for x in (r.get("first name", "").strip(), r["last name"].strip()) if x))
        title = html.unescape(r["title"].strip())
        if not title:
            continue
        rating = _number(r["rating"]) or None
        out.append({"kind": "music", "site": "rym", "key": make_key("music", title, creator=artist), "title": title, "creator": artist,
                    "year": _year(re.sub(r"\D", "", r.get("release_date", ""))), "status": "done" if rating else "planned", "rating": rating,
                    "url": rym_link(artist, title), "notes": html.unescape(r.get("review", "")).strip()})
    return out
