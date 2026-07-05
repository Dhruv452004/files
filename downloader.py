"""
downloader.py
-------------
Asynchronous image downloader. Reads URLs from image.csv, downloads them
concurrently (bounded by a semaphore) using aiohttp, retries on failure,
and saves raw files into the images/ folder with a temporary naming scheme
that converter.py later turns into the final image_XXX.png files.
"""

from __future__ import annotations

import asyncio
import csv
from dataclasses import dataclass
from pathlib import Path

import aiohttp
from tqdm import tqdm

from utils import CSV_PATH, IMAGES_DIR, log_error, log_success, safe_extension_from_url

MAX_CONCURRENT_DOWNLOADS = 10
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 1.5
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=30)

REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}


@dataclass
class DownloadTask:
    serial: int
    url: str


@dataclass
class DownloadResult:
    serial: int
    url: str
    success: bool
    file_path: Path | None = None
    error: str | None = None


def read_tasks_from_csv(csv_path: Path = CSV_PATH) -> list[DownloadTask]:
    """Read (serial, url) pairs out of image.csv."""
    tasks: list[DownloadTask] = []
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                serial = int(row["Serial Number"])
                url = row["Image URL"].strip()
            except (KeyError, ValueError):
                continue
            if url:
                tasks.append(DownloadTask(serial=serial, url=url))
    return tasks


class ImageDownloader:
    """Downloads images concurrently with retry logic and a tqdm progress bar."""

    def __init__(
        self,
        output_dir: Path = IMAGES_DIR,
        max_concurrent: int = MAX_CONCURRENT_DOWNLOADS,
        max_retries: int = MAX_RETRIES,
    ):
        self.output_dir = output_dir
        self.output_dir.mkdir(exist_ok=True)
        self.max_concurrent = max_concurrent
        self.max_retries = max_retries

    async def download_all(self, tasks: list[DownloadTask]) -> list[DownloadResult]:
        """Download every task concurrently, returning a list of results."""
        semaphore = asyncio.Semaphore(self.max_concurrent)
        results: list[DownloadResult] = []

        async with aiohttp.ClientSession(headers=REQUEST_HEADERS, timeout=REQUEST_TIMEOUT) as session:
            progress = tqdm(total=len(tasks), desc="Downloading images", unit="img")

            async def worker(task: DownloadTask) -> None:
                async with semaphore:
                    result = await self._download_with_retry(session, task)
                    results.append(result)
                    progress.update(1)

            await asyncio.gather(*(worker(t) for t in tasks))
            progress.close()

        return results

    async def _download_with_retry(
        self, session: aiohttp.ClientSession, task: DownloadTask
    ) -> DownloadResult:
        last_error = "Unknown error"

        for attempt in range(1, self.max_retries + 1):
            try:
                return await self._download_once(session, task)
            except Exception as exc:  # noqa: BLE001 - log and retry
                last_error = str(exc)
                if attempt < self.max_retries:
                    await asyncio.sleep(RETRY_BACKOFF_SECONDS * attempt)

        log_error(task.url, "download", f"failed after {self.max_retries} attempts: {last_error}")
        return DownloadResult(serial=task.serial, url=task.url, success=False, error=last_error)

    async def _download_once(
        self, session: aiohttp.ClientSession, task: DownloadTask
    ) -> DownloadResult:
        async with session.get(task.url) as response:
            if response.status != 200:
                raise RuntimeError(f"HTTP {response.status}")

            content = await response.read()
            if not content:
                raise RuntimeError("Empty response body")

            ext = self._resolve_extension(task.url, response.headers.get("Content-Type"))

            # Use original filename from URL (e.g. "banner.jpg")
            from urllib.parse import urlparse
            import re
            url_path = urlparse(task.url).path
            original_stem = Path(url_path).stem or f"raw_{task.serial:03d}"
            original_stem = re.sub(r'[\\/*?:"<>|]', "_", original_stem)
            filename = f"{original_stem}{ext}"

            # Avoid collision if two URLs have same filename
            file_path = self.output_dir / filename
            if file_path.exists():
                filename = f"{original_stem}_{task.serial:03d}{ext}"
                file_path = self.output_dir / filename

            file_path.write_bytes(content)

            log_success(task.url, "download", f"saved as {filename}")
            return DownloadResult(serial=task.serial, url=task.url, success=True, file_path=file_path)

    @staticmethod
    def _resolve_extension(url: str, content_type: str | None) -> str:
        """Prefer the Content-Type header to determine the real file extension,
        falling back to the URL's extension if the header is missing/unknown."""
        mime_map = {
            "image/jpeg": ".jpg",
            "image/jpg": ".jpg",
            "image/png": ".png",
            "image/webp": ".webp",
            "image/gif": ".gif",
            "image/bmp": ".bmp",
            "image/tiff": ".tiff",
            "image/svg+xml": ".svg",
        }
        if content_type:
            content_type = content_type.split(";")[0].strip().lower()
            if content_type in mime_map:
                return mime_map[content_type]
        return safe_extension_from_url(url)
