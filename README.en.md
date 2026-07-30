# Bilimanga-Downloader

**English** · [中文](README.md)

Downloader for [bilimanga.net](https://www.bilimanga.net/) manga and [linovelib](https://www.linovelib.com)
light novels, packaged per volume into **EPUB** or **PDF**.

## Pick how to use it

| | ⭐ Double-click executable | CLI / from source | 🐵 Userscript |
|---|---|---|---|
| Best for | just use it | tweak code / automate | grab it inside the browser |
| Get it | download for your OS from [Releases](../../releases), double-click | `python3 start.py` | install Tampermonkey → the script |
| UI | native window | native window, `--cli` for terminal | floating button on the page |
| Stability | solid | solid | so-so (fallback) |

Prefer the first two; the userscript is a fallback. **Chrome or Edge** must be installed.

## Double-click executable

Download and unzip from [Releases](../../releases), then double-click `Bilimanga-Downloader.app` (macOS) or
`Bilimanga-Downloader.exe` (Windows). Then: paste a link or id → Parse → tick volumes → Download.

- Manga: `https://www.bilimanga.net/detail/54.html` or id `54`
- Novel: `https://www.linovelib.com/novel/2139.html`

Files go to your Downloads folder (`~/Downloads`) by default; change it in Settings.

## CLI / from source

```bash
python3 start.py            # GUI
python3 start.py --cli      # terminal menu
python3 start.py <link or id>          # download directly
python3 start.py --out <dir> <link or id>   # output to a folder for this run
```

## Settings

- Output folder (defaults to Downloads)
- Default format EPUB / PDF
- Proxy, rate-limit / backoff-retry / resume, debug logging

Site address is shown read-only; download threads auto-adjust (the UI shows the live count).

## Layout

```
Bilimanga-Downloader/
├── start.py              # entry (GUI by default, --cli for terminal)
├── packaging/            # build script & GitHub Actions workflow
├── src/bilimanga_dl/     # source
├── userscript/           # 🐵 userscript build
└── resource/             # cover image app_cover.png
```

> For personal study / backing up public content only. Please respect the site's terms.
