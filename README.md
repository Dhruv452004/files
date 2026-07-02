# Website Image Scraper, Downloader & PNG Converter

Extracts every image from a given website, saves the URLs to `image.csv`,
downloads them concurrently, and converts them all to PNG.

## Project Structure

```
project/
├── main.py            # CLI entry point (orchestrates the pipeline)
├── scraper.py          # Static (requests/BS4) + dynamic (Playwright) scraping
├── downloader.py        # Async concurrent downloader with retries
├── converter.py          # PNG conversion (Pillow + cairosvg for SVG)
├── utils.py             # Logging, URL helpers, shared constants
├── image.csv             # Generated: Serial Number, Image URL
├── images/               # Generated: final image_001.png, image_002.png ...
├── logs/
│   ├── success.log        # Generated: timestamped success events
│   └── error.log           # Generated: timestamped failures
└── requirements.txt
```

## Installation

1. Use Python 3.11 or newer.
2. Create a virtual environment (recommended):
   ```bash
   python3 -m venv venv
   source venv/bin/activate      # Windows: venv\Scripts\activate
   ```
3. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
4. Install the Playwright browser binary (required once, for dynamic/JS-rendered scraping):
   ```bash
   playwright install chromium
   ```
   Linux users may also need OS-level dependencies; if `playwright install`
   warns about missing libraries, run:
   ```bash
   playwright install-deps chromium
   ```
5. (Optional, for SVG conversion) `cairosvg` needs the Cairo graphics
   library installed at the OS level:
   - Debian/Ubuntu: `sudo apt-get install libcairo2`
   - macOS: `brew install cairo`
   - Windows: see https://www.cairographics.org or use WSL.
   If Cairo isn't available, SVGs will simply fail conversion (logged to
   `error.log`) — everything else still works.

## Usage

Interactive mode:
```bash
python main.py
```
```
Enter Website URL: https://example.com
[✓] Found 152 images
[✓] image.csv created
[✓] Downloading images...
[✓] Converting to PNG...
[✓] Completed successfully
```

Non-interactive mode:
```bash
python main.py --url https://example.com
```

Skip the JavaScript-rendering pass (faster, static HTML only):
```bash
python main.py --url https://example.com --no-dynamic
```

## How It Works

1. **Scraping (`scraper.py`)**
   - Fetches the raw HTML with `requests`.
   - Parses it with BeautifulSoup, extracting URLs from `<img src>`,
     `<img srcset>`, `<source>` tags, `data-src` / `data-lazy-src` /
     `data-original` lazy-load attributes, inline `style="background-image:
     url(...)"`, `<style>` blocks, and `<link rel="icon">`.
   - Then launches a headless Chromium browser via Playwright, waits for the
     page to render, auto-scrolls to the bottom to trigger lazy-loaded
     content, and re-parses the fully rendered DOM (plus computed
     `background-image` styles) to catch anything JavaScript added.
   - All URLs are converted to absolute form, deduplicated, and filtered to
     drop obviously non-image links (`.js`, `.css`, `.pdf`, fonts, etc.).
   - Results are written to `image.csv` as `Serial Number,Image URL`.

2. **Downloading (`downloader.py`)**
   - Reads `image.csv`.
   - Downloads all images concurrently using `aiohttp`, bounded by a
     semaphore (default 10 concurrent downloads) to avoid overwhelming the
     target server.
   - Each download retries up to 3 times with exponential backoff on
     failure (network errors, timeouts, non-200 responses, empty bodies).
   - A `tqdm` progress bar shows live download progress.
   - The real file extension is determined from the `Content-Type` response
     header (falling back to the URL's extension) and the raw file is saved
     as `images/raw_XXX.<ext>`.

3. **Converting (`converter.py`)**
   - Every successfully downloaded raw file is converted to PNG using
     Pillow, preserving the alpha channel when the source has transparency
     (RGBA/LA modes, or palette images with a transparency index).
   - Animated formats (GIF, animated WebP, multi-page TIFF) use their first
     frame, since PNG is a static format.
   - SVGs are rasterized to PNG using `cairosvg`.
   - Final files are named `image_001.png`, `image_002.png`, ... matching
     the original CSV serial number, and saved into `images/`. The
     intermediate raw file is deleted after a successful conversion.

## Error Handling

- **Network failures** (DNS errors, timeouts, connection resets) during
  scraping or downloading are caught, logged to `logs/error.log` with a
  timestamp, and do not crash the pipeline — other images continue
  processing.
- **Retry logic**: each image download is retried up to `MAX_RETRIES` (3)
  times with increasing backoff before being marked as failed.
- **Invalid/irrelevant URLs** (data URIs, `javascript:` links, fonts,
  stylesheets, etc.) are filtered out before being added to `image.csv`.
- **Dynamic scraping failures** (e.g., Playwright/Chromium not installed,
  page timeout) degrade gracefully — the pipeline falls back to whatever
  was found in the static HTML pass instead of stopping entirely.
- **Conversion failures** (corrupted image data, missing `cairosvg` for
  SVG) are caught per-file, logged, and skipped without affecting other
  images.
- **Logs**: every success and failure throughout scraping, downloading, and
  converting is recorded with a timestamp in `logs/success.log` or
  `logs/error.log` respectively, including the source image URL and the
  stage at which the event occurred.

## Configuration

Tunable constants live near the top of each module:
- `downloader.py`: `MAX_CONCURRENT_DOWNLOADS`, `MAX_RETRIES`, `RETRY_BACKOFF_SECONDS`
- `scraper.py`: `REQUEST_TIMEOUT`, lazy-load attribute list (`LAZY_LOAD_ATTRS`)

## Notes & Limitations

- Some sites block automated requests/bots (Cloudflare, captchas) — in
  those cases scraping may return zero or partial results regardless of
  the method used.
- SVG conversion requires the system Cairo library; without it, SVGs are
  skipped (logged) but everything else still completes.
- Only the page you provide is crawled (single page, not a full-site
  crawl/spider across multiple pages).
