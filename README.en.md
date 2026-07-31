# Bilimanga-Downloader

**English** · [中文](README.md)

Download manga from [bilimanga.net](https://www.bilimanga.net/) and light novels from [bilinovel](https://www.bilinovel.com), packaged per volume into **EPUB** or **PDF** for your e-reader.

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
5. When it finishes, the files are in your **Downloads folder** (`~/Downloads`), organized by book title.

After one book finishes, the window returns to the start so you can paste the next one; type `q` to quit.

> Requires **Python 3.9+**. If double-click does nothing, see "Double-click won't open" below.

## Using the interface

Each step is guided, with a hint line below it:

- **Paste a URL**: the type (manga vs novel) is **auto-detected** from the domain — no need to choose first. You can also type just a book id (e.g. `703`), in which case it asks once which type it is.
- **Confirm the book**: shows title, author, and volume count. Enter to confirm, `n` to go back.
- **Select volumes**:
  - Option 1 — **cursor checkboxes**: ↑↓ to move, Space to toggle, Enter to confirm.
  - Option 2 — **type a range**: e.g. `1-9,15,20-25` (comma-separated, `a-b` for a run); Enter alone selects all.
- **Choose format** (manga only): EPUB or PDF; novels are always EPUB.
- Type `b` at any step to **go back**.

At the start prompt, `s` opens **settings** and `q` quits.

## Settings

Type `s` at the start prompt. Press Enter to keep a value, or type a new one; changes are saved automatically:

- **Output directory**: defaults to your Downloads folder (`~/Downloads`); change it to any directory.
- **Default format**: EPUB or PDF (manga only).
- **Proxy**: auto by default (follows system env vars, falls back to direct if unreachable); or set `http://127.0.0.1:7890` to force a proxy.
- **Rate limit / retry / resume**: defaults are fine for most cases; with resume on, re-downloading a book skips files already fetched.
- **Debug logging**: turn on to write detailed logs to `logs/` when troubleshooting.

## How downloads work

- Both manga and novels **connect directly by default** — fast and light on memory. Only when a site occasionally shows a human check does it **auto-launch your local Chrome / Edge** to clear it once, then reuse it. Having **Chrome or Edge** installed as a fallback is recommended, but usually unused.
- The download thread count auto-tunes to your network; if the site rate-limits, it slows down and recovers automatically to avoid missing pages.

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
├── run.command           # macOS double-click launcher
├── run.bat               # Windows double-click launcher
├── src/bilimanga_dl/     # source
└── resource/             # assets
```

> For personal study and backup of public content only; please follow each site's terms.
