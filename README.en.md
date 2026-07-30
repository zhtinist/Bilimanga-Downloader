# Bilimanga-Downloader

**English** · [中文](README.md)

Downloader for [bilimanga.net](https://www.bilimanga.net/) manga and [linovelib](https://www.linovelib.com)
light novels, packaged per volume into **EPUB** or **PDF**.

## Pick how to use it

| | ⭐ CLI / from source | Double-click executable | 🐵 Userscript |
|---|---|---|---|
| Best for | just use it / tweak code / automate | double-click to run | grab it inside the browser |
| Get it | `python3 start.py` | download for your OS from [Releases](../../releases), double-click | install Tampermonkey → the script |
| UI | terminal interface | native window | floating button on the page |
| Status | recommended | on hold (macOS permission issues) | maintenance paused (fallback) |

CLI / from source is the recommended path; the double-click executable is on hold and the userscript is no longer
maintained. **Chrome or Edge** must be installed.

## CLI / from source

No conda needed — `python3 start.py` creates a project-local `.venv` and installs dependencies automatically.

```bash
python3 start.py                            # open the terminal interface
python3 start.py <link or id>               # download directly
python3 start.py --out <dir> <link or id>   # output to a folder for this run
```

In the terminal interface: first pick Manga / Novel, then enter a link or id, then select chapters interactively
(arrow keys to tick or type a range); you can step back at any point.

- Manga: `https://www.bilimanga.net/detail/54.html` or id `54`
- Novel: `https://www.linovelib.com/novel/2139.html`

Files go to your Downloads folder (`~/Downloads`) by default; use `--out` to override.

## Settings

- Output folder (defaults to Downloads, or set with `--out`)
- Default format EPUB / PDF
- Proxy, rate-limit / backoff-retry / resume, debug logging

Download threads auto-adjust (the terminal shows the live count). Novels default to a direct connection to the mobile
site (faster) and fall back to the browser on errors; manga still needs a local Chrome / Edge (to pass Cloudflare).

## Layout

```
Bilimanga-Downloader/
├── start.py              # entry (terminal interface)
├── packaging/            # build script & GitHub Actions workflow
├── src/bilimanga_dl/     # source
├── userscript/           # 🐵 userscript build (maintenance paused)
└── resource/             # cover image app_cover.png
```

> For personal study / backing up public content only. Please respect the site's terms.
