"""
utils.py
--------
Shared utility helpers used across the project:
- Logging configuration (success.log / error.log)
- URL normalization / validation helpers
- Misc small helpers (filename safety, mime->extension mapping)
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlsplit, urlunsplit

# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent
LOGS_DIR = PROJECT_ROOT / "logs"
IMAGES_DIR = PROJECT_ROOT / "images"
CSV_PATH = PROJECT_ROOT / "image.csv"

LOGS_DIR.mkdir(exist_ok=True)
IMAGES_DIR.mkdir(exist_ok=True)


# --------------------------------------------------------------------------
# Logging setup
# --------------------------------------------------------------------------
def _build_logger(name: str, filename: str, level: int) -> logging.Logger:
    """Create an isolated logger that writes to its own file with timestamps."""
    logger = logging.getLogger(name)
    logger.setLevel(level)
    logger.propagate = False  # don't double-print to root logger

    if not logger.handlers:  # avoid duplicate handlers on re-import
        handler = logging.FileHandler(LOGS_DIR / filename, encoding="utf-8")
        formatter = logging.Formatter(
            fmt="%(asctime)s | %(levelname)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        handler.setFormatter(formatter)
        logger.addHandler(handler)

    return logger


success_logger = _build_logger("success_logger", "success.log", logging.INFO)
error_logger = _build_logger("error_logger", "error.log", logging.ERROR)


def log_success(url: str, stage: str, message: str = "OK") -> None:
    """Log a successful step (download/convert/etc.) for a given image URL."""
    success_logger.info(f"[{stage}] {url} -> {message}")


def log_error(url: str, stage: str, message: str) -> None:
    """Log a failed step (download/convert/etc.) for a given image URL."""
    error_logger.error(f"[{stage}] {url} -> {message}")


# --------------------------------------------------------------------------
# URL helpers
# --------------------------------------------------------------------------
VALID_IMAGE_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".webp", ".gif",
    ".bmp", ".tiff", ".tif", ".svg",
}

DATA_URI_PREFIX = "data:image"


def make_absolute(base_url: str, possibly_relative_url: str) -> Optional[str]:
    """Convert a relative URL to an absolute URL using the page's base URL.

    Returns None for invalid / unusable inputs (data URIs, javascript:, empty).
    """
    if not possibly_relative_url:
        return None

    url = possibly_relative_url.strip()

    if not url or url.lower().startswith(("javascript:", "about:", DATA_URI_PREFIX)):
        return None

    try:
        absolute = urljoin(base_url, url)
    except ValueError:
        return None

    parsed = urlparse(absolute)
    if parsed.scheme not in ("http", "https"):
        return None

    return absolute


def looks_like_image_url(url: str) -> bool:
    """Heuristic check to filter out obviously non-image URLs.

    Many image URLs have query strings (e.g. CDN resize params) so we check
    the path's extension OR allow it through if it contains common image
    hints, since some valid image URLs lack a file extension entirely
    (e.g. signed CDN URLs). We only hard-reject known non-image extensions.
    """
    parsed = urlparse(url)
    path = parsed.path.lower()
    ext = Path(path).suffix

    # Hard reject obviously non-image file types
    non_image_exts = {
        ".html", ".htm", ".php", ".js", ".css", ".json", ".xml",
        ".pdf", ".woff", ".woff2", ".ttf", ".eot", ".otf",
        ".mp4", ".webm", ".mov", ".avi", ".mkv", ".m3u8", ".ts",
        ".mp3", ".wav", ".ogg",
    }
    if ext in non_image_exts:
        return False

    return True


def safe_extension_from_url(url: str) -> str:
    """Extract a usable file extension from a URL, defaulting to '.jpg'."""
    parsed = urlparse(url)
    ext = Path(parsed.path).suffix.lower()
    if ext in VALID_IMAGE_EXTENSIONS:
        return ext
    return ".jpg"  # safe fallback; actual format will be re-detected on download


def extract_urls_from_srcset(srcset: str) -> list[str]:
    """Parse a `srcset` attribute string into a list of candidate URLs.

    srcset format example: "img1.jpg 1x, img2.jpg 2x" or "img1.jpg 480w, img2.jpg 800w"
    """
    if not srcset:
        return []
    urls = []
    for part in srcset.split(","):
        part = part.strip()
        if not part:
            continue
        # Split on whitespace and take the first token (the URL)
        candidate = part.split()[0]
        if candidate:
            urls.append(candidate)
    return urls


def extract_largest_from_srcset(srcset: str) -> Optional[str]:
    """Pick only the single largest image variant out of a srcset string.

    Responsive `srcset` attributes typically list multiple sizes of the
    *same* image (e.g. "img.jpg?w=200 200w, img.jpg?w=800 800w"). We only
    want the highest-resolution variant, not every size as a separate image.
    Descriptors can be width-based ("800w") or density-based ("2x"); both
    are compared on a common numeric scale so the largest wins.
    """
    if not srcset:
        return None

    best_url: Optional[str] = None
    best_score = -1.0

    for part in srcset.split(","):
        part = part.strip()
        if not part:
            continue
        tokens = part.split()
        url = tokens[0]
        descriptor = tokens[1] if len(tokens) > 1 else ""

        score = 0.0
        if descriptor.endswith("w"):
            try:
                score = float(descriptor[:-1])
            except ValueError:
                score = 0.0
        elif descriptor.endswith("x"):
            try:
                # Density descriptors (1x, 2x, 3x) -> scale up so they compare
                # sensibly against width descriptors from other tags.
                score = float(descriptor[:-1]) * 1000
            except ValueError:
                score = 0.0

        if url and score >= best_score:
            best_score = score
            best_url = url

    return best_url


# Query params that only describe a *size/quality variant* of the same
# underlying image (Shopify CDN, most other image CDNs use these too).
# Stripping them lets us collapse "image.jpg?width=200" and
# "image.jpg?width=800" down to one unique image.
SIZE_QUERY_PARAMS = {
    "width", "w", "height", "h", "size", "quality", "q",
    "crop", "format", "fit", "dpr", "scale",
}


def normalize_url_key(url: str) -> str:
    """Return a canonical version of an image URL with size/quality query
    params stripped, used purely as a deduplication key (not for display
    or downloading -- the original, highest-resolution URL is kept for that).
    """
    parsed = urlsplit(url)
    query_pairs = parse_qsl(parsed.query, keep_blank_values=True)
    filtered = [(k, v) for k, v in query_pairs if k.lower() not in SIZE_QUERY_PARAMS]
    filtered.sort()  # order-independent key
    new_query = urlencode(filtered)
    # Drop fragment too; it never affects which image is served.
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path, new_query, ""))


def extract_size_hint(url: str) -> int:
    """Pull a numeric size hint (width/height/size param) out of a URL's
    query string, used to decide which variant of a duplicate is "largest"
    and therefore worth keeping as the canonical URL.
    """
    parsed = urlsplit(url)
    query_pairs = dict(parse_qsl(parsed.query))
    for key in ("width", "w", "size", "height", "h"):
        if key in query_pairs:
            digits = re.sub(r"\D", "", query_pairs[key])
            if digits:
                return int(digits)
    return 0


CSS_URL_PATTERN = re.compile(r"""url\(\s*['"]?([^'")]+)['"]?\s*\)""", re.IGNORECASE)


def extract_urls_from_css(css_text: str) -> list[str]:
    """Extract url(...) references from inline style / <style> CSS text."""
    if not css_text:
        return []
    return CSS_URL_PATTERN.findall(css_text)


def timestamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
