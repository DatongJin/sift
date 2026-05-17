# Sift — Development Guide

A complete reference for working on Sift's codebase: architecture, every
module, the HTTP API, data flow, build/release, testing, and how to extend it.

For *why* decisions were made, see [DECISIONS.md](DECISIONS.md).
For end-user docs, see [README.md](README.md).

---

## 1. What Sift is

A local-only desktop tool that finds and safely removes:

- **Duplicate files** (byte-identical, SHA-256)
- **Similar photos** (perceptual hash / dHash)
- **Empty folders** (recursively empty)
- **Largest items** (top-N files and folders by size)

It runs a tiny Python HTTP server on `127.0.0.1`, opens a chromeless browser
window as the UI, and processes everything on the local machine. Nothing is
uploaded; there is no cloud component.

---

## 2. Repository layout

| Path | Purpose |
| --- | --- |
| `dupfinder.py` | **Core engine + CLI.** All scanning, hashing, quarantine, restore logic. ~1,600 lines. Module name kept for back-compat; the product is "Sift". |
| `dupfinder_app.py` | **HTTP server.** Wraps the core in a local web API, manages background scan jobs, CSRF, browser lifecycle. |
| `app_ui.html` | **The UI.** Single file: hand-written CSS, vanilla JS, dark/light theming. Served by the server. |
| `test_dupfinder.py` | **Test suite.** 83 `unittest` tests against the core. |
| `build_exe.py` | **Build script.** PyInstaller `--onefile` → `dist/sift.exe` + `dist/sift-win64.zip`. |
| `sift.vbs` | Windows silent launcher (double-click to run from source). |
| `open_ui.vbs` | Reopen the UI window if the server is already running. |
| `dupfinder_gui.py` | Legacy Tkinter GUI. Not used by the web app; kept for reference. |
| `docs/index.html` | Landing page, served via GitHub Pages. |
| `README.md` / `DECISIONS.md` / `DECISIONS.zh.md` / `DEVELOPMENT.md` | Docs. |
| `.dupfinder_quarantine/` | (runtime, gitignored) Quarantined files + manifests. |
| `.dupfinder_cache.sqlite3` | (runtime, gitignored) Hash cache. |

---

## 3. Architecture

```
                      ┌─────────────────────────────┐
   double-click  ───▶ │  sift.exe (dupfinder_app.py)│
                      │                             │
                      │  ┌───────────────────────┐  │
                      │  │ HTTP server thread    │  │  127.0.0.1:8765
                      │  │ (ThreadingHTTPServer) │◀─┼──── fetch() ────┐
                      │  └───────────────────────┘  │                 │
                      │  ┌───────────────────────┐  │                 │
                      │  │ background scan jobs  │  │                 │
                      │  │ (worker threads)      │  │                 │
                      │  └───────────────────────┘  │                 │
                      │  ┌───────────────────────┐  │                 │
                      │  │ main thread:          │  │                 │
                      │  │ browser_proc.wait()   │  │                 │
                      │  └───────────────────────┘  │                 │
                      └──────────────┬──────────────┘                 │
                                     │ launches                       │
                                     ▼                                │
                      ┌─────────────────────────────┐                 │
                      │ Edge/Chrome --app           │─────────────────┘
                      │ (chromeless window)         │   renders app_ui.html
                      │ --user-data-dir=<temp>      │
                      └─────────────────────────────┘
                       close window ⇒ process exits
                       ⇒ main thread unblocks ⇒ server shuts down
```

Three layers:

1. **Core (`dupfinder.py`)** — pure logic, no HTTP. Importable, CLI-runnable,
   fully unit-tested. Knows nothing about the web.
2. **Server (`dupfinder_app.py`)** — translates HTTP requests into core
   calls, manages long-running jobs, security, and process lifecycle.
3. **UI (`app_ui.html`)** — talks to the server over `fetch`. No build step.

The split means the core can be tested and used headlessly (the CLI), and the
server is a thin adapter.

---

## 4. Core engine (`dupfinder.py`)

### 4.1 Constants

| Constant | Value | Meaning |
| --- | --- | --- |
| `PARTIAL_BYTES` | 8 KB | Bytes hashed in the partial-hash pass |
| `CHUNK` | 64 KB | Read size for full hashing |
| `DEFAULT_SKIP` | `.git`, `node_modules`, `__pycache__`, quarantine/cache names | Always-skipped names |
| `QUARANTINE_DIRNAME` | `.dupfinder_quarantine` | Quarantine root dir name |
| `HASH_CACHE_FILENAME` | `.dupfinder_cache.sqlite3` | SQLite cache filename |
| `JUNK_FILES` | `thumbs.db`, `.ds_store`, `desktop.ini` | Treated as "not real content" |
| `SIMILAR_IMAGE_EXTS` | jpg/png/webp/bmp/gif/tiff… | Extensions scanned for similarity |
| `DEFAULT_SIMILARITY_THRESHOLD` | 5 | Default dHash Hamming threshold |
| `THUMBNAIL_SIZE` | 160 | Thumbnail max side (px) |

### 4.2 Data directory resolution

`app_data_dir()` → where quarantine + cache live. Tries, in order:
`Path.cwd()` → `~/.dupfinder` → `<temp>/dupfinder`, picking the first that's
writable (verified by a probe file). Cached after first call;
`reset_app_data_dir_cache()` exists for tests.

### 4.3 Scanning primitives

- `iter_files(roots, skip, min_size, cancel)` — generator yielding
  `(path, size, (dev, ino))`. Honors skip patterns and a `cancel`
  `threading.Event`. OS errors are logged and skipped, never fatal.
- `group_by_size(entries)` — `{size: [(path, ident), …]}`, keeping only
  sizes with > 1 file.
- `HashCache` — SQLite wrapper. Key: `(path, size, mtime_ns, partial,
  algorithm)`. `get`/`set`/`stats`. Auto-invalidates on file change because
  size/mtime are in the key.
- `hash_file(path, partial, cache, cancel)` — SHA-256. Partial = first 8 KB;
  full = streamed 64 KB chunks with a cancel check each chunk. Cache-aware.

### 4.4 Duplicate detection

`find_duplicates(size_groups, cancel, progress, cache)`:

```
for each size group:
    _refine_by_hash(partial=True)   # 8 KB hash buckets
        for each surviving bucket:
            _refine_by_hash(partial=False)  # full SHA-256 buckets
                _dedupe_hardlinks()  # collapse same dev+inode
                → a confirmed duplicate group
results sorted by wasted bytes desc
```

`_dedupe_hardlinks` matters on Windows: hardlinks share dev+inode, so they're
collapsed to one entry (deleting a hardlink wouldn't reclaim space). When
`st_ino == 0` (some FAT/network volumes) it falls back to treating each as
unique — safe (no data loss), just may show hardlinks as "duplicates".

### 4.5 Similar-photo pipeline

```
iter_files → filter by SIMILAR_IMAGE_EXTS
   ↓ ThreadPoolExecutor(8 workers)
_compute_image_signature(path):
   Image.open → draft("RGB", 160) → exif_transpose
   → thumbnail (160px JPEG base64)
   → grayscale 9×8 → dHash (64-bit int)
   → (dhash, width, height, thumbnail)
   ↓
union-find clustering: edge if hamming(a,b) ≤ threshold
   ↓
groups sorted by reclaimable bytes; each group's largest file = default keeper
```

- `compute_image_dhash`, `compute_image_thumbnail` — standalone (used by
  tests).
- `_compute_image_signature` — the fast production path (one decode, three
  outputs, `draft()` mode).
- `pillow_available()` — cached import check; the whole feature degrades
  gracefully if Pillow is missing.

### 4.6 Empty folders

`find_empty_folders(roots, skip, ignore_junk, cancel)` — `os.walk` bottom-up;
a dir is empty if it has no real files (junk optionally ignored) and every
subdir is itself empty. User-picked roots are never reported. Result sorted
deepest-first. `delete_empty_folders` removes junk then `rmdir`s — `rmdir`
fails on a non-empty dir, so a misjudgment can't destroy data.

### 4.7 Largest items

`compute_top_largest(roots, skip, min_size, top_files, top_folders, cancel)`
— single walk. A min-heap of size `top_files` tracks the biggest files;
per-directory direct sizes accumulate, then fold child → parent by depth to
get recursive folder sizes. Returns `(files, folders, total_files,
total_bytes)`.

### 4.8 Safety pipeline

| Function | Role |
| --- | --- |
| `DeletionCandidate` | `path, size, mtime_ts?, sha256?` |
| `validate_deletion_candidates(cands, protected_roots)` | Re-stat each file; reject if protected, missing, size changed, or (if a hash/mtime was supplied) content changed. Returns `(ready, errors)`. This is the TOCTOU guard. |
| `path_is_under_any` / `protected_indices` | Protected-path checks |
| `quarantine_files(deletions, root)` | `shutil.move` into `root`, preserving original path structure; writes `manifest.json`. Returns `(count, bytes, manifest, errors)`. |
| `permanent_delete_files` | `os.unlink`. Opt-in only. |
| `restore_from_manifest(path)` | Reads a manifest, moves files back; skips any whose original path is now occupied (never overwrites). |
| `list_quarantine_runs(base)` | Summaries of past runs for the history UI. |

### 4.9 Preview

`preview_file(path)` → bounded payload: `kind` ∈ {image, text, metadata,
error}. Images ≤ 8 MB are base64 data URLs; text ≤ 32 KB (binary-sniffed via
NUL byte); everything else returns metadata only.

### 4.10 CLI

`main(argv)` (entry: `python dupfinder.py …`). Modes: default = duplicates,
`--find-empty`, `--largest`, `--similar-images`, `--restore PATH`. Removal:
`--delete` (interactive menu via `parse_action` /
`build_decisions_interactive`), `--auto POLICY`, `--permanent`, `--yes`.
`--hash-cache` enables the SQLite cache.

---

## 5. HTTP server (`dupfinder_app.py`)

### 5.1 Request handling

`Handler(BaseHTTPRequestHandler)` on a `ThreadingHTTPServer`. Every POST
passes through `_csrf_ok()` then `_dispatch_post()`, the latter wrapped in a
catch-all that returns `{"error": "..."}` with HTTP 500 (so the UI never sees
an opaque failure).

### 5.2 CSRF guard (`_csrf_ok`)

Rejects a POST unless **all** hold:

1. `Host` header host is `127.0.0.1` / `localhost` (defeats DNS rebinding).
2. `Origin`, if present, equals `http://127.0.0.1:<port>`; `null` is rejected.
3. `Content-Type` is `application/json` (forces a CORS preflight we never
   answer → cross-origin `fetch` is blocked at the browser).

### 5.3 Background job system

Long scans can't be a single blocking request (the socket would time out), so:

- `_begin_scan()` / `_end_scan()` / `_signal_cancel()` — module-level
  `threading.Event`; a new scan cancels the previous one.
- `_create_scan_job(cancel)` → `job_id`; jobs live in `_scan_jobs` (capped at
  `MAX_SCAN_JOBS = 12`, oldest non-running pruned).
- `_update_scan_job` / `_public_scan_job` (strips the `cancel` Event before
  serializing).
- `_run_scan_job` / `_run_similar_images_job` — worker-thread entry points.
  They call the core with a `progress` callback that updates the job, then
  store the final `result` (or `error`/`cancelled`).

The UI: `POST /api/scan_start` → `{job_id}` → poll `POST /api/scan_status`
every ~450 ms until `status` ∈ {done, cancelled, error}.

### 5.4 Browser lifecycle

`_launch_browser_tracked(url)` finds Edge/Chrome (`_find_chromium_exe`) and
launches `--app=URL --user-data-dir=<temp/sift_browser_profile_<pid>>`,
returning the `Popen`. `main()` runs the server on a daemon thread and blocks
on `browser_proc.wait()`. Closing the window ⇒ process exits ⇒ server
shutdown + temp profile cleanup. No tracked browser ⇒ fall back to
`webbrowser.open` and stay alive until Ctrl+C (old behavior).

`--no-browser` keeps the server foregrounded and never opens a window (for
manual use / debugging).

### 5.5 Native folder picker (`_pick`)

Runs tkinter's `filedialog` in a daemon thread (lock-serialized). Previously a
subprocess — that broke in the frozen exe because `sys.executable` is
`sift.exe`, not a Python interpreter.

---

## 6. HTTP API reference

All endpoints are `POST`, JSON in / JSON out, same-origin only.

| Endpoint | Body | Returns |
| --- | --- | --- |
| `/api/browse_folder` | `{}` | `{path}` (native dialog) |
| `/api/browse_manifest` | `{}` | `{path}` |
| `/api/scan` | `{paths, skip?, min_size?, protected_paths?, hash_cache?}` | duplicate result (synchronous; legacy) |
| `/api/scan_start` | same as above | `{job_id, …}` |
| `/api/scan_status` | `{job_id}` | job state incl. `status`, `result` |
| `/api/scan_empty` | `{paths, skip?, ignore_junk?}` | `{folders, count}` |
| `/api/scan_largest` | `{paths, skip?, min_size?, top_files?, top_folders?}` | `{files, folders, total_files, total_bytes}` |
| `/api/scan_similar_images` | `{paths, skip?, min_size?, threshold?}` | similar result (synchronous; legacy) |
| `/api/scan_similar_start` | same | `{job_id, pillow_available}` |
| `/api/delete_empty_folders` | `{folders}` | `{count, errors}` |
| `/api/execute` | `{permanent, deletions:[{path,size,mtime_ts?,sha256?}], protected_paths?}` | `{mode, count, freed, errors}` |
| `/api/restore` | `{manifest_path}` | `{return_code}` |
| `/api/quarantine_history` | `{}` | `{runs:[…]}` |
| `/api/preview` | `{path}` | preview payload |
| `/api/open_location` | `{path}` | `{ok}` (opens Explorer) |
| `/api/cancel_scan` | `{}` | `{cancelled: bool}` |
| `/api/shutdown` | `{}` | `{ok}` then server stops |

`GET /` (and `/index.html`, `/app_ui.html`) serves the UI; `GET
/favicon.ico` → 204; anything else → 404.

---

## 7. The UI (`app_ui.html`)

Single file. `state` object holds folders, per-mode results, similarity
threshold, etc. `api(path, body)` is the fetch wrapper — it surfaces real
errors (HTTP status + body, bad JSON, network) instead of an empty `{}`.

Scan flow: `startScan* ()` → `activeFolders().map(f => f.path)` →
`/api/scan_*_start` → `waitForScan(jobId)` polls status → render. Each mode
has its own results container (`#results`, `#empty-results`,
`#largest-results`, `#similar-results`) and they don't clear each other.

Folders carry a `mode` (`normal` / `protected` / `excluded`). `activeFolders`
excludes the excluded; `protectedPaths` feeds the safety pipeline.

Theming is pure CSS via `prefers-color-scheme`; no JS toggle.

---

## 8. Build & release

### Build

```
python build_exe.py
```

Runs PyInstaller `--onefile --windowed --name sift` with a long
`--exclude-module` list (numpy/pandas/scipy/jupyter/…) so the Anaconda
toolchain doesn't bloat the binary (235 MB → 16 MB). Outputs:

- `dist/sift.exe` — the bare binary
- `dist/sift-win64.zip` — exe + `README.txt`

### Release (GitHub)

```
gh release create v0.1.0 dist/sift.exe dist/sift-win64.zip \
  --repo DatongJin/sift --title "…" --notes "…"
# or update assets on an existing tag:
gh release upload v0.1.0 dist/sift.exe dist/sift-win64.zip --clobber --repo DatongJin/sift
```

`releases/latest/download/sift-win64.zip` always points to the newest
release — the README and landing page link to that stable URL.

### Landing page

`docs/index.html` is served by GitHub Pages (configured: branch `main`, path
`/docs`). Every push that touches `docs/` auto-redeploys in ~30–60 s. Live at
`https://datongjin.github.io/sift/`.

---

## 9. Testing

```
python test_dupfinder.py
```

83 `unittest` tests against the core only (no HTTP). Each test gets a fresh
`tempfile.TemporaryDirectory`; `tearDown` also calls
`reset_app_data_dir_cache()` so the data-dir resolver doesn't leak between
tests. Coverage includes: three-tier dedup, hardlink collapsing, cancel
signals (incl. mid-large-file), empty-folder recursion + junk handling,
largest-items heap/fold correctness, dHash identical/near/dissimilar +
clustering, quarantine→restore round-trip, validate-before-delete (size/hash
change detection), protected paths, preview kinds, data-dir fallback.

The HTTP layer is verified manually (CSRF matrix, job polling, lifecycle).
There is no CI workflow yet.

---

## 10. Running from source (dev setup)

```
git clone https://github.com/DatongJin/sift.git
cd sift
python -m pip install pillow            # optional; enables similar-photos
python dupfinder_app.py                 # opens the UI
python dupfinder_app.py --no-browser    # server only
python test_dupfinder.py                # tests
python dupfinder.py <dir> --largest     # headless CLI
```

Only dependency is **Pillow**, and only for the similar-photos mode.
Everything else is Python 3.10+ standard library.

---

## 11. How to add a new scan mode (worked example)

Say you want a "stale files" mode (files not modified in N days):

1. **Core**: add `find_stale_files(roots, skip, older_than_days, min_size,
   cancel)` to `dupfinder.py` modeled on `compute_top_largest` (single walk,
   `cancel` checks, return a list of entries).
2. **Tests**: add cases to `test_dupfinder.py` (found/not-found, threshold
   boundary, cancel, skip patterns).
3. **CLI**: add `--stale` + `--older-than` args in `main()`, dispatch to a
   `_run_stale()` helper.
4. **API**: add `_handle_scan_stale` to `dupfinder_app.py`; for a long scan,
   mirror the `_run_*_job` background pattern + a `/api/scan_stale_start`
   route; otherwise a synchronous handler.
5. **UI**: add a button in the actions grid, a `state.stale*` slice, a
   `startScanStale()` (copy `startScanLargest`, swap the endpoint), a
   `renderStaleResults()`, and reuse `/api/execute` for deletion (it already
   does the safety validation).

The safety pipeline (`validate_deletion_candidates` → `quarantine_files` →
manifest → restore) is reused for free — never write a second deletion path.

---

## 12. Known limitations & future work

- **Windows-only binary.** Code is ~90 % portable; Mac/Linux need their own
  PyInstaller builds (can't cross-compile).
- **Unsigned.** Triggers SmartScreen on first launch. Code signing ≈ $300/yr.
- **HDD random-read penalty.** The 8-worker photo scan thrashes a mechanical
  disk; great on SSD/NVMe.
- **Hardlinks on inode-0 volumes** show as duplicates (safe, just cosmetic).
- **No CI.** Tests are run manually.
- **`_scan_jobs` is in-memory.** Restarting the server loses job history
  (results are already delivered to the UI by then, so low impact).
- **Candidate next features:** drill-in on largest folders, stale-files mode,
  quarantine-history UI, file-type breakdown, Chinese i18n. See DECISIONS.md
  §"Things explicitly NOT done".
