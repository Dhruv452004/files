"""
main.py
-------
Command-line entry point that orchestrates the full pipeline:

    1. Ask the user for a website URL.
    2. Scrape the page (static + dynamic/Playwright) for image URLs.
    3. Save discovered URLs to image.csv.
    4. Download every image (async, concurrent, with retries).
    5. Convert every downloaded image into a normalized PNG file.

Usage:
    python main.py
    python main.py --url https://example.com --no-dynamic
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from converter import PNGConverter
from downloader import ImageDownloader, read_tasks_from_csv
from scraper import WebsiteImageScraper
from utils import CSV_PATH, IMAGES_DIR


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scrape, download, and convert website images to PNG.")
    parser.add_argument("--url", type=str, default=None, help="Website URL to scrape.")
    parser.add_argument(
        "--no-dynamic",
        action="store_true",
        help="Skip the Playwright (JavaScript-rendered) scraping pass; static HTML only.",
    )
    return parser.parse_args()


def get_url(args: argparse.Namespace) -> str:
    if args.url:
        return args.url.strip()
    url = input("Enter Website URL: ").strip()
    while not url.lower().startswith(("http://", "https://")):
        print("[!] Please enter a valid URL starting with http:// or https://")
        url = input("Enter Website URL: ").strip()
    return url


def run_scrape_stage(url: str, use_dynamic: bool) -> list[str]:
    print(f"\n[*] Crawling {url} ...")
    scraper = WebsiteImageScraper(url=url, use_dynamic=use_dynamic)
    result = scraper.scrape()

    if not result.image_urls:
        print("[x] No images found on this page. Exiting.")
        sys.exit(1)

    print(f"[✓] Found {len(result.image_urls)} images")

    count = scraper.save_to_csv(result.image_urls, csv_path=CSV_PATH)
    print(f"[✓] image.csv created ({count} rows) -> {CSV_PATH}")
    return result.image_urls


def run_download_stage() -> list[tuple[int, Path, str]]:
    print("\n[*] Downloading images...")
    tasks = read_tasks_from_csv(CSV_PATH)
    if not tasks:
        print("[x] image.csv is empty. Nothing to download.")
        sys.exit(1)

    downloader = ImageDownloader(output_dir=IMAGES_DIR)
    results = asyncio.run(downloader.download_all(tasks))

    successes = [r for r in results if r.success]
    failures = [r for r in results if not r.success]

    print(f"[✓] Downloaded {len(successes)}/{len(tasks)} images")
    if failures:
        print(f"[!] {len(failures)} downloads failed (see logs/error.log)")

    # Build (serial, raw_path, source_url) tuples for the converter
    return [(r.serial, r.file_path, r.url) for r in successes if r.file_path is not None]


def run_convert_stage(raw_files: list[tuple[int, Path, str]]) -> None:
    print("\n[*] Converting to PNG...")
    converter = PNGConverter(output_dir=IMAGES_DIR)
    results = converter.convert_all(raw_files)

    successes = [r for r in results if r.success]
    failures = [r for r in results if not r.success]

    print(f"[✓] Converted {len(successes)}/{len(raw_files)} images to PNG")
    if failures:
        print(f"[!] {len(failures)} conversions failed (see logs/error.log)")


def main() -> None:
    args = parse_args()
    url = get_url(args)
    use_dynamic = not args.no_dynamic

    run_scrape_stage(url, use_dynamic)
    raw_files = run_download_stage()
    run_convert_stage(raw_files)

    print("\n[✓] Completed successfully")
    print(f"    - CSV file:   {CSV_PATH}")
    print(f"    - PNG images: {IMAGES_DIR}")
    print(f"    - Logs:       logs/success.log, logs/error.log")


if __name__ == "__main__":
    main()
