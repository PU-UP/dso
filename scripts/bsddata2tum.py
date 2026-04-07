#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as _dt
import os
import re
import struct
import sys
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple


@dataclass(frozen=True)
class Calib:
    width: int
    height: int
    fx: float
    fy: float
    cx: float
    cy: float


def _repo_root_from_this_file() -> Path:
    return Path(__file__).resolve().parents[1]


def _parse_timestamp_from_stem(stem: str) -> float:
    # BSD image file name is timestamp like "1761810844.173597574234009"
    try:
        return float(stem)
    except ValueError as e:
        raise ValueError(f"无法从文件名解析时间戳: {stem!r}") from e


def _read_calib_yaml(calib_path: Path) -> Calib:
    """
    Reads needed fields from bsd_1030/calibration_config.yaml.
    Tries PyYAML first; falls back to a simple regex parser.
    """
    text = calib_path.read_text(encoding="utf-8", errors="replace")

    # Try PyYAML if available
    try:
        import yaml  # type: ignore

        obj = yaml.safe_load(text)
        cam = obj["intrinsics"]["camera"]
        width = int(cam["width"])
        height = int(cam["height"])
        proj = cam["projection_parameters"]
        fx, fy, cx, cy = map(float, proj[:4])
        return Calib(width=width, height=height, fx=fx, fy=fy, cx=cx, cy=cy)
    except Exception:
        pass

    # Fallback parser for the current YAML layout
    def _find_int(key: str) -> int:
        m = re.search(rf"^\s*{re.escape(key)}\s*:\s*(\d+)\s*$", text, flags=re.M)
        if not m:
            raise ValueError(f"标定文件缺少字段 {key!r}")
        return int(m.group(1))

    width = _find_int("width")
    height = _find_int("height")

    m = re.search(r"^\s*projection_parameters\s*:\s*$([\s\S]*?)(^\s*\w+\s*:|\Z)", text, flags=re.M)
    if not m:
        raise ValueError("标定文件缺少 projection_parameters")
    block = m.group(1)
    nums = re.findall(r"^\s*-\s*([+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)\s*$", block, flags=re.M)
    if len(nums) < 4:
        raise ValueError(f"projection_parameters 数量不足，期望>=4，实际={len(nums)}")
    fx, fy, cx, cy = map(float, nums[:4])
    return Calib(width=width, height=height, fx=fx, fy=fy, cx=cx, cy=cy)


def _write_camera_txt(out_dir: Path, calib: Calib) -> None:
    # DSO supports "ATAN" (no prefix) with 5 params: fx fy cx cy dist
    # dist=0 is equivalent to pinhole in the used ATAN/FOV model.
    camera_txt = "\n".join(
        [
            f"{calib.fx} {calib.fy} {calib.cx} {calib.cy} 0",
            f"{calib.width} {calib.height}",
            "crop",
            f"{calib.width} {calib.height}",
            "",
        ]
    )
    (out_dir / "camera.txt").write_text(camera_txt, encoding="utf-8")


def _copy_or_generate_pcalib(out_dir: Path) -> None:
    repo = _repo_root_from_this_file()
    src = repo / "data" / "sequence_08" / "pcalib.txt"
    dst = out_dir / "pcalib.txt"
    if src.is_file():
        dst.write_bytes(src.read_bytes())
        return

    # Fallback: identity gamma (strictly increasing)
    line = " ".join(str(i) for i in range(256)) + "\n\n"
    dst.write_text(line, encoding="utf-8")


def _png_chunk(chunk_type: bytes, data: bytes) -> bytes:
    crc = zlib.crc32(chunk_type)
    crc = zlib.crc32(data, crc) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + chunk_type + data + struct.pack(">I", crc)


def _write_png_gray8(path: Path, width: int, height: int, value: int = 255) -> None:
    if not (0 <= value <= 255):
        raise ValueError("value 必须在 0..255")

    # Each scanline: filter byte 0 + width bytes
    row = bytes([0]) + bytes([value]) * width
    raw = row * height
    compressed = zlib.compress(raw, level=6)

    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)  # 8-bit, grayscale
    out = bytearray()
    out += sig
    out += _png_chunk(b"IHDR", ihdr)
    out += _png_chunk(b"IDAT", compressed)
    out += _png_chunk(b"IEND", b"")
    path.write_bytes(bytes(out))


def _write_vignette_placeholder(out_dir: Path, calib: Calib) -> None:
    _write_png_gray8(out_dir / "vignette.png", calib.width, calib.height, value=255)


def _read_y_plane_from_yuv420(path: Path, width: int, height: int) -> bytes:
    b = path.read_bytes()
    need = width * height * 3 // 2
    if len(b) != need:
        raise ValueError(
            f"YUV420 文件大小不匹配: {path.name} bytes={len(b)} 期望={need} (w={width}, h={height})"
        )
    return b[: width * height]


def _write_pgm_gray8(path: Path, width: int, height: int, pixels: bytes) -> None:
    if len(pixels) != width * height:
        raise ValueError("PGM 像素长度不匹配")
    header = f"P5\n{width} {height}\n255\n".encode("ascii")
    path.write_bytes(header + pixels)


def _write_jpg_gray8(path: Path, width: int, height: int, pixels: bytes, quality: int = 95) -> None:
    if len(pixels) != width * height:
        raise ValueError("JPG 像素长度不匹配")
    if not (1 <= quality <= 100):
        raise ValueError("quality 必须在 1..100")

    # Prefer Pillow; fallback to OpenCV.
    try:
        from PIL import Image  # type: ignore

        img = Image.frombytes("L", (width, height), pixels)
        img.save(str(path), format="JPEG", quality=quality, optimize=True)
        return
    except Exception:
        pass

    try:
        import cv2  # type: ignore
        import numpy as np  # type: ignore

        arr = np.frombuffer(pixels, dtype=np.uint8).reshape((height, width))
        ok = cv2.imwrite(str(path), arr, [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)])
        if not ok:
            raise RuntimeError("cv2.imwrite 返回失败")
        return
    except Exception:
        raise RuntimeError(
            "保存 .jpg 需要 Pillow 或 opencv-python(+numpy)。请安装其一：\n"
            "  - pip install pillow\n"
            "  - 或 pip install opencv-python numpy"
        )


def _list_yuv_files(image_dir: Path) -> List[Path]:
    files = [p for p in image_dir.iterdir() if p.is_file() and p.suffix.lower() == ".yuv"]
    if not files:
        raise FileNotFoundError(f"目录中未找到 .yuv: {image_dir}")

    def _key(p: Path) -> float:
        return _parse_timestamp_from_stem(p.stem)

    files.sort(key=_key)
    return files


def _list_timestamped_image_files(image_dir: Path) -> List[Path]:
    exts = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".pgm"}
    files = [p for p in image_dir.iterdir() if p.is_file() and p.suffix.lower() in exts]
    if not files:
        raise FileNotFoundError(f"目录中未找到可用图像文件({sorted(exts)}): {image_dir}")

    def _key(p: Path) -> float:
        return _parse_timestamp_from_stem(p.stem)

    files.sort(key=_key)
    return files


def _default_out_dir(repo_root: Path) -> Path:
    today = _dt.date.today().strftime("%Y%m%d")
    return repo_root / "data" / f"bsd_tum_{today}"


def convert(
    image_dir: Path,
    calib_yaml: Path,
    out_dir: Path,
    exposure: float,
    ext: str,
    strict_size: bool,
) -> None:
    if exposure <= 0:
        raise ValueError("--exposure 必须为正数（避免 DSO 将曝光判为无效）")
    if ext not in ("jpg", "pgm"):
        raise ValueError("--ext 仅支持 jpg 或 pgm（仅当输入为 .yuv 需要转换时才生效）")

    calib = _read_calib_yaml(calib_yaml)

    # If input isn't YUV, no conversion needed: just copy & rename.
    has_yuv = any(p.is_file() and p.suffix.lower() == ".yuv" for p in image_dir.iterdir())

    out_dir.mkdir(parents=True, exist_ok=True)
    images_out = out_dir / "images"
    images_out.mkdir(parents=True, exist_ok=True)

    # write meta files
    _write_camera_txt(out_dir, calib)
    _copy_or_generate_pcalib(out_dir)
    _write_vignette_placeholder(out_dir, calib)

    times_lines: List[str] = []
    if has_yuv:
        yuv_files = _list_yuv_files(image_dir)
        for idx, yuv_path in enumerate(yuv_files):
            ts = _parse_timestamp_from_stem(yuv_path.stem)
            try:
                y = _read_y_plane_from_yuv420(yuv_path, calib.width, calib.height)
            except Exception:
                if strict_size:
                    raise
                else:
                    continue

            img_name = f"{idx:06d}.{ext}"
            out_path = images_out / img_name
            if ext == "pgm":
                _write_pgm_gray8(out_path, calib.width, calib.height, y)
            else:
                _write_jpg_gray8(out_path, calib.width, calib.height, y)
            times_lines.append(f"{idx:06d} {ts:.16f} {exposure}")
    else:
        src_files = _list_timestamped_image_files(image_dir)
        for idx, src_path in enumerate(src_files):
            ts = _parse_timestamp_from_stem(src_path.stem)
            dst_ext = src_path.suffix.lower().lstrip(".")
            dst_name = f"{idx:06d}.{dst_ext}"
            (images_out / dst_name).write_bytes(src_path.read_bytes())
            times_lines.append(f"{idx:06d} {ts:.16f} {exposure}")

    (out_dir / "times.txt").write_text("\n".join(times_lines) + ("\n" if times_lines else ""), encoding="utf-8")


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="将 BSD bsd_1030 (YUV420, 文件名为时间戳) 转成 DSO 兼容的 TUM 风格序列目录。"
    )
    p.add_argument("image_dir", type=str, help="BSD 图片目录（例如 data/bsd_1030/left_images）")
    p.add_argument("calib_yaml", type=str, help="标定文件（例如 data/bsd_1030/calibration_config.yaml）")
    p.add_argument("--out", type=str, default="", help="输出目录（默认 data/bsd_tum_YYYYMMDD）")
    p.add_argument("--exposure", type=float, default=0.01, help="固定曝光时间（默认 0.01）")
    p.add_argument("--ext", type=str, default="jpg", choices=["jpg", "pgm"], help="输出图像格式（默认 jpg）")
    p.add_argument(
        "--skip-bad-size",
        action="store_true",
        help="遇到大小不匹配的 yuv 时跳过该帧（默认遇到即报错退出）",
    )
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _build_argparser().parse_args(argv)
    repo = _repo_root_from_this_file()

    image_dir = Path(args.image_dir).expanduser().resolve()
    calib_yaml = Path(args.calib_yaml).expanduser().resolve()
    out_dir = Path(args.out).expanduser().resolve() if args.out else _default_out_dir(repo)

    if not image_dir.is_dir():
        print(f"ERROR: image_dir 不存在或不是目录: {image_dir}", file=sys.stderr)
        return 2
    if not calib_yaml.is_file():
        print(f"ERROR: calib_yaml 不存在或不是文件: {calib_yaml}", file=sys.stderr)
        return 2

    try:
        convert(
            image_dir=image_dir,
            calib_yaml=calib_yaml,
            out_dir=out_dir,
            exposure=float(args.exposure),
            ext=str(args.ext),
            strict_size=(not bool(args.skip_bad_size)),
        )
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    print(f"OK: 输出到 {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

