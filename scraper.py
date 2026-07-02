"""
scraper.py
----------
Responsible for crawling a single web page and extracting every discoverable
image URL from it:
    - <img src="...">
    - <img srcset="...">
    - data-src / data-lazy-src / data-original (lazy-load attributes)
    - inline style="background-image:url(...)"
    - <style> blocks with background-image: url(...)
    - JS-rendered content (via Playwright), including scroll-triggered
      lazy-loaded images.

The scraper tries a fast static HTML fetch first (requests + BeautifulSoup).
It then ALSO runs a Playwright-driven dynamic pass to catch JS-rendered /
lazy-loaded images, and merges + deduplicates results from both passes.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from typing import Iterable

import requests
from bs4 import BeautifulSoup

from utils import (
    CSV_PATH,
    extract_largest_from_srcset,
    extract_size_hint,
    extract_urls_from_css,
    log_error,
    log_success,
    looks_like_image_url,
    make_absolute,
    normalize_url_key,
)

LAZY_LOAD_ATTRS = (
    "data-src",
    "data-lazy-src",
    "data-original",
    "data-srcset",
    "data-lazy",
    "data-bg",
)

REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}

REQUEST_TIMEOUT = 15  # seconds


@dataclass
class ScraperResult:
    """Container for the outcome of a scrape operation."""
    base_url: str
    image_urls: list[str] = field(default_factory=list)


class WebsiteImageScraper:
    """Extracts image URLs from a webpage, combining static + dynamic passes."""

    def __init__(self, url: str, use_dynamic: bool = True, scroll_pause: float = 1.0):
        self.url = url
        self.use_dynamic = use_dynamic
        self.scroll_pause = scroll_pause
        # Maps a normalized dedup key (size/quality query params stripped)
        # -> (best_url_seen_so_far, size_hint_of_that_url). This is how we
        # collapse Shopify/CDN responsive variants (?width=200, ?width=800,
        # etc.) into a single unique image, keeping the highest-res variant.
        self._found: dict[str, tuple[str, int]] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def scrape(self) -> ScraperResult:
        """Run the full scrape (static + optional dynamic) and return results."""
        html = self._fetch_static_html()
        if html:
            self._parse_html(html, self.url)

        if self.use_dynamic:
            try:
                self._scrape_dynamic()
            except Exception as exc:  # noqa: BLE001 - we want to degrade gracefully
                log_error(self.url, "dynamic_scrape", str(exc))
                print(f"[!] Dynamic (JS) scraping failed, continuing with static results: {exc}")

        cleaned = sorted(url for url, _ in self._found.values())
        return ScraperResult(base_url=self.url, image_urls=cleaned)

    def save_to_csv(self, image_urls: Iterable[str], csv_path=CSV_PATH) -> int:
        """Save discovered image URLs to image.csv. Returns number of rows written."""
        count = 0
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["Serial Number", "Image URL"])
            for i, url in enumerate(image_urls, start=1):
                writer.writerow([i, url])
                count += 1
        return count

    # ------------------------------------------------------------------
    # Static (requests + BeautifulSoup) pass
    # ------------------------------------------------------------------
    def _fetch_static_html(self) -> str | None:
        try:
            resp = requests.get(self.url, headers=REQUEST_HEADERS, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            log_success(self.url, "fetch_static", f"status={resp.status_code}")
            return resp.text
        except requests.RequestException as exc:
            log_error(self.url, "fetch_static", str(exc))
            print(f"[!] Static fetch failed: {exc}")
            return None

    def _parse_html(self, html: str, base_url: str) -> None:
        soup = BeautifulSoup(html, "html.parser")

        # 1) <img> tags: src, srcset, lazy-load attrs
        for img in soup.find_all("img"):
            self._collect_from_img_tag(img, base_url)

        # 2) <source> tags inside <picture>
        for source in soup.find_all("source"):
            srcset = source.get("srcset") or source.get("data-srcset")
            if srcset:
                largest = extract_largest_from_srcset(srcset)
                self._add(make_absolute(base_url, largest))
            src = source.get("src")
            if src:
                self._add(make_absolute(base_url, src))

        # 3) Inline style="background-image:url(...)" on any tag
        for tag in soup.find_all(style=True):
            for candidate in extract_urls_from_css(tag["style"]):
                self._add(make_absolute(base_url, candidate))

        # 4) <style> blocks with background-image: url(...)
        for style_tag in soup.find_all("style"):
            css_text = style_tag.string or ""
            for candidate in extract_urls_from_css(css_text):
                self._add(make_absolute(base_url, candidate))

        # 5) <link rel="icon"/"apple-touch-icon"> as a bonus (favicons are images too)
        for link in soup.find_all("link", rel=True):
            rels = [r.lower() for r in link.get("rel", [])]
            if any("icon" in r for r in rels):
                href = link.get("href")
                if href:
                    self._add(make_absolute(base_url, href))

    def _collect_from_img_tag(self, img, base_url: str) -> None:
        # Standard src
        src = img.get("src")
        if src:
            self._add(make_absolute(base_url, src))

        # srcset (multiple candidate URLs with width/density descriptors) --
        # keep only the largest variant, since the rest are the same image.
        srcset = img.get("srcset")
        if srcset:
            largest = extract_largest_from_srcset(srcset)
            self._add(make_absolute(base_url, largest))

        # Lazy-load attributes
        for attr in LAZY_LOAD_ATTRS:
            value = img.get(attr)
            if not value:
                continue
            if attr.endswith("srcset"):
                largest = extract_largest_from_srcset(value)
                self._add(make_absolute(base_url, largest))
            else:
                self._add(make_absolute(base_url, value))

    # ------------------------------------------------------------------
    # Dynamic (Playwright) pass
    # ------------------------------------------------------------------
    def _scrape_dynamic(self) -> None:
        """Render the page with a real browser, scroll to trigger lazy-load,
        then re-extract image URLs from the fully rendered DOM."""
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(user_agent=REQUEST_HEADERS["User-Agent"])

            try:
                page.goto(self.url, wait_until="networkidle", timeout=30000)
            except Exception:
                # Fall back to a less strict wait condition for slow/streaming pages
                page.goto(self.url, wait_until="domcontentloaded", timeout=30000)

            self._auto_scroll(page)

            # Let any final lazy-loaded images settle
            page.wait_for_timeout(int(self.scroll_pause * 1000))

            rendered_html = page.content()
            self._parse_html(rendered_html, self.url)

            # Also grab computed background-image styles JS may have set directly
            bg_urls = page.eval_on_selector_all(
                "*",
                """
                (elements) => elements
                    .map(el => getComputedStyle(el).backgroundImage)
                    .filter(bg => bg && bg.includes('url('))
                """,
            )
            for css_value in bg_urls:
                for candidate in extract_urls_from_css(css_value):
                    self._add(make_absolute(self.url, candidate))

            browser.close()
            log_success(self.url, "fetch_dynamic", "rendered + scrolled")

    @staticmethod
    def _auto_scroll(page) -> None:
        """Scroll to the bottom of the page incrementally to trigger lazy loading."""
        page.evaluate(
            """
            async () => {
                await new Promise((resolve) => {
                    let totalHeight = 0;
                    const distance = 400;
                    const timer = setInterval(() => {
                        const scrollHeight = document.body.scrollHeight;
                        window.scrollBy(0, distance);
                        totalHeight += distance;
                        if (totalHeight >= scrollHeight || totalHeight > 20000) {
                            clearInterval(timer);
                            resolve();
                        }
                    }, 200);
                });
            }
            """
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _add(self, url: str | None) -> None:
        """Add a discovered URL, collapsing CDN/Shopify size variants.

        Image CDNs (Shopify included) commonly serve the *same* image at
        many sizes via query params like ?width=200, ?width=800, etc.
        We dedupe on a normalized key (those params stripped) and, among
        duplicates, keep whichever URL has the largest size hint -- so the
        final list contains one entry per unique image, pointing at its
        highest-resolution variant.
        """
        if not url:
            return
        if not looks_like_image_url(url):
            return

        key = normalize_url_key(url)
        size_hint = extract_size_hint(url)

        existing = self._found.get(key)
        if existing is None or size_hint > existing[1]:
            self._found[key] = (url, size_hint)
