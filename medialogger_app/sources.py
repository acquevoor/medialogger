"""Catalog search and username pulls. Workers call these, so nothing here touches Qt or the database.

Hard rule: no request ever goes to a rateyourmusic.com page or a Letterboxd HTML page. Both answer 403 and RYM's
robots.txt disallows everything, so they only appear as links for your browser. Letterboxd's public RSS is the one
exception. Backloggd and RanobeDB have no pull: robots.txt and a login stand in the way.
"""
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor

from .store import COVER_HOSTS, cover_ok, make_key, norm

UA = "medialogger/0.1 ( https://github.com/acquevoor )"  # MusicBrainz requires a descriptive agent
MAX_COVER = 8 << 20
SLOW = threading.Lock()  # MusicBrainz asks for 1 req/s and Wikidata for serial requests
_last = 0.0


def fetch(url, data=None, headers=None, sleep=time.sleep):
    """-> bytes. A 429 waits for Retry-After (at most 60 s) and tries once more."""
    req = urllib.request.Request(url, data=data, headers={"User-Agent": UA, **(headers or {})})
    for attempt in (1, 2):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code != 429 or attempt == 2:
                raise
            ra = str(e.headers.get("Retry-After") or "")
            sleep(min(int(ra) if ra.isdigit() else 10, 60))


def slow_fetch(url, headers=None):
    global _last
    with SLOW:
        time.sleep(max(0, _last + 1 - time.monotonic()))
        try:
            return fetch(url, headers=headers)
        finally:
            _last = time.monotonic()


def _json(label, body_fn):
    """Run a request; failures become RuntimeError("<label>: …") with the site's own message when it sends one."""
    try:
        return json.loads(body_fn())
    except urllib.error.HTTPError as e:
        raw = e.read()[:2000]
        try:
            doc = json.loads(raw)
            msg = doc["errors"][0]["message"] if "errors" in doc else doc.get("message", "")
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            msg = raw.decode(errors="replace")[:120]
        raise RuntimeError(f"{label}: {msg or f'HTTP {e.code}'}") from None
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        raise RuntimeError(f"{label}: {e}") from None


def _post(label, url, doc):
    return _json(label, lambda: fetch(url, json.dumps(doc).encode(), {"Content-Type": "application/json"}))


def _year(s):
    m = re.match(r"\d{4}", str(s or ""))
    return int(m[0]) if m and 1000 <= int(m[0]) <= 3000 else None


def _link(fmt, title):
    return fmt.format(urllib.parse.quote(title, safe=""))


BACKLOGGD_SEARCH = "https://backloggd.com/search/games/{}/"
LETTERBOXD_SEARCH = "https://letterboxd.com/search/films/{}/"
RYM_SEARCH = "https://rateyourmusic.com/search?searchterm={}&searchtype=l"


def rym_link(artist, title):
    return _link(RYM_SEARCH, f"{artist} {title}".strip())


# ---------- AniList ----------

ANILIST = "https://graphql.anilist.co"
MEDIA = "id siteUrl format episodes chapters isAdult genres title{romaji english} startDate{year} coverImage{large}"
SEARCH_Q = ("query($s:String,$t:MediaType){Page(perPage:20){media(search:$s,type:$t){%s title{native} synonyms"
            " studios(isMain:true){nodes{name}} staff(perPage:1){nodes{name{full}}}}}}") % MEDIA
LIST_Q = ("query($u:String,$t:MediaType){MediaListCollection(userName:$u,type:$t){lists{entries{status progress notes"
          " score(format:POINT_10_DECIMAL) startedAt{year month day} completedAt{year month day} media{%s}}}}}") % MEDIA
ANILIST_STATUS = {"CURRENT": "active", "REPEATING": "active", "PLANNING": "planned", "COMPLETED": "done", "PAUSED": "paused", "DROPPED": "dropped"}


def _anilist_row(kind, m):
    return {"kind": kind, "site": "anilist", "key": str(m["id"]), "title": m["title"]["english"] or m["title"]["romaji"],
            "year": (m.get("startDate") or {}).get("year"), "url": m.get("siteUrl") or "", "cover": (m.get("coverImage") or {}).get("large") or "",
            "nsfw": bool(m.get("isAdult") or {"Hentai", "Ecchi"} & set(m.get("genres") or []))}


def _anilist_detail(m):
    """TV · 26 eps, Movie, Manga · 380 ch: what tells a show from its film or spin-off."""
    fmt = {"TV": "TV", "TV_SHORT": "TV short", "MOVIE": "Movie", "OVA": "OVA", "ONA": "ONA", "ONE_SHOT": "One-shot"}.get(m.get("format"), (m.get("format") or "").title())
    n = f"{m['episodes']} eps" if m.get("episodes") else f"{m['chapters']} ch" if m.get("chapters") else ""
    return " · ".join(x for x in (fmt, n) if x)


def anilist_search(kind, q):
    doc = _post("AniList", ANILIST, {"query": SEARCH_Q, "variables": {"s": q, "t": kind.upper()}})
    out = []
    for m in doc["data"]["Page"]["media"]:
        names = [n["name"] for n in m["studios"]["nodes"]] if kind == "anime" else [n["name"]["full"] for n in m["staff"]["nodes"]]
        t = m["title"]
        out.append({**_anilist_row(kind, m), "creator": ", ".join(names[:2]), "detail": _anilist_detail(m),
                    "alt": [x for x in (t.get("romaji"), t.get("english"), t.get("native"), *(m.get("synonyms") or [])) if x]})
    return out


def _fuzzy(d):
    d = d or {}
    return f"{d['year']:04}-{d['month']:02}-{d['day']:02}" if d.get("year") and d.get("month") and d.get("day") else ""


def anilist_pull(user):
    out = []
    for kind in ("anime", "manga"):
        doc = _post("AniList", ANILIST, {"query": LIST_Q, "variables": {"u": user, "t": kind.upper()}})
        for lst in doc["data"]["MediaListCollection"]["lists"]:
            for e in lst["entries"]:
                out.append({**_anilist_row(kind, e["media"]), "status": ANILIST_STATUS.get(e["status"], "planned"), "rating": e["score"] or None,
                            "progress": e["progress"] or None, "started": _fuzzy(e["startedAt"]), "finished": _fuzzy(e["completedAt"]), "notes": e["notes"] or ""})
    return out


# ---------- VNDB ----------

VNDB = "https://api.vndb.org/kana"
VN_FIELDS = "title,image.url,image.sexual,released,developers.name"
VNDB_LABELS = (("Playing", "active"), ("Finished", "done"), ("Stalled", "paused"), ("Dropped", "dropped"), ("Wishlist", "planned"))


def _vn_row(vid, vn):
    return {"kind": "vn", "site": "vndb", "key": vid, "title": vn["title"], "creator": ", ".join(d["name"] for d in vn.get("developers", [])[:2]),
            "year": _year(vn.get("released")), "url": f"https://vndb.org/{vid}", "cover": (vn.get("image") or {}).get("url") or "",
            "nsfw": ((vn.get("image") or {}).get("sexual") or 0) >= 1}  # VNDB's vote on the cover: 1 suggestive, 2 explicit


def vndb_adult(rows):
    """Mark VNs with any 18+ release: the work itself is adult even when its cover is safe. Queried 100 at a time."""
    ids = [r["key"] for r in rows if not r["nsfw"]]
    adult = set()
    for i in range(0, len(ids), 100):
        page = 1
        while True:
            doc = _post("VNDB", f"{VNDB}/release", {"filters": ["and", ["minage", ">=", 18], ["vn", "=", ["or", *(["id", "=", v] for v in ids[i:i + 100])]]],
                                                    "fields": "vns.id", "results": 100, "page": page})
            adult |= {v["id"] for rel in doc["results"] for v in rel["vns"]}
            if not doc["more"]:
                break
            page += 1
    for r in rows:
        r["nsfw"] = r["nsfw"] or r["key"] in adult
    return rows


def vndb_search(q):
    doc = _post("VNDB", f"{VNDB}/vn", {"filters": ["search", "=", q], "fields": VN_FIELDS + ",alttitle,aliases", "results": 20})
    return vndb_adult([{**_vn_row(v["id"], v), "alt": [x for x in (v.get("alttitle"), *(v.get("aliases") or [])) if x]} for v in doc["results"]])


def vndb_status(labels, vote):
    """-> status, or None for a blacklisted VN, which isn't a log entry."""
    if "Blacklist" in labels:
        return None
    return next((s for name, s in VNDB_LABELS if name in labels), "done" if vote else "planned")


def vndb_pull(user):
    url = f"{VNDB}/user?" + urllib.parse.urlencode({"q": user})
    found = _json("VNDB", lambda: fetch(url)).get(user)
    if not found:
        raise RuntimeError(f"VNDB: no user named {user}")
    out, page = [], 1
    while True:
        doc = _post("VNDB", f"{VNDB}/ulist", {"user": found["id"], "results": 100, "page": page,
                                              "fields": "vote,started,finished,notes,labels.label," + ",".join(f"vn.{f}" for f in VN_FIELDS.split(","))})
        for e in doc["results"]:
            status = vndb_status({x["label"] for x in e["labels"]}, e["vote"])
            if status:
                out.append({**_vn_row(e["id"], e["vn"]), "status": status, "rating": e["vote"] / 10 if e["vote"] else None,
                            "started": e["started"] or "", "finished": e["finished"] or "", "notes": e["notes"] or ""})
        if not doc["more"]:
            return vndb_adult(out)
        page += 1


# ---------- Wikidata (films, games) ----------

# One row per item, in the search's own relevance order (apiOrdinal), with what tells same-titled things apart:
# year, creator (director / developer) and platforms. en,mul labels: Hades has no plain en one.
SPARQL = """SELECT ?f ?ord (SAMPLE(?lab) AS ?title) (MIN(?date) AS ?d) (SAMPLE(?slug) AS ?lb) (SAMPLE(?steam) AS ?st) (SAMPLE(?wp) AS ?wpt)
  (SAMPLE(?img) AS ?pic) (GROUP_CONCAT(DISTINCT ?byL; separator=", ") AS ?by) (GROUP_CONCAT(DISTINCT ?platL; separator=", ") AS ?plats) (MAX(?adult) AS ?x) WHERE {
  SERVICE wikibase:mwapi { bd:serviceParam wikibase:api "EntitySearch"; wikibase:endpoint "www.wikidata.org";
    mwapi:search %s; mwapi:language "en"; wikibase:limit 30 . ?f wikibase:apiOutputItem mwapi:item. ?ord wikibase:apiOrdinal true. }
  ?f %s.
  OPTIONAL { ?f rdfs:label ?lab FILTER(LANG(?lab) IN ("en", "mul")) }
  OPTIONAL { ?f wdt:P577 ?date } OPTIONAL { ?f wdt:P6127 ?slug } OPTIONAL { ?f wdt:P1733 ?steam } OPTIONAL { ?f wdt:P18 ?img }
  OPTIONAL { ?wpa schema:about ?f; schema:isPartOf <https://en.wikipedia.org/>; schema:name ?wp }
  OPTIONAL { ?f wdt:%s ?b. ?b rdfs:label ?byL FILTER(LANG(?byL) = "en") }
  OPTIONAL { ?f wdt:P400 ?p. ?p rdfs:label ?platL FILTER(LANG(?platL) = "en") }
  OPTIONAL { ?f wdt:P136|wdt:P31|wdt:P852|wdt:P1657 ?a. VALUES ?a { %s } BIND(1 AS ?adult) }
} GROUP BY ?f ?ord ORDER BY ?ord"""
# genre, class, ESRB or MPA rating that makes a work adult: eroge, pornographic / erotic video game, ESRB Adults Only 18+;
# pornographic / erotic / pink film, hentai, ecchi; NC-17, X
ADULT_ITEMS = "wd:Q1046788 wd:Q7230210 wd:Q3362070 wd:Q14864332 wd:Q185529 wd:Q599558 wd:Q1194365 wd:Q172067 wd:Q219559 wd:Q18665349 wd:Q23817743"


def wikidata(q, cls, by):
    """-> [{id, title, year, slug, steam, wp, img, creator, platforms}] in relevance order. by: P57 director, P178 developer."""
    url = "https://query.wikidata.org/sparql?" + urllib.parse.urlencode({"query": SPARQL % (json.dumps(q), cls, by, ADULT_ITEMS)})
    doc = _json("Wikidata", lambda: slow_fetch(url, {"Accept": "application/sparql-results+json"}))
    out = []
    for b in doc["results"]["bindings"]:
        v = {k: x["value"] for k, x in b.items()}
        if not v.get("title"):
            continue  # no English or multilingual name
        out.append({"id": v["f"].rsplit("/", 1)[1], "title": v["title"], "year": _year(v.get("d")), "slug": v.get("lb", ""), "steam": v.get("st", ""),
                    "wp": v.get("wpt", ""), "img": v.get("pic", ""), "creator": v.get("by", ""), "platforms": v.get("plats", ""), "nsfw": v.get("x") == "1"})
    return out


def wiki_covers(titles):
    """English Wikipedia article -> its lead image (posters, box art), for up to 50 titles in one request.
    pilicense=any: covers are non-free images, which the API leaves out by default."""
    titles = list(dict.fromkeys(t for t in titles if t))[:50]
    if not titles:
        return {}
    url = "https://en.wikipedia.org/w/api.php?" + urllib.parse.urlencode({"action": "query", "prop": "pageimages", "piprop": "thumbnail", "pithumbsize": 300,
                                                                        "pilicense": "any", "redirects": 1, "format": "json", "formatversion": 2, "titles": "|".join(titles)})
    try:
        doc = _json("Wikipedia", lambda: fetch(url)).get("query", {})
    except RuntimeError:
        return {}  # pictures only: a search shouldn't fail over them
    hop = {x["from"]: x["to"] for x in doc.get("normalized", []) + doc.get("redirects", [])}
    found = {p["title"]: p["thumbnail"]["source"] for p in doc.get("pages", []) if p.get("thumbnail")}
    out = {}
    for t in titles:
        final = hop.get(hop.get(t, t), hop.get(t, t))
        if final in found:
            out[t] = found[final]
    return out


def _commons(img):
    # P18 values are http Special:FilePath links
    return img.replace("http://", "https://", 1) + "?width=300" if img else ""


SHORT_PLATFORMS = {"Microsoft Windows": "Windows", "Xbox Series X and Series S": "Xbox Series", "Nintendo Switch": "Switch", "Nintendo Switch 2": "Switch 2",
                   "Nintendo GameCube": "GameCube", "Nintendo 3DS": "3DS", "Nintendo DS": "DS", "PlayStation Portable": "PSP", "PlayStation Vita": "Vita"}


def _platforms(text, shown=3):
    names = [SHORT_PLATFORMS.get(p, p) for p in text.split(", ") if p]
    return ", ".join(names[:shown]) + (f" +{len(names) - shown}" if len(names) > shown else "")


def film_search(q):
    items = wikidata(q, "wdt:P31/wdt:P279* wd:Q11424", "P57")
    covers = wiki_covers(it["wp"] for it in items)
    out = []
    for it in items:
        url = f"https://letterboxd.com/film/{urllib.parse.quote(it['slug'])}/" if it["slug"] else _link(LETTERBOXD_SEARCH, it["title"])
        out.append({"kind": "film", "site": "letterboxd", "key": make_key("film", it["title"], it["year"]), "title": it["title"], "year": it["year"],
                    "creator": it["creator"], "url": url, "cover": covers.get(it["wp"]) or _commons(it["img"]), "nsfw": it["nsfw"]})
    return out


# ---------- games ----------

STEAM_COVER = "https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/{}/library_600x900.jpg"  # portrait; the bigger games have one


def steam_games(q):
    """Steam's store search, games only (category 998): no DLC, soundtracks or demos. -> [(appid, name, image)]
    The image is the wide capsule on the app's own path: newer apps keep their art under a hash, so header.jpg 404s."""
    url = "https://store.steampowered.com/search/results/?" + urllib.parse.urlencode({"term": q, "category1": 998, "json": 1, "count": 12})
    out = []
    for g in _json("Steam", lambda: fetch(url)).get("items", []):
        m = re.search(r"/apps/(\d+)/", g.get("logo", ""))
        if m:
            out.append((m[1], g["name"], g["logo"].replace("capsule_sm_120", "capsule_231x87")))
    return out


def game_search(q):
    """Wikidata first: it knows the year, platforms and developer that tell same-titled games apart (Resident Evil 4 from
    2005 and 2023). Steam fills in what Wikidata lacks, mostly small PC games. A Steam id is the key whenever there is
    one, so the same game is the same entry whichever list it came from. Links go to a Backloggd search: its slugs
    aren't derivable (Wikidata's IGDB slug for Hades is hades--1)."""
    with ThreadPoolExecutor(1) as side:  # both at once: Wikidata alone takes a couple of seconds
        steam_job = side.submit(steam_games, q)
        try:
            items, wd_err = wikidata(q, "wdt:P31 wd:Q7889", "P178"), None
        except RuntimeError as e:
            items, wd_err = [], e  # Wikidata's endpoint is often busy; Steam's hits still stand
        try:
            steam = steam_job.result()
        except RuntimeError:
            steam = []
    if wd_err and not steam:
        raise wd_err
    covers = wiki_covers(it["wp"] for it in items if not it["steam"])
    out, seen = [], set()
    for it in items:
        key = f"steam:{it['steam']}" if it["steam"] else f"wikidata:{it['id']}"
        if key in seen:
            continue
        seen.add(key)
        out.append({"kind": "game", "site": "backloggd", "key": key, "title": it["title"], "year": it["year"], "creator": it["creator"],
                    "detail": _platforms(it["platforms"]), "url": _link(BACKLOGGD_SEARCH, it["title"]),
                    "cover": STEAM_COVER.format(it["steam"]) if it["steam"] else covers.get(it["wp"]) or _commons(it["img"]), "nsfw": it["nsfw"]})
    for appid, name, image in steam:
        if f"steam:{appid}" not in seen:
            seen.add(f"steam:{appid}")
            out.append({"kind": "game", "site": "backloggd", "key": f"steam:{appid}", "title": name, "detail": "Steam",
                        "url": _link(BACKLOGGD_SEARCH, name), "cover": image, "nsfw": False})
    adult = steam_adult([r["key"][6:] for r in out if r["key"].startswith("steam:")])
    for r in out:
        r["nsfw"] = r["nsfw"] or r["key"][6:] in adult and r["key"].startswith("steam:")
    return out


STEAM_SEXUAL = {1, 3, 4}  # Steam's content descriptors: some nudity or sexual content, adult only sexual content, frequent nudity


def steam_items(appids):
    """appid -> Steam's store item (name, type 0 = game, content descriptors), 100 per request, keyless.
    Flags and names only: a failed lookup gives nothing rather than an error."""
    ids, out = [int(a) for a in dict.fromkeys(appids)], {}
    for i in range(0, len(ids), 100):
        req = {"ids": [{"appid": a} for a in ids[i:i + 100]], "context": {"language": "english", "country_code": "US"},
               "data_request": {"include_basic_info": True}}
        url = "https://api.steampowered.com/IStoreBrowseService/GetItems/v1/?" + urllib.parse.urlencode({"input_json": json.dumps(req)})
        try:
            items = _json("Steam", lambda: fetch(url)).get("response", {}).get("store_items", [])
        except RuntimeError:
            continue
        out |= {str(it["appid"]): it for it in items if it.get("appid")}
    return out


def steam_adult(appids, items=None):
    """-> the appids Steam itself marks for nudity or sexual content."""
    items = steam_items(appids) if items is None else items
    return {a for a, it in items.items() if STEAM_SEXUAL & set(it.get("content_descriptorids") or [])}


# ---------- music ----------

def music_search(q):
    term = re.sub(r'([+\-&|!(){}\[\]^"~*?:\\/])', r"\\\1", q)  # Lucene syntax
    url = "https://musicbrainz.org/ws/2/release-group?" + urllib.parse.urlencode({"query": term, "fmt": "json", "limit": 25})
    out = []
    for g in _json("MusicBrainz", lambda: slow_fetch(url))["release-groups"]:
        artist = "".join(a["name"] + a.get("joinphrase", "") for a in g.get("artist-credit", []))
        year = _year(g.get("first-release-date"))
        kinds = " · ".join(filter(None, [g.get("primary-type"), *g.get("secondary-types", [])]))  # Album · Live, Single, EP
        out.append({"kind": "music", "site": "rym", "key": make_key("music", g["title"], creator=artist), "title": g["title"], "creator": artist,
                    "year": year, "detail": kinds, "url": rym_link(artist, g["title"]), "cover": f"https://coverartarchive.org/release-group/{g['id']}/front-250"})
    return out


# ---------- RanobeDB ----------

def novel_search(q):
    url = "https://ranobedb.org/api/v0/series?" + urllib.parse.urlencode({"q": q, "limit": 20})
    out = []
    for s in _json("RanobeDB", lambda: fetch(url))["series"]:
        image = (s.get("book") or {}).get("image") or {}
        name = image.get("filename") or ""
        out.append({"kind": "novel", "site": "ranobedb", "key": str(s["id"]), "title": s.get("title") or s.get("romaji_orig") or s["title_orig"],
                    "year": _year(s.get("c_start_date")), "url": f"https://ranobedb.org/series/{s['id']}",
                    "cover": f"https://images.ranobedb.org/{name}" if name else "", "nsfw": bool(image.get("nsfw")),
                    "alt": [x for x in (s.get("romaji"), s.get("romaji_orig"), s.get("title_orig")) if x]})
    return out


def search(kind, q):
    """-> rows ready for the Log dialog: kind, site, key, title and whatever else the site knows."""
    if kind in ("anime", "manga"):
        return anilist_search(kind, q)
    return {"game": game_search, "music": music_search, "film": film_search, "novel": novel_search, "vn": vndb_search}[kind](q)


def search_many(kinds, q):
    """Every kind's site at once -> (rows, {kind: error}). One site down doesn't lose the others' results."""
    with ThreadPoolExecutor(len(kinds)) as ex:
        jobs = {k: ex.submit(search, k, q) for k in kinds}
    rows, errors = [], {}
    for k, job in jobs.items():
        try:
            rows += job.result()
        except Exception as e:
            errors[k] = e
    if errors and not rows:
        raise RuntimeError("\n".join(f"{k}: {e}" for k, e in errors.items()))
    return rows, errors


# ---------- Letterboxd RSS ----------

LB = "{https://letterboxd.com}"


def parse_rss(xml):
    """Diary rows from a Letterboxd feed; list posts in the feed have no filmTitle and are skipped."""
    out = []
    for item in ET.fromstring(xml).iter("item"):
        title = item.findtext(LB + "filmTitle")
        if not title:
            continue
        year = _year(item.findtext(LB + "filmYear"))
        rating = item.findtext(LB + "memberRating")
        film = re.search(r"/film/([^/]+)", item.findtext("link") or "")
        poster = re.search(r'<img src="([^"]+)"', item.findtext("description") or "")
        out.append({"kind": "film", "site": "letterboxd", "key": make_key("film", title, year), "title": title, "year": year, "status": "done",
                    "url": f"https://letterboxd.com/film/{film[1]}/" if film else "", "cover": poster[1] if poster else "",
                    "rating": float(rating) * 2 if rating else None, "finished": item.findtext(LB + "watchedDate") or ""})
    return out


def letterboxd_pull(user):
    if not re.fullmatch(r"\w+", user):
        raise RuntimeError("Letterboxd: usernames are letters, digits and underscores")
    try:
        return parse_rss(fetch(f"https://letterboxd.com/{user}/rss/"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Letterboxd: HTTP {e.code} for {user}") from None
    except (urllib.error.URLError, TimeoutError, ET.ParseError) as e:
        raise RuntimeError(f"Letterboxd: {e}") from None


PULLS = {"anilist": anilist_pull, "vndb": vndb_pull, "letterboxd": letterboxd_pull}


# ---------- covers ----------

class _CheckedRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not cover_ok(newurl):  # coverartarchive.org and Commons redirect; the target must be on the list too
            raise urllib.error.URLError(f"cover redirected off the allowed hosts: {newurl[:80]}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_cover(url):
    """-> image bytes, only from COVER_HOSTS."""
    if not cover_ok(url):
        raise ValueError(f"cover host not allowed, only {', '.join(COVER_HOSTS)}")
    opener = urllib.request.build_opener(_CheckedRedirect)
    with opener.open(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=30) as r:
        data = r.read(MAX_COVER + 1)
    if len(data) > MAX_COVER:
        raise ValueError("cover is too large")
    return data
