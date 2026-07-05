"""
converter.py
------------
Converts every downloaded raw image (jpg, jpeg, png, webp, gif, bmp, tiff,
svg) into a final PNG file, preserving transparency where the source format
supports it. Output files are named image_001.png, image_002.png, etc.,
matching the original CSV serial number.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageSequence

from utils import IMAGES_DIR, log_error, log_success

try:
    # cairosvg is optional; SVG conversion is attempted only if it's installed.
    import cairosvg  # type: ignore

    CAIROSVG_AVAILABLE = True
except ImportError:  # pragma: no cover - environment dependent
    CAIROSVG_AVAILABLE = False


@dataclass
class ConversionResult:
    serial: int
    source_path: Path
    output_path: Path | None
    success: bool
    error: str | None = None


class PNGConverter:
    """Converts raw downloaded images into normalized PNG output files."""

    def __init__(self, output_dir: Path = IMAGES_DIR):
        self.output_dir = output_dir
        self.output_dir.mkdir(exist_ok=True)

    def convert_all(self, raw_files: list[tuple[int, Path, str]]) -> list[ConversionResult]:
        """Convert a list of (serial, raw_file_path, source_url) into PNGs."""
        results: list[ConversionResult] = []
        for serial, raw_path, source_url in raw_files:
            result = self._convert_one(serial, raw_path, source_url)
            results.append(result)
        return results

    # ------------------------------------------------------------------
    def _convert_one(self, serial: int, raw_path: Path, source_url: str) -> ConversionResult:
        import re
        # raw_path already has the original filename (e.g. "banner.jpg")
        # just swap the extension to .png
        original_stem = re.sub(r'[\\/*?:"<>|]', "_", raw_path.stem)
        output_path = self.output_dir / f"{original_stem}.png"

        # Collision guard: agar same name ka PNG pehle se exist kare
        if output_path.exists() and output_path != raw_path.with_suffix(".png"):
            output_path = self.output_dir / f"{original_stem}_{serial:03d}.png"

        try:
            if raw_path.suffix.lower() == ".svg":
                self._convert_svg(raw_path, output_path)
            else:
                self._convert_raster(raw_path, output_path)

            # Clean up the intermediate raw file (keep only final PNG)
            if raw_path.exists() and raw_path != output_path:
                raw_path.unlink(missing_ok=True)

            log_success(source_url, "convert", f"saved as {output_path.name}")
            return ConversionResult(serial, raw_path, output_path, success=True)

        except Exception as exc:  # noqa: BLE001 - log and report, never crash the batch
            log_error(source_url, "convert", str(exc))
            return ConversionResult(serial, raw_path, None, success=False, error=str(exc))

    @staticmethod
    def _convert_raster(raw_path: Path, output_path: Path) -> None:
        """Convert any Pillow-supported raster format (jpg/png/webp/gif/bmp/tiff) to PNG.

        Transparency is preserved by converting to RGBA when the source has an
        alpha channel or a palette-based transparency index; otherwise we
        convert to RGB to avoid garbled color output.
        For animated formats (GIF/animated WEBP/multi-page TIFF), only the
        first frame is used, since PNG is not an animation format.
        """
        with Image.open(raw_path) as img:
            # Grab the first frame for animated images
            img = next(ImageSequence.Iterator(img))

            if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
                img = img.convert("RGBA")
            else:
                img = img.convert("RGB")

            img.save(output_path, format="PNG")

    @staticmethod
    def _convert_svg(raw_path: Path, output_path: Path) -> None:
        """Convert an SVG to PNG using cairosvg if available.

        SVG is a vector format with no universal raster fallback in Pillow,
        so we depend on cairosvg. If it's not installed, we raise a clear
        error that gets logged to error.log rather than crashing the run.
        """
        if not CAIROSVG_AVAILABLE:
            raise RuntimeError(
                "cairosvg is not installed; cannot convert SVG to PNG. "
                "Install it with: pip install cairosvg"
            )
        cairosvg.svg2png(url=str(raw_path), write_to=str(output_path))
