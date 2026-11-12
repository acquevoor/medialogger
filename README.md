# medialogger

A local log for games, anime, manga, music, films, light novels and visual novels. Your log lives on your machine; nothing is ever written back to a site, and no tokens are needed.

PySide6 + Python stdlib.

## Which way to run it
| You're on | Do this |
|---|---|
| **Windows** | Install Python, then double-click `medialogger.py`. See [Windows](#windows-no-terminal-needed). |
| **NixOS / Nix** | `nix run github:acquevoor/medialogger` |
| **Linux** | `python3 medialogger.py` in this folder. See [Linux](#linux). |

`medialogger.py` is the only file you start.

## Install via flake

```nix
# flake.nix
inputs.medialogger.url = "github:acquevoor/medialogger";

# home-manager (or environment.systemPackages)
home.packages = [ inputs.medialogger.packages.${pkgs.stdenv.hostPlatform.system}.default ];
```

Includes a desktop entry.

## What each site does

| Kind | Site | Search | Your history |
|---|---|---|---|
| Anime, Manga | AniList | AniList | Pull by username (the list must be public) |
| Visual novels | VNDB | VNDB | Pull by username |
| Light novels | RanobeDB | RanobeDB | None, RanobeDB's user API needs a login |
| Games | Backloggd | Steam and Wikidata | None, Backloggd's `robots.txt` disallows user pages |
| Films | Letterboxd | Wikidata | Export zip, plus RSS for recent watches |
| Music | RateYourMusic | MusicBrainz | CSV export |

The app never requests RateYourMusic or Letterboxd pages. Both block automated clients and RYM disallows all crawling, so they only appear as links that open in your browser. Letterboxd's public RSS is the one exception.

## Using it

Everything you log is on one page. The colored buttons at the top choose which kinds show (right-click one to show only that kind, **All** brings them all back), and **List** / **Grid** switch between a sortable table and a wall of covers. Both choices are remembered.

To log something, pick a kind next to the search box, type, and press Enter (when only one kind is showing, the search follows it). Pick a result and fill in the log form. **Add by hand…** covers anything search misses. Ratings are 0–10 everywhere; imported ratings are converted (Letterboxd stars × 2, VNDB votes ÷ 10).

**Sync from sites…** pulls usernames and reads export files. It always shows a preview first ("+12 new, 3 updated, 5 kept local"). Sync never deletes, never blanks a field, and never overwrites an entry you saved by hand.

- **Letterboxd export:** Settings → Data → Export your data. Import the zip as is.
- **RateYourMusic export:** your profile page → Export your data. Save it as a `.csv`.

## Scan for media

**Scan for media…** finds what you already have:

- **Steam library:** every installed game in all your library folders, plus games you played and later uninstalled, read from Steam's own files. Played in the last two weeks comes in as Playing, played before as Played, never launched as Planned. Steam IDs are exact, so nothing needs matching.
- **qBittorrent:** every torrent qBittorrent knows, read from its saved state. A category like "Anime" or "Movies" decides the kind; otherwise the file types do (videos, audio, e-books, comics, games).
- **Choose a folder…:** each file or folder at the top of it is one work, guessed the same way.

Release names are cleaned up ("[SubsPlease] Sousou no Frieren - 01 (1080p)" becomes "Sousou no Frieren"), and episodes of the same show become one row. Fix any title or kind in the table, then press **Find matches** to look each one up on its site. Exact matches, including romaji names against English titles, are checked for you; closest guesses are left for you to check. **Add checked…** shows the same preview as a sync before anything is added.

Scanning only reads files on your computer. Titles are sent to the sites only when you press Find matches.

## Profiles

Each profile is a separate log, picked from the toolbar. **Export…** writes one portable JSON file holding all entries, ratings, notes and site usernames, readable only by you. **Import…** reads one back as a new profile, or merges it into the current one with the newest version of each entry winning. Neither choice removes anything.

Imported files are checked field by field and rejected as a whole if anything is wrong. Covers only load from the catalog sites, so a profile from someone else can't make the app contact other hosts.

## Files

- Profiles: `~/.local/share/medialogger/profiles/<name>.db` (SQLite). On Windows that's `C:\Users\<you>\.local\share\medialogger\profiles\`.
- Covers: `~/.cache/medialogger/covers/`, safe to delete.
- PySide6: `~/.local/share/medialogger/venv/`, made on the first start; safe to delete, the next start makes it again.

To move your log to another machine, use **Export…** and **Import…** rather than copying these files.

## Windows (no terminal needed)
1. Install Python from https://www.python.org/downloads/ . In the installer, tick **"Add python.exe to PATH"** and click *Install Now*.
2. On this GitHub page click the green **Code** button → **Download ZIP**, then right-click the ZIP → **Extract All**.
3. Double-click **`medialogger.py`** in the extracted folder. Keep the `medialogger_app` folder next to it.
   - The first start sets up PySide6 by itself (a black window shows progress for a minute or two). Later starts open right away.
   - If Windows asks which app should open the file, choose **Python**.
   - Leave the black window open while you use the app; closing it closes the app.

Updates are automatic, see [Updates](#updates).

## Linux
Any distro with Python 3.10 or newer:

1. `git clone https://github.com/acquevoor/medialogger` (or **Code** → **Download ZIP** and extract it).
2. `python3 medialogger/medialogger.py`

The first start makes the app's own Python environment in `~/.local/share/medialogger/venv/` and installs PySide6 there, so it needs no root and leaves your system Python alone. Later starts open right away.

- **Debian or Ubuntu:** if it says it couldn't create the environment, run `sudo apt install python3-venv` once.
- **No window on X11** ("could not load the Qt platform plugin xcb"): install `libxcb-cursor0` (Debian, Ubuntu) or `xcb-util-cursor` (Fedora, Arch).
- **NixOS:** use the flake instead; pip's Qt can't load there.

## Updates
Every copy updates itself when it starts, so there's nothing to re-download:
- A `git clone` runs `git pull`, on Windows and Linux alike. A clone on a PC without git installed (made with GitHub Desktop, say) updates by download like the ZIP.
- The ZIP and the Nix package download the latest `medialogger_app/` from this repository's `master` into `~/.local/share/medialogger/code/` and run it from there.

If you're offline, or an update is broken, the app starts with the version it has. This runs code from this repository on your machine each time it changes.

## Development

`python medialogger.py --selftest` or `nix flake check`. The selftest is offline and needs only Python.
