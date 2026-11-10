# medialogger

A local log for games, anime, manga, music, films, light novels and visual novels. Your log lives on your machine; nothing is ever written back to a site, and no tokens are needed.

PySide6 + Python stdlib.

## Which way to run it
| You're on | Do this |
|---|---|
| **Linux** (including Bazzite, SteamOS, Fedora Atomic) | Download the **AppImage** from Releases: nothing to install. See [Linux AppImage](#linux-appimage-no-install). |
| **Windows** | Install Python, then double-click `medialogger.py`. See [Windows](#windows-no-terminal-needed). |
| **NixOS / Nix** | `nix run github:acquevoor/medialogger` |
| **Linux with Python, from source** | `python3 medialogger.py` in this folder. It installs PySide6 with pip on first run. |

`medialogger.py` is the only file you start. The files in `appimage/` are build tools, not the app.

## Install via flake

```nix
# flake.nix
inputs.medialogger.url = "github:acquevoor/medialogger";

# home-manager (or environment.systemPackages)
home.packages = [ inputs.medialogger.packages.${pkgs.stdenv.hostPlatform.system}.default ];
```

Includes a desktop entry.

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

- Profiles: `~/.local/share/medialogger/profiles/<name>.db` (SQLite). On Windows that's `C:\Users\<you>\.local\share\medialogger\profiles\`.
- Covers: `~/.cache/medialogger/covers/`, safe to delete.

To move your log to another machine, use **Export…** and **Import…** rather than copying these files.

## Windows (no terminal needed)
1. Install Python from https://www.python.org/downloads/ . In the installer, tick **"Add python.exe to PATH"** and click *Install Now*.
2. On this GitHub page click the green **Code** button → **Download ZIP**, then right-click the ZIP → **Extract All**.
3. Double-click **`medialogger.py`** in the extracted folder. Keep the `medialogger_app` folder next to it.
   - The first start installs PySide6 by itself (a black window shows progress for a minute or two). Later starts open right away.
   - If Windows asks which app should open the file, choose **Python**.
   - Leave the black window open while you use the app; closing it closes the app.

Updates are automatic, see [Updates](#updates).

## Linux AppImage (no install)
Download `medialogger-x86_64.AppImage` from the repo's **Releases** page, make it executable (`chmod +x medialogger-x86_64.AppImage`, or tick "Allow executing" in your file manager's Properties) and run it.

It's the same app as every other version, and it carries what a desktop would otherwise have to provide: Python, Qt, its own libraries and fonts (Japanese and emoji included, for titles), and the dark look, so it appears the same on every distro. It needs only a Linux desktop with glibc 2.35 or newer (Ubuntu 22.04+, Debian 12+, recent Fedora, Arch, Steam Deck). Each release is tested on Ubuntu 24.04, Debian 12, Fedora and Arch.

To build it yourself, run `appimage/build.sh` on a Debian or Ubuntu machine (on NixOS: `steam-run appimage/build.sh`); `.github/workflows/build.yml` lists the packages it needs. On GitHub, **Actions → Build → Run workflow** builds and tests it on those distros and on Windows, and pushing a `v*` tag publishes it on that release once everything passes.

## Updates
Every copy updates itself when it starts, so there's nothing to re-download:
- A `git clone` runs `git pull`.
- The ZIP, the AppImage and the Nix package download the latest `medialogger_app/` from this repository's `master` into `~/.local/share/medialogger/code/` and run it from there.

If you're offline, or an update is broken, the app starts with the version it has. This runs code from this repository on your machine each time it changes.

## Development

`python medialogger.py --selftest` or `nix flake check`. The selftest is offline and needs only Python.
