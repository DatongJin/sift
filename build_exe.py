"""Build a standalone Windows .exe of Sift with PyInstaller.

Run once: python build_exe.py

Produces dist\\sift.exe — fully self-contained (no Python install required).
Copy that single file anywhere; double-click to launch.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def ensure_pyinstaller() -> bool:
    try:
        import PyInstaller  # noqa: F401
        return True
    except ImportError:
        pass
    print("PyInstaller is not installed.")
    print("It's needed to build a standalone .exe (~50 MB install).")
    ans = input("Install it now via pip? [y/N] ").strip().lower()
    if ans not in ("y", "yes"):
        print("Aborted.")
        return False
    subprocess.check_call([sys.executable, "-m", "pip", "install", "pyinstaller"])
    return True


def main() -> int:
    for f in ("dupfinder_app.py", "app_ui.html", "dupfinder.py"):
        if not (HERE / f).is_file():
            print(f"error: missing {f}", file=sys.stderr)
            return 2

    if not ensure_pyinstaller():
        return 1

    # Clean previous artifacts so subsequent runs don't surprise us.
    for d in ("build", "dist"):
        target = HERE / d
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
    spec = HERE / "dupfinder.spec"
    if spec.exists():
        spec.unlink()

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--onefile",
        "--windowed",
        "--name", "sift",
        "--add-data", f"app_ui.html{';' if sys.platform == 'win32' else ':'}.",
        "--clean",
        "--noconfirm",
        "dupfinder_app.py",
    ]
    print("\n$ " + " ".join(cmd) + "\n")
    rc = subprocess.call(cmd, cwd=str(HERE))
    if rc != 0:
        print(f"\nPyInstaller exited with code {rc}.", file=sys.stderr)
        return rc

    exe_name = "sift.exe" if sys.platform == "win32" else "sift"
    exe = HERE / "dist" / exe_name
    if exe.is_file():
        size_mb = exe.stat().st_size / (1024 * 1024)
        print(f"\nSuccess: {exe}  ({size_mb:.1f} MB)")
        print("Double-click that file to launch the app.")
        print("You can copy the single .exe anywhere — no Python needed on that machine.")
    else:
        print("Build completed but the expected output file was not found.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
