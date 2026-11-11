"""Find what you already have on disk: your Steam library, qBittorrent's torrents, or any folder.

Everything here reads local files only; names reach a site only when you press Find matches (search per row).
Steam is exact: app ids and playtime. Torrents and folders are guesses, from file types and a cleaned-up name.
"""
import os
import re
import sys
import time
from pathlib import Path

from . import sources
from .store import norm

VIDEO = {".mkv", ".mp4", ".avi", ".m4v", ".webm", ".mov", ".wmv", ".ts", ".m2ts", ".flv"}
AUDIO = {".flac", ".mp3", ".m4a", ".ogg", ".opus", ".wav", ".ape", ".aac", ".wv", ".alac", ".dsf"}
BOOK = {".epub", ".mobi", ".azw", ".azw3", ".pdf", ".txt"}
COMIC = {".cbz", ".cbr", ".cb7"}
GAME = {".exe", ".iso", ".nsp", ".xci", ".3ds", ".cia", ".gba", ".nds", ".rvz", ".wbfs", ".chd", ".cue", ".apk", ".x86_64", ".msi", ".pkg"}
CATEGORY_KINDS = (("anime", "anime"), ("movie", "film"), ("film", "film"), ("music", "music"), ("game", "game"), ("manga", "manga"),
                  ("comic", "manga"), ("novel", "novel"), ("book", "novel"), ("vn", "vn"), ("visual", "vn"))
MAX_FILES = 3000  # per folder entry: enough to tell what it is, without walking a whole disk

# release names: everything from the first of these on is technical, not title
TECH = re.compile(r"[\s._(\[-](2160p|1080p|720p|576p|480p|4k|uhd|bd|bluray|blu-ray|bdrip|brrip|bdremux|remux|web-?dl|webrip|web|hdtv|dvdrip|dvd|"
                  r"x26[45]|h\.?26[45]|hevc|avc|xvid|aac\d?|flac|mp3|320|v0|10.?bit|8.?bit|hdr|dual.audio|multi|repack|proper|"
                  r"s\d{1,2}(e\d+)?|season.\d+|complete|batch|vol\.?.?\d+(-\d+)?|v\d+(-\d+)?|fitgirl|dodi|gog|elamigos|codex|plaza|skidrow|"
                  r"cpy|empress|tenoke|razor1911|rune|flt|repack|portable|setup)\b.*", re.I)
EPISODE = re.compile(r"\s-\s(e|ep)?\d{1,4}(v\d)?$|\s(e|ep)\d{1,4}(v\d)?$", re.I)  # "Title - 01", "Title E12"; never a bare number: Mob Psycho 100
YEAR = re.compile(r"(?<=[(\[ ._])(?:19|20)\d\d(?=[)\] ._]|$)")  # lookarounds: "2049_2017" holds two years


def clean_title(name, kind=None):
    """A release or folder name -> (title, year, creator). creator only for music: "Artist - Album"."""
    stem = Path(name).stem if Path(name).suffix.lower() in VIDEO | AUDIO | BOOK | COMIC | GAME | {".zip", ".rar", ".7z"} else name
    stem = re.sub(r"^(\s*[\[{][^\]}]*[\]}])+", "", stem)  # [Group] at the front
    if " " not in stem.strip() and re.search(r"[._]", stem):  # Some.Release.Name or some_release_name
        stem = re.sub(r"[._]", " ", stem)
    stem = f" {stem}"
    years = [m for m in YEAR.finditer(stem) if m.start() > 1]  # a name that starts with a year keeps it: 2001 A Space Odyssey
    year = int(years[-1].group()) if years else None
    if years:
        stem = stem[:years[-1].start()]  # the release year ends the title
    stem = TECH.sub("", stem)
    stem = re.sub(r"[\[{(][^\]})]*[\]})]", "", stem)  # leftover (tags) [hashes]
    stem = re.sub(r"[\[{(][^\]})]*$", "", stem)  # a bracket the technical cut split: "Steins;Gate [BD 1080p ...]"
    stem = re.sub(r"-[A-Za-z0-9]+$", "", stem) if re.search(r"\s\w+-[A-Z0-9]{2,}$", stem) else stem  # Game Name-GROUP
    stem = stem.strip(" -._([")
    if kind in ("anime", None):  # elsewhere a trailing number is title: Blade Runner 2049, Persona 5
        stem = EPISODE.sub("", stem)
    stem = re.sub(r"\s+", " ", stem).strip(" -._~([")
    creator = ""
    if kind == "music" and " - " in stem:
        creator, stem = (x.strip() for x in stem.split(" - ", 1))
    return stem or name, year, creator


def guess_kind(name, files):
    """From [(relative path, size)]: the kind whose files take the most space. Video splits into anime (fansub
    brackets, episode numbers, several episodes) and film. None when nothing looks like media."""
    weight = {}
    videos = 0
    for path, size in files:
        ext = Path(path).suffix.lower()
        kind = "video" if ext in VIDEO else "music" if ext in AUDIO else "novel" if ext in BOOK else "manga" if ext in COMIC else "game" if ext in GAME else None
        if kind:
            weight[kind] = weight.get(kind, 0) + max(size, 1)
            videos += kind == "video"
    if not weight:
        return None
    kind = max(weight, key=weight.get)
    if kind != "video":
        return kind
    anime = re.match(r"\s*\[", name) or re.search(r"\s-\s\d{2,4}\b|\bS\d{1,2}E\d+|\bE\d{2,}\b|\bbatch\b|\bseason\b", name, re.I) or videos >= 3
    return "anime" if anime else "film"


def _row(found, kind, files, extra=None):
    title, year, creator = clean_title(found, kind)
    return {"found": found, "kind": kind, "title": title, "year": year, "creator": creator, "count": 1, **(extra or {})}


def merge(rows):
    """One row per work: a show downloaded episode by episode is one entry, not twelve."""
    out = {}
    for r in rows:
        k = r["kind"], norm(r["title"]), r.get("year")
        if k in out:
            out[k]["count"] += 1
        else:
            out[k] = r
    return list(out.values())


def scan_folder(folder):
    """Each top-level file or folder is one work. Hidden entries and things that hold no media are skipped."""
    out = []
    with os.scandir(folder) as it:
        for e in sorted(it, key=lambda e: e.name.casefold()):
            if e.name.startswith("."):
                continue
            files = []
            if e.is_file(follow_symlinks=False):
                files = [(e.name, e.stat(follow_symlinks=False).st_size)]
            elif e.is_dir(follow_symlinks=False):
                for root, dirs, names in os.walk(e.path):
                    dirs[:] = [d for d in dirs if not d.startswith(".")]
                    for n in names:
                        try:
                            files.append((os.path.join(root, n), os.path.getsize(os.path.join(root, n))))
                        except OSError:
                            pass
                    if len(files) >= MAX_FILES:
                        break
            kind = guess_kind(e.name, files)
            if kind:
                out.append(_row(e.name, kind, files))
    return merge(out)


# ---------- qBittorrent ----------

def bdecode(data):
    """Bencode (torrent files) -> Python values; keys stay bytes. Iterative over the input, recursion only per nesting."""
    def go(i):
        c = data[i:i + 1]
        if c == b"i":
            j = data.index(b"e", i)
            return int(data[i + 1:j]), j + 1
        if c in (b"l", b"d"):
            i, items = i + 1, []
            while data[i:i + 1] != b"e":
                if i >= len(data):
                    raise ValueError("truncated")
                v, i = go(i)
                items.append(v)
            return (dict(zip(items[::2], items[1::2])) if c == b"d" else items), i + 1
        j = data.index(b":", i)
        n = int(data[i:j])
        if n < 0 or j + 1 + n > len(data):
            raise ValueError("bad string length")
        return data[j + 1:j + 1 + n], j + 1 + n
    return go(0)[0]


def _text(b):
    return b.decode("utf-8", "replace") if isinstance(b, bytes) else ""


def qbittorrent_dirs():
    home = Path.home()
    dirs = [home / ".local/share/qBittorrent/BT_backup", home / ".var/app/org.qbittorrent.qBittorrent/data/qBittorrent/BT_backup",
            home / "Library/Application Support/qBittorrent/BT_backup"]
    if os.environ.get("LOCALAPPDATA"):
        dirs.insert(0, Path(os.environ["LOCALAPPDATA"]) / "qBittorrent/BT_backup")
    return [d for d in dirs if d.is_dir()]


def scan_qbittorrent(dirs=None):
    """Every torrent qBittorrent knows, from its own resume files: name and file list from the .torrent, the
    category (an "Anime" or "Movies" category decides the kind) and whether it finished."""
    out = []
    for d in qbittorrent_dirs() if dirs is None else dirs:
        for resume in sorted(Path(d).glob("*.fastresume")):
            try:
                fr = bdecode(resume.read_bytes())
                tor = resume.with_suffix(".torrent")
                info = bdecode(tor.read_bytes()).get(b"info", {}) if tor.exists() else {}
            except (ValueError, OSError):
                continue
            name = _text(fr.get(b"qBt-name")) or _text(info.get(b"name")) or _text(fr.get(b"name"))
            if not name:
                continue
            if b"files" in info:
                files = [("/".join(_text(p) for p in f.get(b"path", [])), f.get(b"length", 0)) for f in info[b"files"]]
            else:
                files = [(name, info.get(b"length", 0))]
            category = _text(fr.get(b"qBt-category")).casefold()
            kind = next((k for word, k in CATEGORY_KINDS if word in category), None) or guess_kind(name, files)
            if kind:
                out.append(_row(name, kind, files, {"done": bool(fr.get(b"completed_time"))}))
    return merge(out)


# ---------- Steam ----------

def vdf(text):
    """Valve's KeyValues text (libraryfolders.vdf, appmanifest, localconfig) -> nested dicts, keys lowercased."""
    root, stack, key = {}, [], None
    node = root
    for m in re.finditer(r'"((?:[^"\\]|\\.)*)"|([{}])', text):
        s, brace = m.group(1), m.group(2)
        if brace == "{":
            child = {}
            node[(key or "").lower()] = child
            stack.append(node)
            node, key = child, None
        elif brace == "}":
            node, key = (stack.pop() if stack else root), None
        elif key is None:
            key = s
        else:
            node[key.lower()] = s.replace("\\\\", "\\")
            key = None
    return root


def steam_roots():
    home = Path.home()
    roots = [home / ".local/share/Steam", home / ".steam/steam", home / ".var/app/com.valvesoftware.Steam/.local/share/Steam",
             home / "Library/Application Support/Steam"]
    if sys.platform == "win32":
        roots = [Path(os.environ.get(v, "")) / "Steam" for v in ("ProgramFiles(x86)", "ProgramFiles")] + roots
    seen, out = set(), []
    for r in roots:
        if (r / "steamapps").is_dir() and r.resolve() not in seen:
            seen.add(r.resolve())
            out.append(r)
    return out


TOOLS = re.compile(r"^(Proton|Steam Linux Runtime|Steamworks|Steam Audio|SteamVR|Steam Controller)|Redistributable", re.I)


RECENT = 14 * 86400


def steam_status(minutes, last_played, now=None):
    """Played in the last two weeks: Playing. Played before: Played (done, in Backloggd's sense). Never: Planned."""
    if not minutes and not last_played:
        return "planned"
    return "active" if last_played and (now or time.time()) - last_played < RECENT else "done"


def scan_steam(roots=None, lookup=sources.steam_items, now=None):
    """Installed games (every library folder's app manifests) plus anything with playtime in localconfig.vdf, which
    covers games you played and uninstalled. Exact Steam ids, no guessing."""
    roots = steam_roots() if roots is None else [Path(r) for r in roots]
    names, played, last = {}, {}, {}
    for root in roots:
        libraries = {root}
        try:
            for lib in vdf((root / "steamapps/libraryfolders.vdf").read_text(encoding="utf-8", errors="replace")).get("libraryfolders", {}).values():
                if isinstance(lib, dict) and lib.get("path"):
                    libraries.add(Path(lib["path"]))
        except OSError:
            pass
        for lib in libraries:
            for man in (lib / "steamapps").glob("appmanifest_*.acf"):
                try:
                    app = vdf(man.read_text(encoding="utf-8", errors="replace")).get("appstate", {})
                except OSError:
                    continue
                if app.get("appid") and app.get("name"):
                    names[app["appid"]] = app["name"]
        for conf in root.glob("userdata/*/config/localconfig.vdf"):
            try:
                apps = vdf(conf.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                continue
            for k in ("userlocalconfigstore", "software", "valve", "steam", "apps"):
                apps = apps.get(k, {}) if isinstance(apps, dict) else {}
            for appid, a in apps.items():
                if isinstance(a, dict) and appid.isdigit():
                    minutes = int(a.get("playtime") or 0)
                    if minutes:
                        played[appid] = max(played.get(appid, 0), minutes)
                    if int(a.get("lastplayed") or 0) and not minutes:
                        played.setdefault(appid, 0)  # launched, under a minute: Steam keeps no playtime
                    last[appid] = max(last.get(appid, 0), int(a.get("lastplayed") or 0))
    names = {a: n for a, n in names.items() if not TOOLS.search(n)}
    items = lookup(set(names) | set(played)) if names or played else {}
    for appid in played:
        it = items.get(appid, {})
        if appid not in names and it.get("type") == 0 and it.get("name"):  # uninstalled: only real games, by Steam's own word
            names[appid] = it["name"]
    adult = sources.steam_adult(list(names), items)
    out = []
    for appid, name in sorted(names.items(), key=lambda x: x[1].casefold()):
        hours = played.get(appid, 0) / 60
        out.append({"found": name, "kind": "game", "title": name, "year": None, "creator": "", "steam": True,
                    "match": {"kind": "game", "site": "backloggd", "key": f"steam:{appid}", "title": name, "url": sources._link(sources.BACKLOGGD_SEARCH, name),
                              "cover": sources.STEAM_COVER.format(appid), "nsfw": appid in adult,
                              "status": steam_status(played.get(appid, 0), last.get(appid, 0), now),
                              "detail": f"{hours:.0f} h played" if hours >= 1 else "played" if appid in played else "installed"}})
    return out


# ---------- matching ----------

def _same(a, b):
    """Same title, spacing aside: Uma Musume = Umamusume."""
    return norm(a).replace(" ", "") == norm(b).replace(" ", "")


def best_match(results, title, year=None):
    """-> (row, exact). Exact: the name equals the result's title or any alternate one (a torrent's romaji against
    AniList's English title), and the year agrees when both have one. Otherwise the search's own first hit, marked
    as a guess for you to check."""
    same = [r for r in results if any(_same(title, t) for t in [r["title"], *r.get("alt", [])])]
    if year:
        dated = [r for r in same if r.get("year") == year]
        if dated:
            return dated[0], True
        same = [r for r in same if not r.get("year")] or ([] if any(r.get("year") for r in same) else same)
    if same:
        return same[0], True
    return (results[0], False) if results else (None, False)


def find_match(row):
    """Search the row's site for its title -> (match row or None, exact)."""
    return best_match(sources.search(row["kind"], row["title"]), row["title"], row.get("year"))
