# medialogger

A local log for games, anime, manga, music, films, light novels and visual novels. Your log lives on your machine; nothing is ever written back to a site, and no tokens are needed.

## What each site does

| Tab | Site | Search | Your history |
|---|---|---|---|
| Anime, Manga | AniList | AniList | Pull by username (the list must be public) |
| Visual novels | VNDB | VNDB | Pull by username |
| Light novels | RanobeDB | RanobeDB | None, RanobeDB's user API needs a login |
| Games | Backloggd | Steam and Wikidata | None, Backloggd's `robots.txt` disallows user pages |
| Films | Letterboxd | Wikidata | Export zip, plus RSS for recent watches |
| Music | RateYourMusic | MusicBrainz | CSV export |

The app never requests RateYourMusic or Letterboxd pages. Both block automated clients and RYM disallows all crawling, so they only appear as links that open in your browser. Letterboxd's public RSS is the one exception.

## Using it

Search in a tab and press Enter, pick a result, and fill in the log form. **Add by hand…** covers anything search misses. Ratings are 0–10 everywhere; imported ratings are converted (Letterboxd stars × 2, VNDB votes ÷ 10).

**Sync from sites…** pulls usernames and reads export files. It always shows a preview first ("+12 new, 3 updated, 5 kept local"). Sync never deletes, never blanks a field, and never overwrites an entry you saved by hand.

- **Letterboxd export:** Settings → Data → Export your data. Import the zip as is.
- **RateYourMusic export:** your profile page → Export your data. Save it as a `.csv`.

## Profiles

Each profile is a separate log, picked from the toolbar. **Export…** writes one portable JSON file holding all entries, ratings, notes and site usernames, readable only by you. **Import…** reads one back as a new profile, or merges it into the current one with the newest version of each entry winning. Neither choice removes anything.

Imported files are checked field by field and rejected as a whole if anything is wrong. Covers only load from the catalog sites, so a profile from someone else can't make the app contact other hosts.

## Files

- Profiles: `~/.local/share/medialogger/profiles/<name>.db` (SQLite)
- Covers: `~/.cache/medialogger/covers/`, safe to delete

## Development

`python medialogger.py --selftest` or `nix flake check`. The selftest is offline and needs only Python.
