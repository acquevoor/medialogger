#!/usr/bin/env python3
"""medialogger: a local log of games, anime, manga, music, films, light novels and visual novels. This launcher installs
PySide6 on a plain Python (Windows double-click), keeps the app up to date and starts it. The app lives in the
medialogger_app/ folder next to this file."""
import importlib.util
import io
import os
import re
import shutil
import subprocess
import sys
import threading
import urllib.request
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)  # the medialogger_app/ folder next to this file
REPO = "acquevoor/medialogger"
CODE = os.path.join(os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share"), "medialogger", "code")


def ensure_pyside():
    """First double-click on a plain Python install (e.g. Windows): fetch PySide6, then restart.
    Called only when the window is about to open, never on import: the selftest imports this file on a Python without Qt."""
    try:
        import PySide6.QtWidgets  # noqa: F401
        return
    except ImportError as e:
        # only install when PySide6 is truly absent; installed-but-broken gets the error, not a reinstall.
        # one attempt only: if the fresh install still can't import, a restart loop would fork-bomb the machine
        if importlib.util.find_spec("PySide6") or os.environ.get("MEDIALOGGER_PIP_TRIED"):
            input(f"PySide6 is installed but can't load: {e}\nPress Enter to close.")
            sys.exit(1)
    print("First run: installing PySide6, this takes a minute or two. Keep this window open...")
    # Essentials: Qt without the built-in browser this app never uses, a third of the download
    if subprocess.call([sys.executable, "-m", "pip", "install", "PySide6-Essentials"]):
        input("Installing PySide6 failed (see above). Press Enter to close.")
        sys.exit(1)
    os.environ["MEDIALOGGER_PIP_TRIED"] = "1"
    sys.exit(subprocess.call([sys.executable] + sys.argv))


def fetch(url, accept="*/*", timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": "medialogger", "Accept": accept})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def current(code):
    """The folder of the downloaded code to run, or None."""
    try:
        with open(os.path.join(code, "current")) as f:
            folder = os.path.join(code, f.read().strip())
    except OSError:
        return None
    return folder if os.path.exists(os.path.join(folder, "medialogger_app", "ui.py")) else None


def install(data, sha, code):
    """Put a GitHub ZIP's medialogger_app/ in CODE/<sha>; nothing changes unless every file compiles."""
    part = os.path.join(code, f"{sha}.{os.getpid()}.part")
    shutil.rmtree(part, ignore_errors=True)
    os.makedirs(os.path.join(part, "medialogger_app"))
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for name in z.namelist():
            top = name.split("/")  # <repo>-<sha>/medialogger_app/x.py; anything else stays out
            if len(top) == 3 and top[1] == "medialogger_app" and top[2].endswith(".py"):
                body = z.read(name)
                compile(body, name, "exec")
                with open(os.path.join(part, "medialogger_app", top[2]), "wb") as f:
                    f.write(body)
    if not os.path.exists(os.path.join(part, "medialogger_app", "ui.py")):
        raise ValueError("no app in the download")
    keep = {sha, os.path.basename(current(code) or "")}  # the older one: a window still open may import from it
    if os.path.exists(os.path.join(code, sha)):
        shutil.rmtree(part)
    else:
        os.rename(part, os.path.join(code, sha))
    with open(os.path.join(code, "current.tmp"), "w") as f:
        f.write(sha)
    os.replace(os.path.join(code, "current.tmp"), os.path.join(code, "current"))
    for old in set(os.listdir(code)) - keep - {"current"}:
        shutil.rmtree(os.path.join(code, old), ignore_errors=True)


def update(folder=HERE, code=CODE):
    """Never stops the app: offline, no git, local edits in the way or a bad download just means it starts as it is.
    A clone pulls. A ZIP, the AppImage and the Nix package have no .git (and two of them are read-only), so the
    latest medialogger_app/ goes in CODE, which is run instead of the copy next to this file."""
    try:
        if os.path.exists(os.path.join(folder, ".git")):
            r = subprocess.run(["git", "-C", folder, "pull", "--ff-only", "--quiet"], capture_output=True, text=True, timeout=15,
                               env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
            print("Not updated:", r.stderr.strip()) if r.returncode else print("Up to date")
            return
        sha = fetch(f"https://api.github.com/repos/{REPO}/commits/master", "application/vnd.github.sha", 8).decode().strip()
        if not re.fullmatch("[0-9a-f]{40}", sha):
            raise ValueError("unexpected answer from GitHub")
        if os.path.basename(current(code) or "") == sha:
            return print("Up to date")
        os.makedirs(code, exist_ok=True)
        install(fetch(f"https://github.com/{REPO}/archive/{sha}.zip"), sha, code)
        print("Updated to", sha[:7])
    except Exception as e:
        print("Not updated:", e)


def load():
    """The app's main(). If the downloaded code won't load, the copy next to this file still does."""
    folder = None if os.path.exists(os.path.join(HERE, ".git")) else current(CODE)
    if folder and sys.argv[1:] in ([], ["--update"]):
        sys.path.insert(0, folder)
        try:
            from medialogger_app.ui import main
            return main
        except Exception as e:
            print("The update won't load, using the built-in version:", e)
            sys.path.remove(folder)
            for m in [m for m in sys.modules if m.split(".")[0] == "medialogger_app"]:
                del sys.modules[m]
    from medialogger_app.ui import main
    return main


if __name__ == "__main__":
    if "--selftest" in sys.argv:  # offline and Qt-free: runs on any Python 3.10+
        from medialogger_app.selftest import selftest
        selftest()
        sys.exit()
    ensure_pyside()
    if sys.argv[1:] in ([], ["--update"]):  # not for --screenshot and the like
        # a thread: a dead network can hang past any timeout. If it's still going, the app starts and it finishes for next time
        t = threading.Thread(target=update, daemon=True)
        t.start()
        t.join(90 if sys.argv[1:] else 20)
    main = load()
    if sys.argv[1:] == ["--update"]:  # the AppImage's build checks this: the download worked and loads in here
        print("code:", sys.modules[main.__module__].__file__)
    else:
        main()
