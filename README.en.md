# Bilimanga-Downloader

**English** · [中文](README.md)

A manga downloader for [bilimanga.net](https://www.bilimanga.net/). Cross-platform (macOS / Windows),
pure Python: drives a real browser to pass Cloudflare, scrapes the artwork, and packages each
volume into **EPUB** or **PDF**.

## Demo (example: book 703《與妳相戀到生命盡頭》)

One command runs the whole flow: confirm → parse catalog → pick chapters → pick format → download & build.

![run flow](docs/run-flow.png)

**Each selected volume gets its own progress bar**, moving through
`download → 🔍 validate → 📦 package → ✓ done`. Volumes download one at a time; as soon as a volume
finishes downloading it is validated + packaged in the background (overlapping the next volume's
download), so EPUBs appear one by one:

![per-volume progress](docs/run-progress.png)

## Quick start

```bash
python3 start.py
```

`start.py` auto-creates an isolated conda env `bilimanga-dl` (falls back to a project-local `.venv` if
conda is absent — neither pollutes your system), installs deps, then opens the CLI. You can also pass an
argument directly:

```bash
python3 start.py https://www.bilimanga.net/detail/703.html   # detail-page URL
python3 start.py https://www.bilimanga.net/read/703/catalog  # catalog URL
python3 start.py 703                                         # book id
python3 start.py --gui      # GUI (optional)
python3 start.py --debug    # debug logging
```

## Flow (CLI, 4 steps)

1. **Confirm** — opens the book's page in your default browser for a visual check; press Enter / y
   (the automation browser is warming up in the background).
2. **Parse catalog** — a real browser passes Cloudflare and scrapes the table of contents.
3. **Pick chapters** — listed as `index + title + which episodes`; type e.g. `1-9,15,19,20-25`
   (Enter = all).
4. **Pick format** — EPUB / PDF, then the download pipeline runs, producing one volume at a time.

## Optimizations / Features

- **Cloudflare bypass** — DrissionPage drives a local Chrome/Edge; a real browser fingerprint passes
  the challenge.
- **3-stage pipeline (download → validate → package)** — volumes download sequentially; the moment a
  volume finishes, validation + packaging run in the background, **overlapping** the next volume's
  download. EPUBs are produced volume by volume; total time ≈ download time.
- **Per-volume progress bars** — pick N volumes → N bars, each showing download / validate / package /
  done at a glance.
- **Adaptive concurrency (sampled every 1 s)** — starts with a few workers and adjusts each second by
  that second's error rate: `0 → +1`, `<40% → −1`, `≥40% → −2` (hard backoff when rate-limited).
  **Shrinking is graceful**: it only lowers the ceiling and never aborts in-flight downloads — busy
  threads finish their current image and simply aren't given new work (no re-downloading, no waste).
- **Lazy-load fix** — reader-page images are injected by JS; the scraper waits for `imagecontent` and
  retries, so later volumes never come up empty.
- **Resume** — temp images live in `temp/download/<title>/<chapter>/`; re-runs skip what's already
  downloaded, and temp is cleaned after packaging.
- **Local validation (offline)** — the validate stage only checks for missing/empty files; gaps are fed
  back to the download stage, no re-crawling.
- **Per-volume cover** — each EPUB's cover is that volume's first image (distinct per volume); series /
  volume metadata is written for reader-app grouping.
- **No special network config** — the browser uses your system's network as-is; only the local CDP
  connection bypasses the proxy (otherwise the tool can't reach the browser when a proxy is on).
- **Mirror fallback / three input forms (detail · catalog · id) / EPUB & PDF** (PDF lays out each image
  full-page, no distortion or padding).

## Layout

```
Bilimanga-Downloader/
├── start.py              # single entry point
├── docs/                 # screenshots
├── src/
│   ├── requirements.txt
│   └── bilimanga_dl/     # source (net / scraper / downloader / build_* / cli / ui …)
├── config/setting.json   # generated at runtime
├── logs/  temp/  downloads/<title>/   # generated at runtime (gitignored)
```

## Requirements

- **Chrome or Edge** must be installed locally (DrissionPage drives it to pass Cloudflare).
- Other Python deps are in `src/requirements.txt`; `start.py` installs them automatically.

> For personal study / backing up public content only. Please respect the site's terms.
