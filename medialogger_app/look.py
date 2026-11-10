"""What the AppImage brings along that a desktop session normally provides: the look (KvDark colors on Fusion, the same
colors the Nix version gets from Kvantum), the fonts (DejaVu Sans, Noto CJK for Japanese titles, Noto Color Emoji), and a
self-check. Nothing here applies outside the AppImage: every other install keeps following the desktop's theme."""
import hashlib
import importlib
import logging
import os
import sys
from pathlib import Path

from PySide6.QtCore import QLibraryInfo
from PySide6.QtGui import QColor, QFont, QFontDatabase, QFontMetrics, QImageReader, QPalette

log = logging.getLogger("medialogger")
FAMILIES = ["DejaVu Sans", "Noto Sans CJK JP", "Noto Color Emoji"]
KVDARK = {"window": "#3D3D3E", "base": "#2E2E2E", "alt": "#383838", "light": "#646464", "midlight": "#555555", "dark": "#1E1E1E",
          "mid": "#3C3C3C", "highlight": "#C3C3C3", "text": "#FFFFFF", "disabled": "#A0A0A0", "highlighted_text": "#000000",
          "link": "#2EB8E6", "visited": "#FF6666"}


def bundled():
    return os.environ.get("MEDIALOGGER_BUNDLED") == "1"


def palette():
    c = {k: QColor(v) for k, v in KVDARK.items()}
    p = QPalette()
    R, G = QPalette.ColorRole, QPalette.ColorGroup
    for role, name in ((R.Window, "window"), (R.Button, "window"), (R.Base, "base"), (R.AlternateBase, "alt"), (R.ToolTipBase, "window"),
                       (R.Light, "light"), (R.Midlight, "midlight"), (R.Dark, "dark"), (R.Mid, "mid"), (R.Shadow, "dark"),
                       (R.WindowText, "text"), (R.Text, "text"), (R.ButtonText, "text"), (R.BrightText, "text"), (R.ToolTipText, "text"),
                       (R.PlaceholderText, "disabled"), (R.Highlight, "highlight"), (R.HighlightedText, "highlighted_text"),
                       (R.Link, "link"), (R.LinkVisited, "visited")):
        p.setColor(role, c[name])
    for role in (R.WindowText, R.Text, R.ButtonText):
        p.setColor(G.Disabled, role, c["disabled"])
    return p


def apply(app):
    """Give the AppImage the same look on every machine. Does nothing elsewhere."""
    if not bundled():
        return
    app.setStyle("Fusion")
    app.setPalette(palette())
    folder = Path(os.environ.get("MEDIALOGGER_FONTS", ""))
    loaded = [f.name for f in sorted(folder.glob("*.tt[fc]")) if QFontDatabase.addApplicationFont(str(f)) >= 0]
    font = QFont()
    font.setFamilies(FAMILIES)
    app.setFont(font)
    if hasattr(QFontDatabase, "setApplicationEmojiFontFamilies"):
        QFontDatabase.setApplicationEmojiFontFamilies(["Noto Color Emoji"])
    log.info("appimage: Fusion + KvDark colors, fonts %s", ", ".join(loaded) or "NONE")


def bundle_check(app):
    """--bundle-check: prove everything the AppImage carries works here. -> list of failures (empty = fine)."""
    bad = []

    def need(ok, what):
        print(("ok   " if ok else "FAIL ") + what, flush=True)
        if not ok:
            bad.append(what)
    formats = [bytes(f) for f in QImageReader.supportedImageFormats()]
    need(b"webp" in formats and b"jpeg" in formats and b"png" in formats, "images: WebP, JPEG, PNG")
    plugins = Path(QLibraryInfo.path(QLibraryInfo.LibraryPath.PluginsPath))
    for p in ("platforms/libqxcb.so", "platforms/libqwayland.so"):
        need((plugins / p).exists(), f"Qt plugin {p}")
    need(not (plugins / "platformthemes/libqgtk3.so").exists(), "no GTK theme plugin (the host's theme can't interfere)")
    need(app.style().objectName().lower() == "fusion" and app.palette().window().color().name() == KVDARK["window"].lower(), "KvDark look on Fusion")
    fams = QFontDatabase.families()
    for f in FAMILIES:
        need(f in fams, f"font {f}")
    need(QFontMetrics(QFont("Noto Sans CJK JP")).inFontUcs4(0x521D), "Japanese glyphs")
    need(QFontMetrics(QFont("Noto Color Emoji")).inFontUcs4(0x1F4CB), "emoji glyphs")
    root = Path(__file__).resolve().parents[1]
    manifest = root / "MANIFEST"
    need(manifest.exists(), "file list")
    for line in manifest.read_text().splitlines() if manifest.exists() else []:
        sha, name = line.split(maxsplit=1)
        need((root / name).exists() and hashlib.sha256((root / name).read_bytes()).hexdigest() == sha, f"app file {name}")
    for m in sorted(p.stem for p in Path(__file__).parent.glob("*.py") if p.stem != "__init__"):
        try:
            importlib.import_module(f"medialogger_app.{m}")
        except Exception as e:
            need(False, f"module {m}: {e}")
    print(f"bundle check: {'FAILED: ' + str(len(bad)) if bad else 'all good'} ({sys.version.split()[0]})", flush=True)
    return bad
