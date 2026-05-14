# Sift

Find duplicates, similar photos, empty folders, and what's eating your disk — safely.

A local-only desktop app for cleaning up files without fear. Everything destructive
goes through a quarantine you can restore from; the server only listens on
`127.0.0.1` and rejects cross-origin requests.

## Download

[**Download Sift for Windows**](https://github.com/DatongJin/sift/releases/latest/download/sift.exe)
(~16 MB, single .exe, no installer, no Python required).

Also see the [landing page](https://datongjin.github.io/sift/) once the repo is public.

## Features

| Mode | What it does |
| --- | --- |
| **Duplicate files** | Find byte-identical files via SHA-256 (three-tier filter: size → 8 KB partial hash → full hash). |
| **Similar photos** | Find visually similar shots via perceptual hashing (dHash). Catches burst shots, recompressed copies, and slight crops — point at a folder, pick the keepers, quarantine the rest. |
| **Empty folders** | Find recursively empty directories. Optionally treats junk metadata (`Thumbs.db`, `.DS_Store`, `desktop.ini`) as effectively empty. |
| **Largest items** | Top-N largest files and folders by recursive size. Answers "what's eating my disk?" |

All actions default to a quarantine (`.dupfinder_quarantine/`) with a JSON manifest;
nothing is permanently deleted unless you explicitly opt in. A one-click restore reverts
any quarantine run.

## Stack

- **Backend**: Python 3.10+ stdlib only (`http.server`, `threading`, `sqlite3`, `hashlib`).
- **Photo similarity**: requires [Pillow](https://pillow.readthedocs.io/) (optional —
  the rest of the app works without it). Anaconda installs include it.
- **UI**: vanilla HTML/CSS/JS served from the same Python process. No build step,
  no node_modules.
- **Browser shell**: launches Edge or Chrome in `--app` mode for a chromeless window.
  Falls back to the default browser.

## Run

### Easiest (Windows)
Double-click **`sift.vbs`**. A chromeless window opens.

If the server is already running and you closed the window, double-click **`open_ui.vbs`**
to reopen the UI.

### From a terminal
```
python dupfinder_app.py              # launches UI in a browser
python dupfinder_app.py --no-browser # server only; open the URL yourself
```

### As a standalone .exe
```
python build_exe.py
```
Produces `dist/sift.exe` — a single ~15 MB file with no Python dependency. Copy
anywhere; double-click to launch.

### CLI (no UI)
The core scanner can run headless:
```
python dupfinder.py <dir>...                          # find duplicates
python dupfinder.py <dir>... --find-empty             # find empty folders
python dupfinder.py <dir>... --largest                # find largest items
python dupfinder.py <dir>... --similar-images         # find similar photos
python dupfinder.py --restore <quarantine-path>       # restore a previous run
```

`--auto keep-newest|keep-oldest|keep-shortest|keep-in <PATH>` for non-interactive
duplicate cleanup. `--permanent` to unlink instead of quarantining (irreversible).

## Safety model

- **127.0.0.1 only**: server never listens on a public interface.
- **CSRF guard**: rejects cross-origin POSTs (`Origin` and `Host` header check),
  requires `Content-Type: application/json` to block form-based attacks.
- **Validate-then-act**: before executing any delete, files are re-stat'd; if size
  (and optionally hash) changed since the scan, the file is skipped — not deleted.
- **Quarantine by default**: every "delete" is actually `shutil.move` into a
  timestamped folder with a JSON manifest. `python dupfinder.py --restore <folder>`
  puts everything back.
- **Protected paths**: mark folders that should never be deleted from, even if
  their content matches a duplicate group.

## Performance notes

Photo similarity uses three optimizations stacked:
1. `Image.draft()` mode tells libjpeg to decode large JPEGs at 1/4 or 1/8 scale
   in the DCT domain — ~10× faster on DSLR-size files with zero dHash accuracy loss.
2. Single decode per image — dHash, dimensions, and thumbnail all come from one
   `Image.open()`.
3. `ThreadPoolExecutor` with 8 workers — Pillow releases the GIL during decode,
   so this scales near-linearly.

Net: ~30× speedup vs naive single-threaded full-decode. A folder of 1,700
12 MB DSLR JPGs scans in about 30 seconds on a modern laptop.

## Repository layout

| File | Purpose |
| --- | --- |
| `dupfinder.py` | Core scanner + CLI (~1200 lines). Module name kept for back-compat. |
| `dupfinder_app.py` | Local HTTP server + browser launcher. |
| `app_ui.html` | The web UI (handwritten CSS, vanilla JS, dark / light themes). |
| `test_dupfinder.py` | unittest suite, 83+ tests. |
| `sift.vbs` | Windows silent launcher. |
| `open_ui.vbs` | Reopen the UI window when server is already running. |
| `build_exe.py` | One-shot PyInstaller build for `sift.exe`. |
| `dupfinder_gui.py` | Legacy Tkinter GUI (kept for reference; not used by the web app). |

## Tests

```
python test_dupfinder.py
```
