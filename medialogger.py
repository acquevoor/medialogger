#!/usr/bin/env python3
"""medialogger: a local log of games, anime, manga, music, films, light novels, visual novels and characters. This launcher installs
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
DATA = os.path.join(os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share"), "medialogger")
CODE = os.path.join(DATA, "code")
VENV = os.path.join(DATA, "venv")
VENV_PY = os.path.join(VENV, "Scripts", "python.exe") if os.name == "nt" else os.path.join(VENV, "bin", "python")


def stop(msg):
    input(f"{msg}\nPress Enter to close.")
    sys.exit(1)


def restart(python):
    sys.exit(subprocess.call([python, os.path.abspath(__file__)] + sys.argv[1:]))


def ensure_pyside():
    """On a plain Python (Windows double-click, a Linux distro's python3) the app runs in its own venv, made and filled
    with PySide6 on the first start. A venv, because distro Pythons refuse pip installs into themselves (PEP 668), and it
    needs no admin rights anywhere. Called only when the window is about to open, never on import: the selftest
    imports this file on a Python without Qt."""
    try:
        import PySide6.QtWidgets  # noqa: F401
        return
    except ImportError as e:
        err = e
    tried = os.environ.get("MEDIALOGGER_PIP_TRIED")
    if os.path.realpath(sys.prefix) != os.path.realpath(VENV):
        if not os.path.exists(VENV_PY):
            if tried:
                stop(f"PySide6 can't load: {err}")
            print("First run: setting up, this takes a minute or two. Keep this window open...")
            if subprocess.call([sys.executable, "-m", "venv", VENV]):
                shutil.rmtree(VENV, ignore_errors=True)  # a half-made venv (no pip) would fail the same way every start
                stop("Couldn't create the app's Python environment (see above). On Debian or Ubuntu: sudo apt install python3-venv")
        restart(VENV_PY)
    # inside the venv. Only install when PySide6 is truly absent; installed-but-broken gets the error, not a reinstall.
    # One attempt only: if the fresh install still can't import, a restart loop would fork-bomb the machine
    if importlib.util.find_spec("PySide6") or tried:
        stop(f"PySide6 is installed but can't load: {err}")
    print("Installing PySide6...")
    # Essentials: Qt without the built-in browser this app never uses, a third of the download
    if subprocess.call([sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "PySide6-Essentials"]):
        stop("Installing PySide6 failed (see above).")
    os.environ["MEDIALOGGER_PIP_TRIED"] = "1"
    restart(sys.executable)


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


def pulls(folder):
    """A git clone with git to pull it. A clone on a PC without git (GitHub Desktop, a copied folder) updates by download instead."""
    return os.path.exists(os.path.join(folder, ".git")) and shutil.which("git")


def update(folder=HERE, code=CODE):
    """Never stops the app: offline, no git, local edits in the way or a bad download just means it starts as it is.
    A clone pulls. A ZIP and the Nix package have no .git (and the Nix one is read-only), so the
    latest medialogger_app/ goes in CODE, which is run instead of the copy next to this file."""
    try:
        if pulls(folder):
            r = subprocess.run(["git", "-C", folder, "pull", "--ff-only", "--quiet"], capture_output=True, text=True, timeout=15,
                               env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
            print("Not updated:", r.stderr.strip()) if r.returncode else print("Up to date")
            return
        # git's own ref list, as `git ls-remote` reads it: the REST API allows 60 calls an hour per IP, and a shared IP runs out
        refs = fetch(f"https://github.com/{REPO}.git/info/refs?service=git-upload-pack", timeout=8)
        m = re.search(rb"([0-9a-f]{40}) refs/heads/master\b", refs)
        if not m:
            raise ValueError("unexpected answer from GitHub")
        sha = m.group(1).decode()
        if os.path.basename(current(code) or "") == sha:
            return print("Up to date")
        os.makedirs(code, exist_ok=True)
        install(fetch(f"https://github.com/{REPO}/archive/{sha}.zip"), sha, code)
        print("Updated to", sha[:7])
    except Exception as e:
        print("Not updated:", e)


def load():
    """The app's main(). If the downloaded code won't load, the copy next to this file still does."""
    folder = None if pulls(HERE) else current(CODE)
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
    if sys.argv[1:] == ["--update"]:  # CI checks this: the download worked and loads in here
        print("code:", sys.modules[main.__module__].__file__)
    else:
        main()
