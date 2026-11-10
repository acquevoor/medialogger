#!/usr/bin/env bash
# Build medialogger-x86_64.AppImage: a portable CPython (python-build-standalone) + PySide6-Essentials from pip (its
# wheels carry Qt and WebP; the app needs no built-in browser) + the app, packed by the official appimagetool.
# Runs on any glibc Linux (CI: ubuntu); on NixOS run it inside `steam-run`.
# Usage: appimage/build.sh            -> medialogger-x86_64.AppImage in the repo root
set -euo pipefail
cd "$(dirname "$0")/.."
BUILD=build/appimage
APPDIR=$BUILD/AppDir
PY_SERIES=${PY_SERIES:-3.12}
mkdir -p "$BUILD"

# 1. portable Python: the newest python-build-standalone release for PY_SERIES (ponytail: newest at build time,
#    pin PY_URL to a fixed asset if builds must be reproducible)
if [ -z "${PY_URL:-}" ]; then
  # the "+" in the asset names is written %2B in the download URLs
  PY_URL=$(curl -fsSL https://api.github.com/repos/astral-sh/python-build-standalone/releases/latest |
    grep -oE "https://[^\"]*cpython-${PY_SERIES//./\\.}\.[0-9]+(\+|%2B)[0-9]+-x86_64-unknown-linux-gnu-install_only\.tar\.gz" | head -1) || true
  [ -n "$PY_URL" ] || { echo "could not find a python-build-standalone ${PY_SERIES} download (set PY_URL)" >&2; exit 1; }
fi
echo "python: $PY_URL"
[ -f "$BUILD/python.tar.gz" ] || curl -fL -o "$BUILD/python.tar.gz" "$PY_URL"

# 2. appimagetool (official, AppImage/appimagetool)
TOOL=$BUILD/appimagetool
[ -x "$TOOL" ] || { curl -fL -o "$TOOL" https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage; chmod +x "$TOOL"; }

# 3. the AppDir
rm -rf "$APPDIR"
mkdir -p "$APPDIR/usr/app"
tar -xzf "$BUILD/python.tar.gz" -C "$APPDIR/usr"      # -> usr/python
PY="$APPDIR/usr/python/bin/python3"
"$PY" -m pip install --no-cache-dir --disable-pip-version-check --no-warn-script-location ${PYSIDE:-PySide6-Essentials}
# nothing of the host's desktop theme may load: the app brings its own look (medialogger_app/look.py)
SITE=$("$PY" -c "import sysconfig; print(sysconfig.get_paths()['purelib'])")
rm -f "$SITE"/PySide6/Qt/plugins/platformthemes/libqgtk3.so
# what the app never loads: QML, 3D, PDF images, and the dev tools and headers. The PDF image plugin needs
# libQt6Pdf, which only the full PySide6 carries
rm -rf "$SITE"/PySide6/{include,typesystems,glue,scripts,examples,doc,assistant,designer,linguist,lupdate,lrelease,uic,rcc} \
       "$SITE"/PySide6/Qt/qml "$SITE"/PySide6/Qt/plugins/imageformats/libqpdf.so \
       "$SITE"/PySide6/Qt/plugins/{designer,qmltooling,sqldrivers,sceneparsers,assetimporters,geometryloaders,renderers,renderplugins,canbus,sensors,texttospeech,position,scxmldatamodel}
find "$APPDIR/usr/python" -name "__pycache__" -prune -exec rm -rf {} +
cp -r medialogger.py medialogger_app README.md "$APPDIR/usr/app/"
rm -rf "$APPDIR/usr/app/medialogger_app/__pycache__"
(cd "$APPDIR/usr/app" && sha256sum medialogger.py medialogger_app/*.py | awk '{print $1, $2}' > MANIFEST)

# everything else a desktop session would provide: libraries and fonts
python3 appimage/bundle_libs.py "$APPDIR"
mkdir -p "$APPDIR/usr/fonts"
for f in /usr/share/fonts/truetype/dejavu/DejaVuSans.ttf /usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf \
         /usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc /usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc \
         /usr/share/fonts/truetype/noto/NotoColorEmoji.ttf; do
  cp "$f" "$APPDIR/usr/fonts/"
done

# programs of the host (your browser, file manager) must not inherit the AppImage's libraries
mkdir -p "$APPDIR/usr/bin"
cat > "$APPDIR/usr/bin/xdg-open" <<'OPEN'
#!/bin/sh
unset LD_LIBRARY_PATH
PATH="$MEDIALOGGER_HOST_PATH"
exec xdg-open "$@"
OPEN
chmod +x "$APPDIR/usr/bin/xdg-open"

cat > "$APPDIR/AppRun" <<'RUN'
#!/bin/sh
# The app inside the AppImage; profiles and covers go to the usual places in your home folder.
# Nothing of the host's Python or Qt setup may leak in, or its plugins and themes would load into this Qt.
HERE="$(dirname "$(readlink -f "$0")")"
unset PYTHONPATH PYTHONHOME QT_PLUGIN_PATH QML2_IMPORT_PATH QT_STYLE_OVERRIDE QT_QPA_PLATFORMTHEME QT_QPA_PLATFORM_PLUGIN_PATH
export PYTHONNOUSERSITE=1 MEDIALOGGER_BUNDLED=1 MEDIALOGGER_FONTS="$HERE/usr/fonts" MEDIALOGGER_HOST_PATH="$PATH"
export LD_LIBRARY_PATH="$HERE/usr/lib" PATH="$HERE/usr/bin:$PATH"
exec "$HERE/usr/python/bin/python3" "$HERE/usr/app/medialogger.py" "$@"
RUN
chmod +x "$APPDIR/AppRun"
cat > "$APPDIR/medialogger.desktop" <<'DESK'
[Desktop Entry]
Type=Application
Name=Media Logger
Comment=Log games, anime, manga, music, films, light novels and visual novels
Exec=medialogger
Icon=medialogger
Categories=Utility;
DESK
# the icon, drawn with the bundled Qt (no binary file in the repo)
QT_QPA_PLATFORM=offscreen "$PY" - "$APPDIR/medialogger.png" <<'ICON'
import sys
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QGuiApplication, QImage, QPainter
app = QGuiApplication([])
img = QImage(256, 256, QImage.Format_ARGB32)
img.fill(Qt.transparent)
p = QPainter(img)
p.setRenderHint(QPainter.Antialiasing)
p.setBrush(QColor("#2fb344"))
p.setPen(Qt.NoPen)
p.drawRoundedRect(QRectF(8, 8, 240, 240), 48, 48)
p.setPen(QColor("white"))
p.setFont(QFont("Sans", 110, QFont.Bold))
p.drawText(QRectF(0, 0, 256, 256), Qt.AlignCenter, "mL")
p.end()
img.save(sys.argv[1])
ICON
ln -sf medialogger.png "$APPDIR/.DirIcon"

# 4. pack
ARCH=x86_64 APPIMAGE_EXTRACT_AND_RUN=1 "$TOOL" --no-appstream "$APPDIR" medialogger-x86_64.AppImage
ls -la medialogger-x86_64.AppImage
