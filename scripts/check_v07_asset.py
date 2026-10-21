"""Read-only diagnostics for fixed V0.7 package PNGs; never repair source art.

Requires the existing project GUI extra (PySide6), not Pillow or NumPy. No Qt
application, window, product factory, profile, configuration or network is used.

Run with the explicit project interpreter, for example:
    python scripts/check_v07_asset.py
    python scripts/check_v07_asset.py --preview data/validation/v07/NEW/asset-preview

JSON goes to stdout. Exit 0 means the measured structural budgets passed, not
visual acceptance. Exit 1 reports image/budget defects; exit 2 is a probe setup
failure. --preview creates a NEW diagnostic directory and never overwrites it.
"""

from __future__ import annotations

import argparse
from hashlib import sha256
from itertools import combinations
import json
from pathlib import Path
import struct
import sys
from typing import Any, TYPE_CHECKING, TypedDict
import zlib

if TYPE_CHECKING:
    from PySide6.QtGui import QImage


class BrightnessAccumulator(TypedDict):
    count: int
    min: int
    max: int
    sum: int
    rgb_min: list[int]
    rgb_max: list[int]


ROOT = Path(__file__).resolve().parents[1]
ASSET_DIR = ROOT / "src" / "deskpal" / "gui" / "assets" / "pet"
EXPRESSIONS = ("neutral", "thinking", "focused", "pleased", "alert")
MAX_FILES = 8
MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_TOTAL_BYTES = 20 * 1024 * 1024
MAX_EDGE = 2048
MAX_TOTAL_PIXELS = 8 * 1024 * 1024
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def png_header(raw: bytes) -> dict[str, Any]:
    """Independently identify the bytes, before asking Qt to decode them."""
    if len(raw) < 33 or raw[:8] != PNG_SIGNATURE:
        raise ValueError("invalid_png_signature_or_truncated_ihdr")
    length = struct.unpack(">I", raw[8:12])[0]
    if length != 13 or raw[12:16] != b"IHDR":
        raise ValueError("invalid_png_ihdr")
    width, height, depth, color, compression, filtering, interlace = struct.unpack(
        ">IIBBBBB", raw[16:29])
    crc = struct.unpack(">I", raw[29:33])[0]
    if zlib.crc32(raw[12:29]) & 0xFFFFFFFF != crc:
        raise ValueError("invalid_ihdr_crc")
    return {"width": width, "height": height, "bit_depth": depth,
            "color_type": color, "compression": compression, "filter": filtering,
            "interlace": interlace, "ihdr_crc_valid": True}


def rgba_statistics(raw: bytes, width: int, height: int,
                    stride: int) -> tuple[dict[str, Any], bytearray]:
    """Analyze straight RGBA bytes; hidden RGB does not imply visible content."""
    alpha = bytearray(width * height)
    histogram = {"zero": 0, "1_to_15": 0, "16_to_127": 0, "128_to_254": 0, "opaque": 0}
    minimum, maximum, alpha_sum = 255, 0, 0
    groups: dict[str, BrightnessAccumulator] = {
        name: {"count": 0, "min": 255, "max": 0, "sum": 0,
               "rgb_min": [255, 255, 255], "rgb_max": [0, 0, 0]}
        for name in ("all_pixels", "alpha_nonzero", "alpha_at_least_128")
    }
    left, top, right, bottom = width, height, -1, -1
    for y in range(height):
        for x in range(width):
            offset = y * stride + x * 4
            r, g, b, a = raw[offset:offset + 4]
            alpha[y * width + x] = a
            minimum, maximum = min(minimum, a), max(maximum, a)
            alpha_sum += a
            bucket = ("zero" if a == 0 else "1_to_15" if a < 16 else
                      "16_to_127" if a < 128 else "128_to_254" if a < 255 else "opaque")
            histogram[bucket] += 1
            # Integer approximation to Rec.709 weights on encoded RGB, 0..255.
            # This is a diagnostic brightness value, not linear-light luminance.
            brightness = (54 * r + 183 * g + 19 * b + 128) >> 8
            for name in ("all_pixels", "alpha_nonzero", "alpha_at_least_128"):
                if (name == "alpha_nonzero" and a == 0) or (name == "alpha_at_least_128" and a < 128):
                    continue
                group = groups[name]
                group["count"] += 1
                group["min"] = min(group["min"], brightness)
                group["max"] = max(group["max"], brightness)
                group["sum"] += brightness
                for channel, value in enumerate((r, g, b)):
                    group["rgb_min"][channel] = min(group["rgb_min"][channel], value)
                    group["rgb_max"][channel] = max(group["rgb_max"][channel], value)
            if a:
                left, top, right, bottom = min(left, x), min(top, y), max(right, x), max(bottom, y)
    summaries: dict[str, dict[str, Any]] = {}
    for name, group in groups.items():
        count = group["count"]
        summary: dict[str, Any] = {
            "count": count, "min": group["min"], "max": group["max"],
            "rgb_min": group["rgb_min"], "rgb_max": group["rgb_max"],
            "mean": group["sum"] / count if count else None,
        }
        if not count:
            for key in ("min", "max", "rgb_min", "rgb_max"):
                summary[key] = None
        summaries[name] = summary
    nonzero = len(alpha) - histogram["zero"]
    return {"alpha": {"min": minimum, "max": maximum, "nonzero_count": nonzero,
                       "nonzero_fraction": nonzero / len(alpha),
                       "mean": alpha_sum / len(alpha), "histogram": histogram,
                       "nonzero_bbox_xyxy_exclusive": [left, top, right + 1, bottom + 1] if nonzero else None},
            "brightness": {"formula": "(54*R + 183*G + 19*B + 128) >> 8; straight encoded RGB",
                           **summaries}}, alpha


def paeth(left: int, above: int, upper_left: int) -> int:
    prediction = left + above - upper_left
    delta_left, delta_above, delta_corner = (
        abs(prediction - left), abs(prediction - above), abs(prediction - upper_left))
    if delta_left <= delta_above and delta_left <= delta_corner:
        return left
    return above if delta_above <= delta_corner else upper_left


def independent_rgba(raw: bytes, header: dict[str, Any]) -> bytes | None:
    """Independently unfilter ordinary non-interlaced 8-bit PNGs using stdlib.

    This deliberately small diagnostic decoder is not a product resource loader.
    Unsupported depths/interlacing are explicitly reported instead of guessed.
    Decompression is bounded by the already checked IHDR canvas dimensions.
    """
    color = header["color_type"]
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(color)
    if header["bit_depth"] != 8 or header["interlace"] != 0 or channels is None:
        return None
    if header["compression"] != 0 or header["filter"] != 0:
        raise ValueError("unsupported_png_compression_or_filter_method")
    packed, palette, transparency = bytearray(), b"", b""
    offset, ended = 8, False
    while offset < len(raw):
        if offset + 12 > len(raw):
            raise ValueError("truncated_png_chunk")
        length = struct.unpack(">I", raw[offset:offset + 4])[0]
        end = offset + 12 + length
        if end > len(raw):
            raise ValueError("truncated_png_chunk_data")
        kind, data = raw[offset + 4:offset + 8], raw[offset + 8:end - 4]
        crc = struct.unpack(">I", raw[end - 4:end])[0]
        if zlib.crc32(kind + data) & 0xFFFFFFFF != crc:
            raise ValueError("invalid_png_chunk_crc")
        if kind == b"IDAT":
            packed.extend(data)
        elif kind == b"PLTE":
            palette = data
        elif kind == b"tRNS":
            transparency = data
        elif kind == b"acTL":
            raise ValueError("animated_png_not_supported")
        elif kind == b"IEND":
            ended = True
            break
        offset = end
    if not ended or not packed:
        raise ValueError("missing_png_iend_or_idat")
    width, height = header["width"], header["height"]
    stride = width * channels
    expected = height * (stride + 1)
    decompressor = zlib.decompressobj()
    scanlines = decompressor.decompress(bytes(packed), expected + 1)
    if len(scanlines) != expected or not decompressor.eof or decompressor.unused_data:
        raise ValueError("invalid_or_oversize_png_scanlines")
    rgba = bytearray(width * height * 4)
    previous = bytearray(stride)
    transparent_gray = struct.unpack(">H", transparency)[0] if color == 0 and len(transparency) == 2 else None
    transparent_rgb = struct.unpack(">HHH", transparency) if color == 2 and len(transparency) == 6 else None
    for y in range(height):
        start = y * (stride + 1)
        filtering = scanlines[start]
        row = bytearray(scanlines[start + 1:start + stride + 1])
        if filtering > 4:
            raise ValueError("invalid_png_row_filter")
        for index in range(stride):
            left = row[index - channels] if index >= channels else 0
            above = previous[index]
            upper_left = previous[index - channels] if index >= channels else 0
            predictor = (0 if filtering == 0 else left if filtering == 1 else above if filtering == 2
                         else (left + above) // 2 if filtering == 3 else paeth(left, above, upper_left))
            row[index] = (row[index] + predictor) & 255
        for x in range(width):
            pixel, output = x * channels, (y * width + x) * 4
            if color == 6:
                rgba[output:output + 4] = row[pixel:pixel + 4]
                continue
            if color == 2:
                r, g, b = row[pixel:pixel + 3]
                a = 0 if (r, g, b) == transparent_rgb else 255
            elif color == 3:
                palette_index = row[pixel]
                entry = palette_index * 3
                if entry + 3 > len(palette):
                    raise ValueError("invalid_png_palette_index")
                r, g, b = palette[entry:entry + 3]
                a = transparency[palette_index] if palette_index < len(transparency) else 255
            else:
                r = g = b = row[pixel]
                a = row[pixel + 1] if color == 4 else (0 if r == transparent_gray else 255)
            rgba[output:output + 4] = bytes((r, g, b, a))
        previous = row
    return bytes(rgba)


def compare_alpha(first: dict[str, Any], second: dict[str, Any],
                  alpha_a: bytearray, alpha_b: bytearray) -> dict[str, Any]:
    result: dict[str, Any] = {"first": first["file"], "second": second["file"],
                              "canvas_equal": first["decoded_size"] == second["decoded_size"]}
    if not result["canvas_equal"]:
        result["comparison"] = "not_comparable_without_resizing_or_alignment"
        return result
    changed = total_delta = max_delta = 0
    masks = {threshold: {"first_count": 0, "second_count": 0, "intersection": 0, "union": 0,
                         "xor_count": 0} for threshold in (1, 128)}
    for a, b in zip(alpha_a, alpha_b):
        delta = abs(a - b)
        changed += delta != 0
        total_delta += delta
        max_delta = max(max_delta, delta)
        for threshold, stats in masks.items():
            member_a, member_b = a >= threshold, b >= threshold
            stats["first_count"] += member_a
            stats["second_count"] += member_b
            stats["intersection"] += member_a and member_b
            stats["union"] += member_a or member_b
            stats["xor_count"] += member_a != member_b
    result["alpha_difference"] = {"changed_pixel_count": changed,
        "mean_absolute_delta": total_delta / len(alpha_a), "max_absolute_delta": max_delta}
    result["silhouettes"] = {}
    for threshold, stats in masks.items():
        result["silhouettes"][f"alpha_at_least_{threshold}"] = {
            **stats, "xor_fraction_of_canvas": stats["xor_count"] / len(alpha_a),
            "iou": stats["intersection"] / stats["union"] if stats["union"] else None}
    return result


def save_previews(image: QImage, alpha: bytearray, directory: Path, stem: str) -> list[str]:
    """Composite diagnostics at exact source size; do not alter source pixels."""
    from PySide6.QtGui import QColor, QImage, QPainter

    width, height = image.width(), image.height()
    checker = QImage(width, height, QImage.Format.Format_RGB32)
    painter = QPainter(checker)
    try:
        edge = 24
        for y in range(0, height, edge):
            for x in range(0, width, edge):
                value = 255 if ((x // edge) + (y // edge)) % 2 == 0 else 200
                painter.fillRect(x, y, edge, edge, QColor(value, value, value))
        painter.drawImage(0, 0, image)
    finally:
        painter.end()
    # The extra alpha view isolates transparency from artwork RGB and compositing.
    mask = QImage(bytes(alpha), width, height, width, QImage.Format.Format_Grayscale8).copy()
    outputs = [(checker, directory / f"{stem}.checker.png"),
               (mask, directory / f"{stem}.alpha.png")]
    for diagnostic, path in outputs:
        # Qt infers PNG from the suffix; omit format to avoid binding/stub disagreement.
        if path.exists() or not diagnostic.save(str(path)):
            raise OSError("preview_write_failed_or_target_exists")
    return [str(path) for _, path in outputs]


def inspect_assets(preview: Path | None) -> dict[str, Any]:
    from PySide6 import __version__ as pyside_version
    from PySide6.QtCore import QByteArray, QBuffer, QIODevice, qVersion
    from PySide6.QtGui import QImage, QImageReader

    paths = sorted(ASSET_DIR.glob("*.png"))
    report: dict[str, Any] = {"schema_version": 1, "source_directory": str(ASSET_DIR),
        "python_executable": sys.executable, "pyside_version": pyside_version, "qt_version": qVersion(),
        "script_sha256": sha256(Path(__file__).read_bytes()).hexdigest(),
        "budgets": {"max_files": MAX_FILES, "max_file_bytes": MAX_FILE_BYTES,
                    "max_total_bytes": MAX_TOTAL_BYTES, "max_edge": MAX_EDGE,
                    "max_total_decoded_pixels": MAX_TOTAL_PIXELS},
        "optional_missing_expressions": [name for name in EXPRESSIONS[1:]
                                          if not (ASSET_DIR / f"{name}.png").is_file()],
        "files": [], "pairs": [], "errors": [],
        "interpretation": "Structural diagnostics only; visual acceptance requires human review."}
    if not (ASSET_DIR / "neutral.png").is_file():
        report["errors"].append("required_neutral_missing")
    if len(paths) > MAX_FILES:
        report["errors"].append("file_count_budget_exceeded; only_first_8_inspected")
    total_bytes = total_pixels = 0
    comparable: list[tuple[dict[str, Any], bytearray]] = []
    for path in paths[:MAX_FILES]:
        info: dict[str, Any] = {"file": path.name, "path": str(path), "errors": []}
        report["files"].append(info)
        try:
            if path.is_symlink() or path.resolve().parent != ASSET_DIR.resolve():
                raise ValueError("source_symlink_or_path_outside_fixed_directory")
            # Hash in bounded chunks even when the input exceeds decoding budget.
            digest, size = sha256(), 0
            with path.open("rb") as stream:
                while block := stream.read(65536):
                    size += len(block)
                    digest.update(block)
            info.update(bytes=size, sha256=digest.hexdigest(), file_byte_budget_ok=size <= MAX_FILE_BYTES)
            total_bytes += size
            if size > MAX_FILE_BYTES:
                raise ValueError("file_byte_budget_exceeded; not_decoded")
            raw = path.read_bytes()
            if len(raw) != size or sha256(raw).hexdigest() != digest.hexdigest():
                raise ValueError("source_changed_during_probe")
            header = png_header(raw)
            info["png_ihdr"] = header
            if not 0 < header["width"] <= MAX_EDGE or not 0 < header["height"] <= MAX_EDGE:
                raise ValueError("dimension_budget_exceeded; not_decoded")
            pixels = header["width"] * header["height"]
            if total_pixels + pixels > MAX_TOTAL_PIXELS:
                raise ValueError("total_pixel_budget_exceeded; not_decoded")
            if total_bytes > MAX_TOTAL_BYTES:
                raise ValueError("total_byte_budget_exceeded; not_decoded")
            buffer = QBuffer()
            buffer.setData(QByteArray(raw))
            if not buffer.open(QIODevice.OpenModeFlag.ReadOnly):
                raise ValueError("qt_buffer_open_failed")
            try:
                reader = QImageReader(buffer)
                reader.setDecideFormatFromContent(True)
                reader.setAutoTransform(False)
                info["detected_format"] = bytes(reader.format().data()).decode("ascii", errors="replace")
                info["reader_size"] = [reader.size().width(), reader.size().height()]
                if info["detected_format"] != "png":
                    raise ValueError("qt_detected_non_png")
                image = reader.read()
                if image.isNull():
                    info["qt_error_code"] = reader.error().name
                    raise ValueError("qt_decode_failed")
            finally:
                buffer.close()
            info.update(decoded_size=[image.width(), image.height()],
                        qt_image_format=image.format().name, has_alpha_channel=image.hasAlphaChannel())
            if info["decoded_size"] != [header["width"], header["height"]]:
                raise ValueError("header_decode_size_mismatch")
            total_pixels += pixels
            rgba = image.convertToFormat(QImage.Format.Format_RGBA8888)
            qt_raw = bytes(rgba.constBits())
            width, height, stride = rgba.width(), rgba.height(), rgba.bytesPerLine()
            qt_pixels = b"".join(qt_raw[y * stride:y * stride + width * 4] for y in range(height))
            stats, alpha = rgba_statistics(qt_pixels, width, height, width * 4)
            info.update(stats)
            info["qt_straight_rgba_sha256"] = sha256(qt_pixels).hexdigest()
            independent = independent_rgba(raw, header)
            if independent is None:
                info["independent_stdlib_decode"] = {"status": "unsupported_depth_or_interlacing"}
            else:
                independent_stats, _ = rgba_statistics(independent, width, height, width * 4)
                equal = independent == qt_pixels
                info["independent_stdlib_decode"] = {"status": "decoded_straight_rgba8",
                    "rgba_sha256": sha256(independent).hexdigest(), "qt_rgba_equal": equal,
                    **independent_stats}
                if not equal:
                    info["errors"].append("qt_and_stdlib_rgba_differ; inspect_before_acceptance")
            if not image.hasAlphaChannel():
                info["errors"].append("alpha_channel_missing")
            if stats["alpha"]["nonzero_count"] == 0:
                info["errors"].append("fully_transparent")
            if stats["alpha"]["min"] == 255:
                info["errors"].append("no_transparent_pixels")
            comparable.append((info, alpha))
            if preview is not None:
                info["diagnostic_previews"] = save_previews(image, alpha, preview, path.stem)
            # Verify the source remains identical after diagnostics, including preview writes.
            info["source_sha256_after"] = sha256(path.read_bytes()).hexdigest()
            info["source_unchanged"] = info["source_sha256_after"] == info["sha256"]
            if not info["source_unchanged"]:
                info["errors"].append("source_changed_during_probe")
        except (OSError, ValueError, RuntimeError, zlib.error) as exc:
            info["errors"].append(str(exc))
    for (first, alpha_a), (second, alpha_b) in combinations(comparable, 2):
        pair = compare_alpha(first, second, alpha_a, alpha_b)
        report["pairs"].append(pair)
        if not pair["canvas_equal"]:
            report["errors"].append(f"canvas_mismatch:{first['file']}:{second['file']}")
    report["totals"] = {"discovered_file_count": len(paths), "inspected_file_count": len(report["files"]),
        "source_bytes_inspected": total_bytes, "source_byte_budget_ok": total_bytes <= MAX_TOTAL_BYTES,
        "decoded_pixels": total_pixels, "decoded_pixel_budget_ok": total_pixels <= MAX_TOTAL_PIXELS}
    if total_bytes > MAX_TOTAL_BYTES:
        report["errors"].append("total_byte_budget_exceeded")
    report["structural_checks_passed"] = not report["errors"] and all(
        not info["errors"] for info in report["files"])
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--preview", type=Path, metavar="NEW_DIRECTORY",
                        help="create exact-size checkerboard and alpha diagnostic PNGs in a NEW directory")
    arguments = parser.parse_args()
    try:
        preview = None
        if arguments.preview is not None:
            preview = arguments.preview.resolve()
            assets = ASSET_DIR.resolve()
            if preview == assets or assets in preview.parents or preview in assets.parents:
                raise ValueError("preview_directory_must_not_overlap_source_assets")
            # Exclusive new directory prevents stale previews or accidental overwrites.
            preview.mkdir(parents=True, exist_ok=False)
        report = inspect_assets(preview)
    except (ImportError, OSError, ValueError, RuntimeError) as exc:
        print(json.dumps({"schema_version": 1, "probe_setup_error": type(exc).__name__,
                          "detail": str(exc)}, ensure_ascii=False, indent=2))
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
    return 0 if report["structural_checks_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
