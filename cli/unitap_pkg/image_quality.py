import struct
import zlib
from pathlib import Path


PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _paeth_predictor(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa = abs(p - a)
    pb = abs(p - b)
    pc = abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    if pb <= pc:
        return b
    return c


def _read_png_chunks(raw: bytes):
    if len(raw) < 8 or raw[:8] != PNG_SIGNATURE:
        raise ValueError("not_png")

    cursor = 8
    while cursor + 8 <= len(raw):
        length = struct.unpack(">I", raw[cursor:cursor + 4])[0]
        cursor += 4
        chunk_type = raw[cursor:cursor + 4]
        cursor += 4
        if cursor + length + 4 > len(raw):
            raise ValueError("broken_png_chunk")
        data = raw[cursor:cursor + length]
        cursor += length
        _crc = raw[cursor:cursor + 4]
        cursor += 4
        yield chunk_type, data
        if chunk_type == b"IEND":
            return

    raise ValueError("iend_not_found")


def _unfilter_scanlines(filtered: bytes, width: int, height: int, bpp: int) -> bytes:
    stride = width * bpp
    out = bytearray(height * stride)
    in_cursor = 0
    out_cursor = 0
    prev_row = bytes(stride)

    for _ in range(height):
        if in_cursor >= len(filtered):
            raise ValueError("filtered_data_too_short")
        filter_type = filtered[in_cursor]
        in_cursor += 1
        if in_cursor + stride > len(filtered):
            raise ValueError("scanline_too_short")

        row = bytearray(filtered[in_cursor:in_cursor + stride])
        in_cursor += stride

        if filter_type == 1:  # Sub
            for i in range(stride):
                left = row[i - bpp] if i >= bpp else 0
                row[i] = (row[i] + left) & 0xFF
        elif filter_type == 2:  # Up
            for i in range(stride):
                row[i] = (row[i] + prev_row[i]) & 0xFF
        elif filter_type == 3:  # Average
            for i in range(stride):
                left = row[i - bpp] if i >= bpp else 0
                up = prev_row[i]
                row[i] = (row[i] + ((left + up) // 2)) & 0xFF
        elif filter_type == 4:  # Paeth
            for i in range(stride):
                left = row[i - bpp] if i >= bpp else 0
                up = prev_row[i]
                up_left = prev_row[i - bpp] if i >= bpp else 0
                row[i] = (row[i] + _paeth_predictor(left, up, up_left)) & 0xFF
        elif filter_type != 0:
            raise ValueError(f"unsupported_filter:{filter_type}")

        out[out_cursor:out_cursor + stride] = row
        out_cursor += stride
        prev_row = row

    return bytes(out)


def _extract_rgb_from_raw(raw_pixels: bytes, width: int, height: int, color_type: int) -> tuple[int, int, float, float, tuple[int, int, int] | None]:
    if color_type == 0:  # grayscale
        bpp = 1
    elif color_type == 2:  # rgb
        bpp = 3
    elif color_type == 6:  # rgba
        bpp = 4
    else:
        raise ValueError(f"unsupported_color_type:{color_type}")

    pixel_count = width * height
    if pixel_count <= 0:
        raise ValueError("empty_image")

    max_samples = 200000
    sample_step = max(1, pixel_count // max_samples)
    dominant_color = None
    dominant_count = 0
    color_counts = {}
    sampled = 0
    green_key_like = 0

    for index in range(0, pixel_count, sample_step):
        base = index * bpp
        if base + bpp > len(raw_pixels):
            break
        if color_type == 0:
            v = raw_pixels[base]
            r = g = b = v
        elif color_type == 2:
            r = raw_pixels[base]
            g = raw_pixels[base + 1]
            b = raw_pixels[base + 2]
        else:
            r = raw_pixels[base]
            g = raw_pixels[base + 1]
            b = raw_pixels[base + 2]

        key = (r, g, b)
        count = color_counts.get(key, 0) + 1
        color_counts[key] = count
        if count > dominant_count:
            dominant_count = count
            dominant_color = key

        if g >= 240 and r <= 20 and b <= 20:
            green_key_like += 1
        sampled += 1

    if sampled == 0:
        raise ValueError("no_samples")

    dominant_ratio = dominant_count / sampled
    unique_colors = len(color_counts)
    green_ratio = green_key_like / sampled
    return sampled, unique_colors, dominant_ratio, green_ratio, dominant_color


def inspect_capture_image(path: str) -> dict:
    file_path = Path(path)
    if not file_path.exists():
        return {
            "ok": False,
            "reason": "file_not_found",
            "isAnomaly": False,
        }

    try:
        raw = file_path.read_bytes()
        ihdr = None
        idat_parts = []
        for chunk_type, chunk_data in _read_png_chunks(raw):
            if chunk_type == b"IHDR":
                ihdr = chunk_data
            elif chunk_type == b"IDAT":
                idat_parts.append(chunk_data)

        if ihdr is None:
            return {"ok": False, "reason": "missing_ihdr", "isAnomaly": False}
        if len(ihdr) != 13:
            return {"ok": False, "reason": "invalid_ihdr", "isAnomaly": False}

        width, height, bit_depth, color_type, _, _, interlace = struct.unpack(">IIBBBBB", ihdr)
        if bit_depth != 8:
            return {
                "ok": False,
                "reason": f"unsupported_bit_depth:{bit_depth}",
                "isAnomaly": False,
                "width": width,
                "height": height,
            }
        if interlace != 0:
            return {
                "ok": False,
                "reason": "unsupported_interlace",
                "isAnomaly": False,
                "width": width,
                "height": height,
            }

        if color_type == 0:
            bpp = 1
        elif color_type == 2:
            bpp = 3
        elif color_type == 6:
            bpp = 4
        else:
            return {
                "ok": False,
                "reason": f"unsupported_color_type:{color_type}",
                "isAnomaly": False,
                "width": width,
                "height": height,
            }

        compressed = b"".join(idat_parts)
        if not compressed:
            return {"ok": False, "reason": "missing_idat", "isAnomaly": False, "width": width, "height": height}
        filtered = zlib.decompress(compressed)
        raw_pixels = _unfilter_scanlines(filtered, width, height, bpp)
        sampled, unique_colors, dominant_ratio, green_ratio, dominant_color = _extract_rgb_from_raw(
            raw_pixels,
            width,
            height,
            color_type,
        )

        anomaly_reasons = []
        if dominant_ratio >= 0.97 and unique_colors <= 32:
            anomaly_reasons.append("near_monochrome")
        if green_ratio >= 0.9 and dominant_ratio >= 0.9:
            anomaly_reasons.append("green_key_like")

        return {
            "ok": True,
            "isAnomaly": len(anomaly_reasons) > 0,
            "anomalyReasons": anomaly_reasons,
            "width": width,
            "height": height,
            "sampledPixels": sampled,
            "uniqueColorsSampled": unique_colors,
            "dominantRatio": round(dominant_ratio, 5),
            "greenLikeRatio": round(green_ratio, 5),
            "dominantColor": dominant_color,
        }
    except Exception as ex:
        return {
            "ok": False,
            "reason": f"inspect_error:{ex}",
            "isAnomaly": False,
        }
