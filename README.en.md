# Bilimanga-Downloader

**English** · [中文](README.md)

Download manga from [bilimanga.net](https://www.bilimanga.net/) and light novels from [bilinovel](https://www.bilinovel.com), packaged per volume into **EPUB** or **PDF** for your e-reader.

Two ways to use it:

- **🐵 Userscript (no Python needed)**: install a browser extension and click to download on any book page.
  → [Install on Greasy Fork](https://greasyfork.org/zh-CN/scripts/588995-bilimanga-%E6%BC%AB%E7%94%BB-%E8%BD%BB%E5%B0%8F%E8%AF%B4%E4%B8%8B%E8%BD%BD%E5%99%A8) (see [`userscript/`](userscript/README.md))
- **💻 Command line (below)**: pure Python, great for batch downloads and automation.

## Quick start

1. Download `Bilimanga-Downloader-CLI.zip` from [Releases](../../releases) and unzip it.
2. Open the app:
   - **macOS**: double-click `run.command`
   - **Windows**: double-click `run.bat`

   A terminal window opens. On the first run it sets up what it needs automatically (give it a moment).
3. **Paste a manga or novel URL** into the window and press Enter. For example:
   - Manga: `https://www.bilimanga.net/detail/703.html`
   - Novel: `https://www.bilinovel.com/novel/2139.html`
4. Confirm the title, then pick the volumes to download with **↑↓ to move, Space to check** (or type a range like `1-5,8`), and press Enter.
5. The book is **added to a background download queue** and the window returns to the start immediately — you can **paste the next one to queue it too**. Finished files land in your **Downloads folder** (`~/Downloads`), organized by book title.

No need to wait for one book to finish before starting another: pick volumes, return to the start, keep adding — the queue downloads them in order (FIFO). Type `q` to quit (if the queue isn't done, it asks whether to wait or exit).

> Requires **Python 3.9+**. If double-click does nothing, see "Double-click won't open" below.

## Using the interface

Each step is guided, with a hint line below it:

- **Paste a URL**: the type (manga vs novel) is **auto-detected** from the domain — no need to choose first. You can also type just a book id (e.g. `703`), in which case it asks once which type it is.
- **Confirm the book**: shows title, author, and volume count. Enter to confirm, `n` to go back.
- **Select volumes**:
  - Option 1 — **cursor checkboxes**: ↑↓ to move, Space to toggle, Enter to confirm.
  - Option 2 — **type a range**: e.g. `1-9,15,20-25` (comma-separated, `a-b` for a run); Enter alone selects all.
- **Choose format** (manga only): EPUB or PDF; novels are always EPUB. Then pick **where to save**: local / Baidu Netdisk / OneDrive.
- Type `b` at any step to **go back**.

At the start prompt: `s` settings, `c` connect Baidu Netdisk, `o` connect OneDrive, `p` view the download queue, `q` quit.

## Cloud upload (optional)

Downloads can go to your own cloud instead of local disk; the top of the start screen shows connection status.

- **Baidu Netdisk** — type `c`, a browser opens to log in; the script reuses that session. Then pick "☁ Baidu" when downloading. (Unofficial cookie-based API — against Baidu's ToS and may break; credentials stay local.)
- **OneDrive** — type `o`, log in with the official Microsoft **device-code** flow (**zero registration** — just sign in to your own Microsoft account and consent). Uploads to your own `OneDrive/bilidownloader/manga|novel/title/`. See **[docs/onedrive.md](docs/onedrive.md)**. Company/school accounts may be blocked by an admin policy — use a personal Microsoft account.

## Settings

Type `s` at the start prompt. Press Enter to keep a value, or type a new one; changes are saved automatically:

- **Output directory**: defaults to your Downloads folder (`~/Downloads`); change it to any directory.
- **Default format**: EPUB or PDF (manga only).
- **Concurrency**: start and max thread counts (manga uses adaptive concurrency between them).
- **Proxy**: auto by default (follows system env vars, falls back to direct if unreachable); or set `http://127.0.0.1:7890` to force a proxy.
- **Rate limit / retry / resume**: defaults are fine; with resume on, re-downloading skips volumes already saved/uploaded (see "duplicate skip" below).
- **Baidu Netdisk / OneDrive**: upload root path, disconnect, and (OneDrive) an optional custom `client_id`.
- **Debug logging**: writes detailed logs to `logs/`; with `--debug` it also writes a `log.txt` into the download directory.

## How downloads work

- Both manga and novels **connect directly by default** — fast and light on memory. Only when a site occasionally shows a human check does it **auto-launch your local Chrome / Edge** to clear it once, then reuse it. Having **Chrome or Edge** installed as a fallback is recommended, but usually unused.
- The download thread count auto-tunes to your network; if the site rate-limits, it slows down and recovers automatically to avoid missing pages.
- **Duplicate files are skipped**: before downloading each volume, it checks whether the **target location** (local / your Baidu / your OneDrive) already has that volume (matched by `title - volume.epub|pdf`). If so it **skips the whole volume** — no re-download, no overwrite. To update a volume, **delete that file first**; to force a full re-download, turn off resume in settings.

## Command-line usage (advanced)

If you prefer the command line or want automation, run `start.py` directly (the first run auto-creates a project-local `.venv` and installs deps, without touching your system):

```bash
python3 start.py                             # open the interface (same as double-click)
python3 start.py <url-or-id>                 # jump straight to confirm/select for this book
python3 start.py --out ~/Books <url-or-id>   # save this download to a specific dir
python3 start.py --debug                      # enable debug logging
```

- `--out <dir>`: output to a directory for this run only (not saved to settings).
- `--debug`: verbose logs for troubleshooting.
- URLs can be a detail page, a catalog page, or just the book id.

## Double-click won't open?

- **macOS**: if blocked on first launch ("cannot verify the developer"), go to System Settings → Privacy & Security and click "Open Anyway"; or right-click `run.command` → Open.
- Requires **Python 3.9+**. Run `python3 --version` in a terminal to check.
- The Windows `run.bat` already switches the console to UTF-8, so Chinese text isn't garbled.

## Layout

```
Bilimanga-Downloader/
├── start.py              # entry (first run auto-creates .venv, installs deps)
├── run.command / run.bat # macOS / Windows double-click launchers
├── docs/onedrive.md      # OneDrive setup guide
├── userscript/           # 🐵 userscript version
└── src/bilimanga_dl/
    ├── core/             # net / rate-limit / logging / images / plugin registry
    ├── sources/          # content sources (manga / novel)
    ├── packagers/        # packagers (EPUB / PDF)
    ├── storage/          # storage targets (local / Baidu / OneDrive)
    └── ui/               # terminal UI (steps, picker, settings, download queue)
```

> Plugin architecture: add a new site / format / cloud = one new plugin file, registered — no changes elsewhere.

> For personal study and backup of public content only; please follow each site's terms.
