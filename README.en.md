# Bilimanga-Downloader

**English** · [中文](README.md)

A manga downloader for [bilimanga.net](https://www.bilimanga.net/): scrapes the artwork and packages each
volume into **EPUB** or **PDF**.

> **The command-line / desktop app (this repo) is the recommended way.** It ships a built-in **native GUI
> window** (no browser, no local server) and also works purely from the terminal — most stable.
> The 🐵 userscript ([`userscript/`](userscript)) is **currently unstable** (breaks easily due to site
> DOM changes, cross-origin prompts and browser differences); keep it only as a fallback and switch back
> to this repo if it misbehaves.

## Three ways to use it (in recommended order)

| | ⭐ Double-click executable | ⭐ CLI / from source | 🐵 Userscript (unstable, fallback) |
|---|---|---|---|
| For | non-technical users, zero setup | terminal users / hackers | don't want to install anything |
| Runtime | **no Python**, just local Chrome/Edge | Python + local Chrome/Edge | browser + Tampermonkey |
| Get it | download for your OS from [Releases](../../releases), double-click | `python3 start.py` | install Tampermonkey → install script |
| UI | double-click opens a **native window** | native window by default, `--cli` for terminal | floating button on the page |
| Platform | macOS / Windows | macOS / Windows | any |

## 1. Double-click executable (recommended, no Python)

1. Download for your OS from [Releases](../../releases):
   - **macOS**: `Bilimanga-Downloader-macOS.zip` → unzip → double-click `Bilimanga-Downloader.app`.
   - **Windows**: `Bilimanga-Downloader-Windows.zip` → unzip → double-click `Bilimanga-Downloader.exe`.
2. It opens a **native app window** (no browser, no local server — so macOS won't keep prompting to
   "allow incoming network connections").
3. In the window: paste a book URL or id and click Parse → tick the volumes → Download. Files go to your
   **browser's Downloads folder** (`~/Downloads`) by default; change it anytime in "① Settings".

> Requires **Chrome or Edge** installed locally (used to pass Cloudflare). The first parse launches the
> browser and takes ~10–20 s. On macOS, if Gatekeeper blocks the first launch, allow it via
> System Settings → Privacy & Security → "Open Anyway".
>
> **About the macOS permission prompt on step 2 ("Parse")**: passing Cloudflare requires launching your
> local Chrome, and this app isn't Apple-signed/notarized, so macOS shows a one-time privacy prompt asking
> whether to let it access/control another app — **just click "Allow" once** and it won't ask again. If you
> accidentally clicked "Don't Allow" and it then can't reach the browser, re-enable
> **Bilimanga-Downloader** under System Settings → Privacy & Security → "App Management" (or "Automation").
> This doesn't affect downloading itself — it's just macOS's routine check for un-notarized apps.

## 2. CLI / from source

```bash
python3 start.py            # launches the native GUI window by default
python3 start.py --cli      # interactive terminal menu
python3 start.py https://www.bilimanga.net/detail/54.html   # detail URL, download directly
python3 start.py 54                                         # book id, download directly
python3 start.py --out ~/Downloads/manga 54                 # override output dir for this run
python3 start.py --debug    # debug logging
```

`start.py` auto-creates an isolated conda env `bilimanga-dl` (falls back to a project-local `.venv` if
conda is absent — neither pollutes your system), installs deps, then starts.

The CLI download is 4 steps: **confirm → parse catalog → pick chapters (e.g. `1-9,15,20-25`, Enter = all)
→ pick format**, then a pipeline produces one volume at a time.

## Settings

In the GUI "① Settings" panel, or `--cli` → Settings:

- **Site URL** — defaults to `https://www.bilimanga.net`, editable (no more built-in mirror fallback, so it
  never silently connects to a different site).
- **Output folder** — defaults to your **browser's Downloads folder** (`~/Downloads`); change it to any
  folder (output is grouped by book title).
- Default format (EPUB/PDF), concurrency, proxy, rate-limit / backoff-retry / resume, debug logging, etc.

Settings and logs live in the project dir when run from source, or in the OS app-data dir when packaged
(macOS `~/Library/Application Support/Bilimanga-Downloader`, Windows `%APPDATA%\Bilimanga-Downloader`).

## Optimizations / Features

- **Cloudflare bypass** — DrissionPage drives a local Chrome/Edge; a real browser fingerprint passes the challenge.
- **3-stage pipeline (download → validate → package)** — volumes download sequentially; the moment one
  finishes, validation + packaging run in the background, **overlapping** the next download. Total time ≈ download time.
- **Per-volume progress bars** — pick N volumes → N bars, each showing download / validate / package / done.
- **Adaptive concurrency (sampled every 1 s)** — `0 → +1`, `<40% → −1`, `≥40% → −2`; graceful shrink that never aborts in-flight downloads.
- **AVIF/WebP → JPEG** — site artwork is often AVIF (e.g. book 54); converted to JPEG for EPUB/PDF compatibility, keeping original pixels.
- **Lazy-load fix** — waits for `imagecontent` and retries so later volumes never come up empty.
- **Resume** — temp images in `temp/download/<title>/<chapter>/`; re-runs skip what's done, temp cleaned after packaging.
- **Three input forms** (detail · catalog · id) **/ EPUB & PDF** (PDF lays out each image full-page, no distortion or padding).

## Layout

```
Bilimanga-Downloader/
├── start.py              # entry: GUI by default, --cli for terminal
├── docs/                 # screenshots
├── packaging/            # PyInstaller scripts & GitHub Actions workflow
├── src/
│   ├── requirements.txt
│   └── bilimanga_dl/     # source (net / scraper / downloader / build_* / cli / gui / ui …)
├── userscript/           # 🐵 userscript build (fallback, unstable)
└── (generated at runtime) config / logs / temp / your output folder
```

## Requirements

- **Chrome or Edge** must be installed locally (DrissionPage drives it to pass Cloudflare).
- From source, other Python deps are in `src/requirements.txt` (`start.py` installs them); executables bundle everything.

> For personal study / backing up public content only. Please respect the site's terms.
