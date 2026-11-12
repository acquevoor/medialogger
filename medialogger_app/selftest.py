"""Offline asserts, fed trimmed real replies. Run with `python medialogger.py --selftest` or `nix flake check`."""
import io
import json
import os
import tempfile
import urllib.error
import zipfile
from email.message import Message
from pathlib import Path

from . import imports, sources as src, store as st

ROW = {"kind": "anime", "site": "anilist", "key": "1", "title": "Cowboy Bebop", "status": "done"}


def rejected(fn, needle):
    try:
        fn()
    except ValueError as e:
        assert needle in str(e), f"{needle!r} not in {e}"
        return
    raise AssertionError(f"no error containing {needle!r}")


def test_store(tmp):
    db = st.open_db("main")
    st.apply(db, st.diff(db, [{**ROW, "rating": 8.5, "notes": "from site"}]))
    e = st.find(db, "anilist", "1")
    assert e["rating"] == 8.5 and e["edited"] == 0 and e["created"] > 0

    # a re-pull fills and changes, but never blanks
    d = st.diff(db, [{**ROW, "rating": 9, "notes": ""}])
    assert len(d["update"]) == 1 and d["update"][0][1] == {"rating": 9}
    st.apply(db, d)
    assert st.find(db, "anilist", "1")["notes"] == "from site"
    assert st.diff(db, [{**ROW, "rating": 9}])["same"] == 1

    # a row saved by hand wins over every later pull
    st.save_entry(db, {**st.find(db, "anilist", "1"), "rating": 3})
    d = st.diff(db, [{**ROW, "rating": 10}])
    assert d["kept"] == 1 and not d["update"]
    assert st.find(db, "anilist", "1")["edited"] == 1

    # rows sharing a key fold together; a bad cover host costs only the cover
    d = st.diff(db, [{**ROW, "key": "2", "rating": 5}, {**ROW, "key": "2", "finished": "2020-01-02"}, {**ROW, "key": "3", "cover": "https://evil.example/x.jpg"}])
    assert len(d["new"]) == 2 and d["new"][0]["rating"] == 5 and d["new"][0]["finished"] == "2020-01-02" and d["new"][1]["cover"] == ""
    assert st.summary(d) == "+2 new, 0 updated, 0 kept local, 0 unchanged"
    st.apply(db, d)
    assert len(st.entries(db)) == 3 and len(st.entries(db, "game")) == 0
    st.delete_entry(db, st.find(db, "anilist", "3")["id"])

    # export -> import round trip, name collision gets a suffix, source untouched
    st.set_setting(db, "anilist", "me")
    path = tmp / "out" / "me.json"
    st.export_profile(db, "main", path)
    assert os.name == "nt" or path.stat().st_mode & 0o777 == 0o600  # Windows has no such modes
    name = st.import_new(st.read_profile(path) | {"name": "main"})
    assert name == "main (2)" and st.list_profiles() == ["main", "main (2)"]
    copy = st.open_db(name)
    assert st.entries(copy) == st.entries(db)
    assert st.get_setting(copy, "anilist") == "me"

    # merge: newest updated wins per key, an older file changes nothing
    doc = st.read_profile(path)
    doc["entries"][0] = {**doc["entries"][0], "rating": 1, "updated": doc["entries"][0]["updated"] + 100}
    doc["entries"].append(st.clean({**ROW, "key": "9", "updated": 5}))
    assert st.merge_profile(db, doc) == (1, 1, 1)
    assert st.find(db, "anilist", doc["entries"][0]["key"])["rating"] == 1
    assert st.merge_profile(db, doc) == (0, 0, 3)

    # a traversal in an imported name stays inside profiles/
    assert "/" not in st.safe_name("../../etc/passwd") and st.safe_name("...") == "profile"
    assert st.safe_name("con") == "_con" and st.safe_name("Lpt1.txt") == "_Lpt1.txt" and st.safe_name("console") == "console"
    assert st.db_path("../x").parent == st.data_dir() / "profiles"
    db.close()
    copy.close()


def test_rejects(tmp):
    good = st.clean(ROW)
    p = tmp / "bad.json"

    def check(doc, needle):
        p.write_text(doc if isinstance(doc, str) else json.dumps(doc))
        rejected(lambda: st.read_profile(p), needle)

    base = {"format": st.FORMAT, "version": 1, "name": "x", "entries": [good]}
    check("{nope", "not valid JSON")
    check({"format": "other"}, "not a medialogger profile")
    check({**base, "version": 2}, "reads up to 1")
    check({**base, "entries": "x"}, "missing")
    for field, value, needle in (("kind", "book", "bad kind"), ("status", "ok", "bad status"), ("rating", 11, "rating"), ("rating", True, "rating"),
                                 ("started", "last year", "YYYY-MM-DD"), ("year", "1998", "year"), ("title", "", "required"), ("title", "x" * 2000, "title"),
                                 ("url", "file:///etc/passwd", "https"), ("url", "javascript:alert(1)", "https"),
                                 ("cover", "https://192.168.1.1/x.png", "cover host"), ("cover", "http://t.vndb.org/x.jpg", "cover host"),
                                 ("cover", "https://t.vndb.org.evil.example/x.jpg", "cover host")):
        check({**base, "entries": [good, {**good, field: value}]}, "entry 2")
        rejected(lambda: st.clean({**good, field: value}), needle)
    p.write_text('{"format":"medialogger-profile","version":1,"name":"x","entries":[{"rating":NaN}]}')
    rejected(lambda: st.read_profile(p), "entry 1")
    # rejection happens before any write
    before = st.list_profiles()
    p.write_text(json.dumps({**base, "name": "hacked", "entries": [good, {"kind": "bad"}]}))
    rejected(lambda: st.import_new(st.read_profile(p)), "entry 2")
    assert st.list_profiles() == before
    # unknown fields are dropped
    p.write_text(json.dumps({**base, "settings": {"anilist": "a", "evil": "b"}, "entries": [{**good, "evil": 1}]}))
    doc = st.read_profile(p)
    assert doc["settings"] == {"anilist": "a"} and "evil" not in doc["entries"][0]


def test_mapping():
    assert src.vndb_status({"Finished", "Voted"}, 80) == "done" and src.vndb_status({"Voted"}, 80) == "done"
    assert src.vndb_status({"Dropped", "Voted"}, 30) == "dropped" and src.vndb_status(set(), None) == "planned"
    assert src.vndb_status({"Blacklist"}, None) is None and src.vndb_status({"Wishlist"}, None) == "planned"
    assert st.make_key("film", "Blade Runner: 2049!", 2017) == st.make_key("film", "blade runner  2049", 2017)
    assert st.make_key("music", "OK Computer", creator="Radiohead") == "radiohead|ok computer"


def stub(replies):
    """fetch answering in order, or by URL when given {url part: reply} (game search asks two sites at once);
    bodies are JSON-encoded unless already bytes."""
    calls = []

    def fake(url, data=None, headers=None, **kw):
        calls.append((url, data))
        r = next(v for k, v in replies.items() if k in url) if isinstance(replies, dict) else replies.pop(0)
        return r if isinstance(r, bytes) else json.dumps(r).encode()
    src.fetch = src.slow_fetch = fake
    return calls


def test_sources():
    real = src.fetch, src.slow_fetch
    try:
        stub([{"data": {"Page": {"media": [{"id": 154587, "siteUrl": "https://anilist.co/anime/154587", "title": {"romaji": "Sousou no Frieren", "english": "Frieren: Beyond Journey's End"},
                                            "startDate": {"year": 2023}, "coverImage": {"large": "https://s4.anilist.co/x.jpg"}, "format": "TV", "episodes": 28, "studios": {"nodes": [{"name": "MADHOUSE"}]}, "staff": {"nodes": []}}]}}}])
        r = src.search("anime", "frieren")[0]
        assert r["key"] == "154587" and r["creator"] == "MADHOUSE" and r["year"] == 2023 and r["title"].startswith("Frieren")
        assert r["detail"] == "TV · 28 eps" and r["cover"] == "https://s4.anilist.co/x.jpg"

        media = {"id": 5, "siteUrl": "https://anilist.co/anime/5", "title": {"romaji": "R", "english": None}, "startDate": {"year": None}, "coverImage": {"medium": ""}}
        stub([{"data": {"MediaListCollection": {"lists": [{"entries": [{"status": "REPEATING", "progress": 3, "notes": None, "score": 0, "startedAt": {"year": 2020, "month": 1, "day": 2},
                                                                        "completedAt": {"year": 2021, "month": None, "day": None}, "media": media}]}]}}},
              {"data": {"MediaListCollection": {"lists": []}}}])
        r = src.anilist_pull("me")
        assert r == [{"kind": "anime", "site": "anilist", "key": "5", "title": "R", "year": None, "url": "https://anilist.co/anime/5", "cover": "", "status": "active",
                      "rating": None, "progress": 3, "started": "2020-01-02", "finished": "", "notes": "", "nsfw": False}]

        vn = {"title": "Minna de Nyan Nyan", "developers": [{"name": "Yamikumo"}], "image": {"url": "https://t.vndb.org/cv/59/77859.jpg"}, "released": "2003-10-17"}
        calls = stub([{"yorhel": {"id": "u2"}},
                      {"more": True, "results": [{"id": "v1", "vote": 30, "started": None, "finished": None, "notes": None, "labels": [{"label": "Dropped"}, {"label": "Voted"}], "vn": vn}]},
                      {"more": False, "results": [{"id": "v2", "vote": None, "started": None, "finished": None, "notes": "x", "labels": [{"label": "Blacklist"}], "vn": vn}]},
                      {"more": False, "results": [{"id": "r9", "vns": [{"id": "v1"}, {"id": "v77"}]}]}])
        r = src.vndb_pull("yorhel")
        assert len(r) == 1 and r[0]["rating"] == 3 and r[0]["status"] == "dropped" and r[0]["year"] == 2003
        assert json.loads(calls[2][1])["page"] == 2
        assert r[0]["nsfw"] is True and '"minage", ">=", 18' in calls[3][1].decode()  # an 18+ release makes the VN adult
        stub([{"nobody": None}])
        try:
            src.vndb_pull("nobody")
            raise AssertionError("missing user accepted")
        except RuntimeError as e:
            assert "no user" in str(e)

        stub([{"series": [{"book": {"image": {"filename": "32dZ.jpg"}}, "id": 2405, "title": "Overlord", "title_orig": "オーバーロード", "romaji_orig": "Overlord", "c_start_date": 20120730},
                          {"book": None, "id": 7, "title": None, "romaji_orig": None, "title_orig": "原題", "c_start_date": 99999999}]}])
        r = src.search("novel", "overlord")
        assert r[0]["url"] == "https://ranobedb.org/series/2405" and r[0]["cover"] == "https://images.ranobedb.org/32dZ.jpg" and r[0]["year"] == 2012
        assert r[1]["title"] == "原題" and r[1]["year"] is None and r[1]["cover"] == ""

        stub([{"release-groups": [{"id": "abc", "title": "OK Computer", "first-release-date": "1997-06-16", "artist-credit": [{"name": "Radiohead", "joinphrase": ""}]}]}])
        r = src.search("music", "ok computer")[0]
        assert r["creator"] == "Radiohead" and r["key"] == "radiohead|ok computer" and r["url"].startswith("https://rateyourmusic.com/search")
        assert st.cover_ok(r["cover"])

        def bind(f, ord_, title=None, **v):
            """One grouped Wikidata row: d date, lb Letterboxd slug, st Steam id, wpt Wikipedia title, by creator, plats."""
            b = {"f": {"value": f"http://www.wikidata.org/entity/{f}"}, "ord": {"value": str(ord_)}}
            return b | ({"title": {"value": title}} if title else {}) | {k: {"value": x} for k, x in v.items()}
        wiki = {"query": {"normalized": [{"from": "dune (2021 film)", "to": "Dune (2021 film)"}], "redirects": [{"from": "Dune (2021 film)", "to": "Dune (film)"}],
                          "pages": [{"title": "Dune (film)", "thumbnail": {"source": "https://upload.wikimedia.org/dune.jpg"}}]}}
        calls = stub({"query.wikidata.org": {"results": {"bindings": [
                          bind("Q1", 0, "Dune", d="2021-09-03T00:00:00Z", lb="dune-2021", wpt="dune (2021 film)", by="Denis Villeneuve"),
                          bind("Q2", 1)]}},  # no English or multilingual name: dropped
                      "wikipedia.org": wiki})
        r = src.search("film", 'du"ne')
        assert len(r) == 1 and r[0]["year"] == 2021 and r[0]["url"] == "https://letterboxd.com/film/dune-2021/" and r[0]["key"] == "dune|2021"
        assert r[0]["cover"] == "https://upload.wikimedia.org/dune.jpg" and r[0]["creator"] == "Denis Villeneuve"
        assert '\\"' in calls[0][0] or "%5C%22" in calls[0][0]  # the search term can't break out of the SPARQL string
        assert "pilicense=any" in calls[1][0]

        # same title, told apart by year, platforms and Steam id; a Steam hit Wikidata also has appears once
        stub({"store.steampowered.com": {"items": [{"name": "Resident Evil 4", "logo": "https://shared.fastly.steamstatic.com/store_item_assets/steam/apps/2050650/capsule_sm_120.jpg"},
                                                   {"name": "Indie Thing", "logo": "https://shared.fastly.steamstatic.com/store_item_assets/steam/apps/42/capsule_sm_120.jpg"}]},
              "query.wikidata.org": {"results": {"bindings": [
                  bind("Q1", 0, "Resident Evil 4", d="2005-01-11T00:00:00Z", st="254700", by="Capcom", plats="Nintendo GameCube, Microsoft Windows, Wii, PlayStation 2, Zeebo"),
                  bind("Q2", 1, "Resident Evil 4", d="2023-03-24T00:00:00Z", st="2050650", by="Capcom"),
                  bind("Q3", 2, "Resident Evil 4: Wii Edition", d="2007-05-31T00:00:00Z", wpt="RE4 Wii")]}},
              "wikipedia.org": {"query": {"pages": [{"title": "RE4 Wii", "thumbnail": {"source": "https://upload.wikimedia.org/wii.jpg"}}]}},
              "api.steampowered.com": {"response": {"store_items": [{"appid": 2050650, "content_descriptorids": [2, 5]}, {"appid": 42, "content_descriptorids": [1, 5]}]}}})
        r = src.search("game", "resident evil 4")
        assert [g["key"] for g in r] == ["steam:254700", "steam:2050650", "wikidata:Q3", "steam:42"], [g["key"] for g in r]
        assert [g["year"] for g in r[:3]] == [2005, 2023, 2007] and r[0]["detail"] == "GameCube, Windows, Wii +2"
        assert r[0]["cover"].endswith("/254700/library_600x900.jpg") and r[2]["cover"] == "https://upload.wikimedia.org/wii.jpg" and r[3]["detail"] == "Steam"
        assert r[3]["cover"].endswith("/apps/42/capsule_231x87.jpg")
        assert all(st.cover_ok(g["cover"]) for g in r) and "backloggd.com/search/games/Resident" in r[0]["url"]
        assert [g["nsfw"] for g in r] == [False, False, False, True], "violence (2) and mature (5) aren't adult; nudity (1) is"

        # Wikidata down: Steam's hits still come back; both down: an error
        def wd_down(url, data=None, headers=None, **kw):
            if "wikidata" in url:
                raise urllib.error.URLError("busy")
            return json.dumps({"items": [{"name": "Hades", "logo": "https://shared.fastly.steamstatic.com/store_item_assets/steam/apps/1145360/c.jpg"}]}).encode()
        src.fetch = src.slow_fetch = wd_down
        assert [g["key"] for g in src.search("game", "hades")] == ["steam:1145360"]

        def all_down(url, data=None, headers=None, **kw):
            raise urllib.error.URLError("offline")
        src.fetch = src.slow_fetch = all_down
        try:
            src.search("game", "hades")
            raise AssertionError("no error with every source down")
        except RuntimeError as e:
            assert "Wikidata" in str(e)
        assert src.wiki_covers(["x"]) == {}  # pictures never fail a search

        stub([{"release-groups": [{"id": "abc", "title": "Live in Rainbows", "first-release-date": "2008", "primary-type": "Album", "secondary-types": ["Live"],
                                   "artist-credit": [{"name": "Radiohead"}]}]}])
        assert src.search("music", "in rainbows")[0]["detail"] == "Album · Live"
    finally:
        src.fetch, src.slow_fetch = real

    feed = b"""<rss xmlns:letterboxd="https://letterboxd.com" xmlns:tmdb="https://themoviedb.org"><channel>
      <item><title>All of a Sudden, 2026 - 4.5</title><link>https://letterboxd.com/dave/film/all-of-a-sudden-2026/</link>
        <letterboxd:watchedDate>2026-11-03</letterboxd:watchedDate><letterboxd:filmTitle>All of a Sudden</letterboxd:filmTitle><letterboxd:filmYear>2026</letterboxd:filmYear>
        <letterboxd:memberRating>4.5</letterboxd:memberRating><description><![CDATA[<p><img src="https://a.ltrbxd.com/p.jpg?v=1"/></p>]]></description></item>
      <item><title>A list</title><link>https://letterboxd.com/dave/list/x/</link></item></channel></rss>"""
    r = src.parse_rss(feed)
    assert len(r) == 1 and r[0]["rating"] == 9 and r[0]["finished"] == "2026-11-03" and r[0]["url"] == "https://letterboxd.com/film/all-of-a-sudden-2026/" and st.cover_ok(r[0]["cover"])
    rejected(lambda: src.fetch_cover("https://evil.example/x.jpg"), "not allowed")
    rejected(lambda: src.fetch_cover("http://t.vndb.org/x.jpg"), "not allowed")
    h = src._CheckedRedirect()
    try:
        h.redirect_request(None, None, 302, "", Message(), "http://169.254.169.254/latest")
        raise AssertionError("redirect off the allowlist followed")
    except urllib.error.URLError:
        pass

    # one 429 waits and retries; a second raises
    waits, opens = [], []

    def urlopen(req, timeout):
        opens.append(req.full_url)
        hdr = Message()
        hdr["Retry-After"] = "999"
        if len(opens) == 1:
            raise urllib.error.HTTPError(req.full_url, 429, "slow down", hdr, io.BytesIO(b""))
        return io.BytesIO(b"ok")
    real_open, src.urllib.request.urlopen = src.urllib.request.urlopen, urlopen
    try:
        assert src.fetch("https://x.test/", sleep=waits.append) == b"ok" and waits == [60]
    finally:
        src.urllib.request.urlopen = real_open


def test_adult(tmp):
    """Adult covers: which signals count, that no source can clear another's flag, and that your choice wins."""
    assert st.adult({"title": "Hentai Ninja"}) and st.adult({"title": "エロマンガ先生"}) and st.adult({"title": "NTR Paradise"})
    assert not any(st.adult({"title": t}) for t in ("Essex", "Central Park", "Resident Evil 4", "Sussex", "Eroica"))
    assert st.adult({"title": "Fine", "nsfw": 1}) and not st.adult({"title": "Hentai Ninja", "nsfw": -1})
    rejected(lambda: st.clean({**ROW, "nsfw": 2}), "nsfw")
    assert st.clean({**ROW, "nsfw": True})["nsfw"] == 1 and st.clean(ROW)["nsfw"] == 0

    db = st.open_db("adult")
    film = {"kind": "film", "site": "letterboxd", "key": "x|2000", "title": "X", "status": "done"}
    st.apply(db, st.diff(db, [{**film, "nsfw": True}]))
    d = st.diff(db, [{**film, "nsfw": False, "rating": 6}])  # an RSS pull has no adult flags: it can't clear one
    assert d["update"] == [(st.find(db, "letterboxd", "x|2000")["id"], {"rating": 6})]
    st.apply(db, d)
    assert st.find(db, "letterboxd", "x|2000")["nsfw"] == 1
    st.save_entry(db, {**st.find(db, "letterboxd", "x|2000"), "nsfw": -1})  # you said it's fine
    assert st.diff(db, [{**film, "nsfw": True}])["kept"] == 1 and not st.adult(st.find(db, "letterboxd", "x|2000"))

    # round trip, and a profile from before this field still imports
    path = tmp / "adult.json"
    st.export_profile(db, "adult", path)
    assert st.read_profile(path)["entries"][0]["nsfw"] == -1
    doc = json.loads(path.read_text())
    for e in doc["entries"]:
        del e["nsfw"]
    path.write_text(json.dumps(doc))
    assert st.read_profile(path)["entries"][0]["nsfw"] == 0
    db.close()

    # a profile database from before the column existed gets it, and keeps its rows
    import sqlite3
    old = st.db_path("old")
    con = sqlite3.connect(old)
    con.executescript(st.SCHEMA.replace("nsfw integer not null default 0, ", ""))
    con.execute("insert into entries(kind,site,key,title,status,created,updated) values('film','letterboxd','y|1','Y','done',1,1)")
    con.commit()
    con.close()
    db = st.open_db("old")
    assert st.find(db, "letterboxd", "y|1")["nsfw"] == 0
    st.save_entry(db, {**st.find(db, "letterboxd", "y|1"), "nsfw": 1})
    assert st.find(db, "letterboxd", "y|1")["nsfw"] == 1
    db.close()

    real = src.fetch, src.slow_fetch
    try:
        stub([{"data": {"Page": {"media": [{"id": i, "siteUrl": "", "title": {"romaji": t, "english": None}, "startDate": {}, "coverImage": {}, "isAdult": a,
                                            "genres": g, "studios": {"nodes": []}, "staff": {"nodes": []}}
                                           for i, t, a, g in ((1, "Bible Black", True, ["Hentai"]), (2, "Prison School", False, ["Comedy", "Ecchi"]), (3, "Frieren", False, ["Fantasy"]))]}}}])
        assert [r["nsfw"] for r in src.search("anime", "x")] == [True, True, False]
        stub([{"results": [{"id": "v1", "title": "A", "image": {"url": "https://t.vndb.org/a.jpg", "sexual": 1.4}, "released": "2001"},
                           {"id": "v2", "title": "B", "image": {"url": "https://t.vndb.org/b.jpg", "sexual": 0}, "released": "2002"},
                           {"id": "v3", "title": "C", "image": None, "released": "2003"}]},
              {"more": False, "results": [{"id": "r1", "vns": [{"id": "v3"}]}]}])
        assert [r["nsfw"] for r in src.search("vn", "x")] == [True, False, True]
        stub([{"series": [{"book": {"image": {"filename": "a.jpg", "nsfw": True}}, "id": 1, "title": "A", "c_start_date": 2001},
                          {"book": {"image": {"filename": "b.jpg", "nsfw": False}}, "id": 2, "title": "B", "c_start_date": 2002}]}])
        assert [r["nsfw"] for r in src.search("novel", "x")] == [True, False]
        calls = stub({"query.wikidata.org": {"results": {"bindings": [
            {"f": {"value": "http://www.wikidata.org/entity/Q1"}, "ord": {"value": "0"}, "title": {"value": "Emmanuelle"}, "x": {"value": "1"}},
            {"f": {"value": "http://www.wikidata.org/entity/Q2"}, "ord": {"value": "1"}, "title": {"value": "Heat"}}]}}, "wikipedia.org": {"query": {}}})
        assert [r["nsfw"] for r in src.search("film", "x")] == [True, False]
        assert "wd%3AQ185529" in calls[0][0] or "wd:Q185529" in calls[0][0]  # pornographic film is among the adult items

        def steam_down(url, data=None, headers=None, **kw):
            raise urllib.error.URLError("offline")
        src.fetch = steam_down
        assert src.steam_adult(["1"]) == set() and src.steam_adult([]) == set()
    finally:
        src.fetch, src.slow_fetch = real


def test_imports(tmp):
    z = tmp / "lb.zip"
    with zipfile.ZipFile(z, "w") as f:
        f.writestr("watchlist.csv", "Date,Name,Year,Letterboxd URI\n2024-01-01,Dune,2021,https://boxd.it/a\n2024-01-01,Heat,1995,https://boxd.it/b\n")
        f.writestr("ratings.csv", "Date,Name,Year,Letterboxd URI,Rating\n2024-02-01,Dune,2021,https://boxd.it/a,4.5\n")
        f.writestr("diary.csv", "﻿Date,Name,Year,Letterboxd URI,Rating,Rewatch,Tags,Watched Date\n"
                                "2024-05-01,Dune,2021,https://boxd.it/d2,,Yes,,2024-05-01\n2024-02-01,Dune,2021,https://boxd.it/d1,4.5,,,2024-02-01\n")
    db = st.open_db("imports")
    d = st.diff(db, imports.parse_letterboxd(z))
    assert len(d["new"]) == 2
    dune = next(r for r in d["new"] if r["title"] == "Dune")
    assert dune["status"] == "done" and dune["rating"] == 9 and dune["finished"] == "2024-05-01"
    assert next(r for r in d["new"] if r["title"] == "Heat")["status"] == "planned"
    st.apply(db, d)
    assert st.diff(db, imports.parse_letterboxd(z))["same"] == 2  # importing twice changes nothing

    rym = tmp / "rym.csv"
    rym.write_text('RYM Album, First Name,Last Name,First Name localized, Last Name localized,Title,Release_Date,Rating,Ownership,Purchase Date,Media Type,Review\n'
                   '"1","","Radiohead","","","OK Computer","1997","10","n","","","great &amp; sad"\n'
                   '"2","The","Beatles","","","Abbey Road","1969","0","o","","",""\n')
    a, b = imports.parse_rym(rym)
    assert a["rating"] == 10 and a["status"] == "done" and a["key"] == "radiohead|ok computer" and a["notes"] == "great & sad" and a["year"] == 1997
    assert b["creator"] == "The Beatles" and b["rating"] is None and b["status"] == "planned"
    assert not st.diff(db, [a, b])["kept"]

    rym.write_text("Artist,Album\nA,B\n")
    rejected(lambda: imports.parse_rym(rym), "found: artist, album")
    with zipfile.ZipFile(z, "w") as f:
        f.writestr("diary.csv", "Film,When\nDune,2024\n")
    rejected(lambda: imports.parse_letterboxd(z), "found: film, when")
    with zipfile.ZipFile(z, "w") as f:
        f.writestr("other.txt", "x")
    rejected(lambda: imports.parse_letterboxd(z), "found: other.txt")
    rym.write_text("not a zip")
    rejected(lambda: imports.parse_letterboxd(rym), "not a Letterboxd")
    db.close()


def bencode(v):
    if isinstance(v, int):
        return b"i%de" % v
    if isinstance(v, (bytes, str)):
        v = v.encode() if isinstance(v, str) else v
        return b"%d:%s" % (len(v), v)
    if isinstance(v, list):
        return b"l" + b"".join(map(bencode, v)) + b"e"
    return b"d" + b"".join(bencode(k) + bencode(x) for k, x in sorted(v.items())) + b"e"


def test_scan(tmp):
    """Names in, titles out; Steam's files, qBittorrent's resume data and plain folders read correctly."""
    from . import scan
    for name, kind, want in (("[SubsPlease] Sousou no Frieren - 01 (1080p) [ABCD1234].mkv", "anime", ("Sousou no Frieren", None, "")),
                             ("[Judas] Mob Psycho 100 S3 - 05.mkv", "anime", ("Mob Psycho 100", None, "")),
                             ("Dune.Part.Two.2024.2160p.WEB-DL.DDP5.1.x265-GROUP", "film", ("Dune Part Two", 2024, "")),
                             ("Blade_Runner_2049_2017_1080p", "film", ("Blade Runner 2049", 2017, "")),
                             ("2001 A Space Odyssey (1968)", "film", ("2001 A Space Odyssey", 1968, "")),
                             ("Radiohead - OK Computer (1997) [FLAC]", "music", ("OK Computer", 1997, "Radiohead")),
                             ("Hades.v1.38290-GOG", "game", ("Hades", None, "")), ("Persona 5 Royal-FLT", "game", ("Persona 5 Royal", None, "")),
                             ("Berserk v01-41 (Dark Horse) [Digital]", "manga", ("Berserk", None, "")),
                             ("[deanzel] Steins;Gate [BD 1080p Hi10p Dual Audio FLAC]", "anime", ("Steins;Gate", None, "")),
                             ("[AV1ARY] New Game! (BD 1080p AV1 Opus)", "anime", ("New Game!", None, ""))):
        assert scan.clean_title(name, kind) == want, (name, scan.clean_title(name, kind))
    assert scan.guess_kind("[Grp] Show - 01", [("a.mkv", 9)]) == "anime" and scan.guess_kind("Heat 1995", [("heat.mkv", 9)]) == "film"
    assert scan.guess_kind("Show", [(f"e{i}.mkv", 9) for i in range(5)]) == "anime" and scan.guess_kind("x", [("readme.txt", 1), ("a.flac", 99)]) == "music"
    assert scan.guess_kind("x", [("notes.doc", 1)]) is None

    # best match: same title and year beats the first hit; no exact title is a guess; a dated mismatch isn't exact
    res = [{"title": "Dune", "year": 1984}, {"title": "Dune", "year": 2021}, {"title": "Dune: Part Two", "year": 2024}]
    assert scan.best_match(res, "Dune", 2021) == (res[1], True) and scan.best_match(res, "dune", None) == (res[0], True)
    assert scan.best_match(res, "Dune", 1999) == (res[0], False) and scan.best_match(res, "Arrival") == (res[0], False) and scan.best_match([], "x") == (None, False)
    ani = [{"title": "The Elusive Samurai", "year": 2024, "alt": ["Nige Jouzu no Wakagimi", "逃げ上手の若君"]}, {"title": "Umamusume: Pretty Derby", "alt": []}]
    assert scan.best_match(ani, "Nige Jouzu no Wakagimi") == (ani[0], True) and scan.best_match(ani, "Uma Musume Pretty Derby") == (ani[1], True)

    # Steam: two libraries, a tool skipped, playtime and recency decide the status, uninstalled games only if Steam says game
    root, lib2 = tmp / "steam", tmp / "games"
    (root / "steamapps").mkdir(parents=True)
    (lib2 / "steamapps").mkdir(parents=True)
    (root / "steamapps/libraryfolders.vdf").write_text(f'"libraryfolders"\n{{ "0" {{ "path" "{root}" }} "1" {{ "path" "{lib2}" }} }}')
    for lib, appid, name in ((root, 1145360, "Hades"), (root, 1493710, "Proton Experimental"), (lib2, 4000, "Garry's Mod"), (lib2, 5, "Unplayed")):
        (lib / f"steamapps/appmanifest_{appid}.acf").write_text(f'"AppState" {{ "appid" "{appid}" "name" "{name}" }}')
    conf = root / "userdata/1/config"
    conf.mkdir(parents=True)
    now = 1_800_000_000
    (conf / "localconfig.vdf").write_text('"UserLocalConfigStore" { "Software" { "Valve" { "Steam" { "apps" { '
                                          f'"1145360" {{ "Playtime" "600" "LastPlayed" "{now - 86400}" }} "4000" {{ "Playtime" "90" "LastPlayed" "{now - 90 * 86400}" }} '
                                          '"777" { "Playtime" "30" } "778" { "Playtime" "50" } } } } } }')
    asked = []

    def lookup(ids):
        asked.append(set(ids))
        return {"777": {"name": "Played Then Removed", "type": 0, "content_descriptorids": [1, 5]}, "778": {"name": "A Soundtrack", "type": 6}}
    got = {r["match"]["key"]: r["match"] for r in scan.scan_steam([root], lookup, now)}
    assert set(got) == {"steam:1145360", "steam:4000", "steam:5", "steam:777"}, got.keys()
    assert [got[k]["status"] for k in ("steam:1145360", "steam:4000", "steam:5", "steam:777")] == ["active", "done", "planned", "done"]
    assert got["steam:1145360"]["detail"] == "10 h played" and got["steam:5"]["detail"] == "installed" and got["steam:777"]["nsfw"] and not got["steam:4000"]["nsfw"]
    assert asked == [{"1145360", "4000", "5", "777", "778"}]

    # qBittorrent: names and files from the .torrent, the category decides when set, a broken file is skipped
    bt = tmp / "BT_backup"
    bt.mkdir()
    for h, info, extra in (("a", {"name": "[SubsPlease] Frieren - 01 (1080p).mkv", "length": 9}, {"completed_time": 5}),
                           ("b", {"name": "[SubsPlease] Frieren - 02 (1080p).mkv", "length": 9}, {}),
                           ("c", {"name": "Radiohead - OK Computer (1997) [FLAC]", "files": [{"path": ["01.flac"], "length": 9}, {"path": ["cover.jpg"], "length": 1}]}, {}),
                           ("d", {"name": "Some Thing", "files": [{"path": ["x.mkv"], "length": 9}]}, {"qBt-category": "Movies"})):
        (bt / f"{h}.fastresume").write_bytes(bencode({"qBt-name": "", **extra}))
        (bt / f"{h}.torrent").write_bytes(bencode({"info": info}))
    (bt / "e.fastresume").write_bytes(b"d4:name")
    rows = {r["title"]: r for r in scan.scan_qbittorrent([bt])}
    assert set(rows) == {"Frieren", "OK Computer", "Some Thing"}, rows.keys()
    assert rows["Frieren"]["kind"] == "anime" and rows["Frieren"]["count"] == 2 and rows["Frieren"]["done"]
    assert rows["OK Computer"]["kind"] == "music" and rows["OK Computer"]["creator"] == "Radiohead" and rows["Some Thing"]["kind"] == "film"
    rejected(lambda: scan.bdecode(b"5:ab"), "bad string")

    # a folder: one work per top-level entry; hidden and media-less entries skipped
    media = tmp / "media"
    (media / "Heat (1995)").mkdir(parents=True)
    (media / "Heat (1995)/heat.mkv").write_bytes(b"x" * 9)
    (media / ".hidden").mkdir()
    (media / ".hidden/a.mkv").write_bytes(b"x")
    (media / "Docs").mkdir()
    (media / "Docs/a.doc").write_bytes(b"x")
    (media / "Overlord v01-16.epub").write_bytes(b"x")
    assert [(r["title"], r["kind"], r["year"]) for r in scan.scan_folder(media)] == [("Heat", "film", 1995), ("Overlord", "novel", None)]


def test_launcher(tmp):
    """The launcher: a clone fast-forwards and keeps local edits; without .git a download is installed only if it compiles.
    Importing it must never install anything: this runs on a Python without Qt."""
    import importlib.util
    import shutil
    import subprocess
    spec = importlib.util.spec_from_file_location("launcher", Path(__file__).parent.parent / "medialogger.py")
    launcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(launcher)

    def git(folder, *a):
        subprocess.run(["git", "-C", str(folder), "-c", "user.name=t", "-c", "user.email=t@t", *a], check=True, capture_output=True)
    up, me, plain = tmp / "up", tmp / "me", tmp / "plain"
    up.mkdir(), plain.mkdir()
    if shutil.which("git"):
        git(up, "init", "-q")
        (up / "f").write_text("1")
        git(up, "add", "f")
        git(up, "commit", "-qm", "1")
        git(tmp, "clone", "-q", str(up), str(me))
        (up / "f").write_text("2")
        git(up, "commit", "-qam", "2")
        launcher.update(str(me))
        assert (me / "f").read_text() == "2"
        (up / "f").write_text("3")
        git(up, "commit", "-qam", "3")
        (me / "f").write_text("mine")  # would be overwritten, so git refuses and the app starts anyway
        launcher.update(str(me))
        assert (me / "f").read_text() == "mine"

    def fake_zip(**files):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            for name, body in files.items():
                z.writestr(name.replace("__", "/"), body)
        return buf.getvalue()
    sha1, sha2, sha3, code = "a" * 40, "b" * 40, "c" * 40, str(tmp / "code")
    served = {}
    launcher.fetch = lambda url, accept="", timeout=30: served["sha"].encode() if "api.github" in url else served["zip"]
    served.update(sha=sha1, zip=fake_zip(**{"r/medialogger_app/ui.py": "main = 1", "r/evil.py": "x", "r/medialogger_app/sub/x.py": "x"}))
    launcher.update(str(plain), code)
    assert launcher.current(code) == str(Path(code, sha1)) and sorted(os.listdir(Path(code, sha1, "medialogger_app"))) == ["ui.py"]
    served["zip"] = None  # same commit: nothing is downloaded (install would raise, and current stays)
    launcher.update(str(plain), code)
    assert launcher.current(code) == str(Path(code, sha1))
    served.update(sha=sha2, zip=fake_zip(**{"r/medialogger_app/ui.py": "def broken(:"}))
    launcher.update(str(plain), code)
    assert launcher.current(code) == str(Path(code, sha1)) and sha2 not in os.listdir(code), "a file that doesn't compile changes nothing"
    served.update(sha=sha2, zip=fake_zip(**{"r/medialogger_app/ui.py": "main = 2"}))
    launcher.update(str(plain), code)
    served.update(sha=sha3, zip=fake_zip(**{"r/medialogger_app/ui.py": "main = 3"}))
    launcher.update(str(plain), code)
    assert launcher.current(code) == str(Path(code, sha3)) and sorted(os.listdir(code)) == sorted([sha2, sha3, "current"])
    served["sha"] = "<html>"
    launcher.update(str(plain), code)
    assert launcher.current(code) == str(Path(code, sha3))
    nogit = tmp / "nogit"
    (nogit / ".git").mkdir(parents=True)  # a clone, but no git on PATH: it updates by download like a ZIP
    which, launcher.shutil.which = launcher.shutil.which, lambda _: None
    served.update(sha=sha1, zip=fake_zip(**{"r/medialogger_app/ui.py": "main = 4"}))
    try:
        launcher.update(str(nogit), code)
    finally:
        launcher.shutil.which = which
    assert launcher.current(code) == str(Path(code, sha1))


def selftest():
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        os.environ["XDG_DATA_HOME"], os.environ["XDG_CACHE_HOME"] = t, t  # never touch the real log
        test_store(tmp)
        test_rejects(tmp)
        test_mapping()
        test_sources()
        test_imports(tmp)
        test_adult(tmp)
        test_scan(tmp)
        test_launcher(tmp)
    print("selftest ok")
