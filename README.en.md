# Bilimanga-Downloader

**English** · [中文](README.md)

Downloader for [bilimanga.net](https://www.bilimanga.net/) manga and [bilinovel](https://www.bilinovel.com) light novels, packaged per volume into **EPUB** or **PDF**. A **pure-Python command-line tool** — cross-platform (macOS / Windows / Linux), no compilation needed.

## Requirements

- **Python 3.9+**
- For **manga**, a local **Chrome or Edge** is also required (to pass Cloudflare); light novels use a direct mobile-site connection and usually need no browser.

## Quick start

```bash
python3 start.py                          # terminal interface
python3 start.py <url-or-id>              # download directly
python3 start.py --out <dir> <url-or-id>  # output to a specific dir
python3 start.py --debug                   # verbose logging
```

The first run **auto-creates a project-local `.venv` and installs deps** (no conda, no global pollution).

You can also **double-click to launch**:

- macOS: double-click `run.command`
- Windows: double-click `run.bat`

> `run.bat` runs `chcp 65001` to switch the console to UTF-8, so Chinese text won't be garbled.

## Usage

In the terminal, just **paste a manga or novel URL** (detail or catalog page). The type is **auto-detected from the domain** — no need to pick first; a bare book id will prompt once for the type. Then select chapters interactively (arrow keys to check, or type a range like `1-9,15`); every step can go back.

- Manga: `https://www.bilimanga.net/detail/703.html` or id `703`
- Novel: `https://www.bilinovel.com/novel/2139.html` or id `2139`

Downloads default to your system Downloads folder `~/Downloads`; use `--out` to change it. At the entry prompt, `s` opens settings and `q` quits.

## Settings

- Output directory (defaults to `~/Downloads`, or `--out`)
- Default format EPUB / PDF (manga only; novels are always EPUB)
- Proxy, rate-limit / backoff-retry / resume, debug logging

Thread count auto-tunes (the terminal shows the live count).

## Layout

```
Bilimanga-Downloader/
├── start.py              # entry (terminal interface, auto .venv)
├── run.command           # macOS double-click launcher
├── run.bat               # Windows double-click launcher
├── src/bilimanga_dl/     # source
└── resource/             # assets
```

> For personal study and backup of public content only; please follow each site's terms.
