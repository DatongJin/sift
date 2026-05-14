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

    # Exclude heavy scientific / dev libraries that an Anaconda install
    # would otherwise drag in (pandas, numpy, scipy, matplotlib, etc.).
    # Sift only needs the Python stdlib + Pillow.
    excludes = [
        "numpy", "pandas", "scipy", "matplotlib", "sympy",
        "IPython", "jedi", "notebook", "jupyter", "jupyter_client",
        "sphinx", "pytest", "pyarrow", "h5py", "tables",
        "sklearn", "statsmodels", "seaborn", "plotly",
        "PyQt5", "PyQt6", "PySide2", "PySide6",
        "tornado",
        "test",
    ]
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--onefile",
        "--windowed",
        "--name", "sift",
        "--add-data", f"app_ui.html{';' if sys.platform == 'win32' else ':'}.",
        "--clean",
        "--noconfirm",
    ]
    for mod in excludes:
        cmd.extend(["--exclude-module", mod])
    cmd.append("dupfinder_app.py")
    print("\n$ " + " ".join(cmd) + "\n")
    rc = subprocess.call(cmd, cwd=str(HERE))
    if rc != 0:
        print(f"\nPyInstaller exited with code {rc}.", file=sys.stderr)
        return rc

    exe_name = "sift.exe" if sys.platform == "win32" else "sift"
    exe = HERE / "dist" / exe_name
    if not exe.is_file():
        print("Build completed but the expected output file was not found.")
        return 1
    size_mb = exe.stat().st_size / (1024 * 1024)
    print(f"\nSuccess: {exe}  ({size_mb:.1f} MB)")

    # Bundle the exe into a zip so the release looks like a typical
    # portable Windows tool: a zip containing the exe and a short readme.
    import zipfile
    import textwrap
    readme = textwrap.dedent(f"""\
        Sift — find duplicates, similar photos, empty folders, and what's eating your disk.

        How to use
        ----------
        1. Double-click `sift.exe`.
        2. If Windows shows a "SmartScreen protected your PC" dialog, click
           "More info" -> "Run anyway". (We are not yet code-signed.)
        3. A chromeless window opens. Add a folder, run a scan.
        4. Close the window to exit. Files moved to quarantine can be
           restored anytime via the Restore button.

        Privacy
        -------
        Sift runs entirely on your computer. No network calls, no telemetry,
        no uploads. Your files never leave your machine.

        Source: https://github.com/DatongJin/sift
        """)
    zip_name = "sift-win64.zip" if sys.platform == "win32" else "sift.zip"
    zip_path = HERE / "dist" / zip_name
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        zf.write(exe, arcname=exe_name)
        zf.writestr("README.txt", readme)
    zip_mb = zip_path.stat().st_size / (1024 * 1024)
    print(f"Packaged: {zip_path}  ({zip_mb:.1f} MB)")
    print("\nBoth the bare exe and the zip are in dist/.")
    print("Upload one (or both) as release assets on GitHub.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
