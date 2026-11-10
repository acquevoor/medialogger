#!/usr/bin/env python3
"""Copy the shared libraries the app needs from the build machine into the AppDir, so it doesn't depend on what the
target machine has installed. Left out on purpose (the AppImage project's own rule): glibc and the C++ runtime, the
graphics driver stack and the X11 / fontconfig core, which must match the host. Usage: bundle_libs.py <AppDir>"""
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

KEEP_HOST = re.compile(r"^(ld-linux|libc|libm|libdl|librt|libpthread|libresolv|libnsl|libutil|libanl|libgcc_s|libstdc\+\+"
                       r"|libGL|libEGL|libGLX|libGLdispatch|libOpenGL|libgbm|libdrm|libvulkan"
                       r"|libX11|libxcb|libXext|libfontconfig|libfreetype|libexpat|libz)\.so")
# what the app can't run without: a library missing for one of these fails the build
CORE = re.compile(r"libQt6(Core|Gui|Widgets|Network|Svg|DBus|WaylandClient|XcbQpa)\b|libqxcb\.so|libqwayland"
                  r"|imageformats/|Qt(Core|Gui|Widgets|Network)\.abi3")


def ldd(path, env):
    r = subprocess.run(["ldd", str(path)], capture_output=True, text=True, env=env)
    return re.findall(r"^\s*(\S+) => (\S+)", r.stdout, re.M)


def main(appdir):
    appdir = Path(appdir)
    lib = appdir / "usr/lib"
    lib.mkdir(parents=True, exist_ok=True)
    site = next(appdir.glob("usr/python/lib/python3*/site-packages"))
    qt = site / "PySide6/Qt"
    env = {**os.environ, "LD_LIBRARY_PATH": f"{qt / 'lib'}:{appdir / 'usr/python/lib'}:{site / 'shiboken6'}:{site / 'PySide6'}"}
    files = [p for p in site.rglob("*.so*") if p.is_file()]
    copied, missing = {}, {}
    for f in files:
        for name, where in ldd(f, env):
            if where == "not":
                missing.setdefault(name, []).append(str(f.relative_to(site)))
            elif not KEEP_HOST.match(name) and not where.startswith(str(appdir.resolve())) and name not in copied:
                shutil.copy2(where, lib / name)  # follows the symlink: the real file under its soname
                copied[name] = where
    print(f"bundled {len(copied)} libraries:", " ".join(sorted(copied)))
    fatal = {n: u for n, u in missing.items() if any(CORE.search(x) for x in u)}
    for n, u in missing.items():
        print(f"{'MISSING' if n in fatal else 'optional, missing'}: {n} (for {u[0]}{' and more' if len(u) > 1 else ''})")
    if fatal:
        sys.exit(f"{len(fatal)} libraries the app needs are not on the build machine")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("This is a build tool for the AppImage, not the app.\n"
                 "To run the app, download the AppImage from the Releases page, or run: python3 medialogger.py\n"
                 "(from the folder above this one). See the README.")
    main(sys.argv[1])
