#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as _dt
import os
import re
import struct
import sys
import threading
import queue
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

def _maybe_copy_pcalib(out_dir: Path) -> bool:
    repo = _repo_root_from_this_file()
    src = repo / "data" / "sequence_08" / "pcalib.txt"
    dst = out_dir / "pcalib.txt"
    if src.is_file():
        dst.write_bytes(src.read_bytes())
        return True
    return False


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

def _write_times_txt(out_dir: Path, rows: Sequence[Tuple[int, float]], exposure: Optional[float]) -> None:
    lines: List[str] = []
    if exposure is None:
        for idx, ts in rows:
            lines.append(f"{idx:06d} {ts:.16f}")
    else:
        if exposure <= 0:
            raise ValueError("exposure must be > 0")
        for idx, ts in rows:
            lines.append(f"{idx:06d} {ts:.16f} {exposure}")
    (out_dir / "times.txt").write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def convert(
    image_dir: Path,
    calib_yaml: Path,
    out_dir: Path,
    exposure: float,
    ext: str,
    strict_size: bool,
    photometric_policy: str = "skip",  # skip | fake
    exposure_policy: str = "constant",  # constant | disable
    constant_exposure: float = 0.01,
    pcalib_policy: str = "copy_seq08",  # copy_seq08 | identity | skip
) -> None:
    if ext not in ("jpg", "pgm"):
        raise ValueError("--ext 仅支持 jpg 或 pgm")

    calib = _read_calib_yaml(calib_yaml)

    # If input isn't YUV, no conversion needed: just copy & rename.
    has_yuv = any(p.is_file() and p.suffix.lower() == ".yuv" for p in image_dir.iterdir())

    out_dir.mkdir(parents=True, exist_ok=True)
    images_out = out_dir / "images"
    images_out.mkdir(parents=True, exist_ok=True)

    # write meta files (camera always)
    _write_camera_txt(out_dir, calib)

    # photometric files (optional)
    if photometric_policy == "fake":
        if pcalib_policy == "copy_seq08":
            copied = _maybe_copy_pcalib(out_dir)
            if not copied:
                # fall back to identity unless user explicitly wanted skip
                line = " ".join(str(i) for i in range(256)) + "\n\n"
                (out_dir / "pcalib.txt").write_text(line, encoding="utf-8")
        elif pcalib_policy == "identity":
            line = " ".join(str(i) for i in range(256)) + "\n\n"
            (out_dir / "pcalib.txt").write_text(line, encoding="utf-8")
        elif pcalib_policy == "skip":
            pass
        else:
            raise ValueError(f"unknown pcalib_policy: {pcalib_policy}")

        _write_vignette_placeholder(out_dir, calib)
    elif photometric_policy == "skip":
        pass
    else:
        raise ValueError(f"unknown photometric_policy: {photometric_policy}")

    times_lines: List[str] = []
    times_rows: List[Tuple[int, float]] = []
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
            times_rows.append((idx, ts))
    else:
        src_files = _list_timestamped_image_files(image_dir)
        for idx, src_path in enumerate(src_files):
            ts = _parse_timestamp_from_stem(src_path.stem)
            # Convert/copy to desired ext (default: jpg in GUI).
            dst_name = f"{idx:06d}.{ext}"
            dst_path = images_out / dst_name
            if ext == "pgm":
                # For non-yuv input, pgm output isn't supported without decoding; fall back to raw copy if already pgm.
                if src_path.suffix.lower() == ".pgm":
                    dst_path.write_bytes(src_path.read_bytes())
                else:
                    raise RuntimeError("输入为非YUV时输出 pgm 不支持；请改用 jpg")
            else:
                # Save to JPG: decode with Pillow/OpenCV, then write.
                _convert_image_to_jpg_gray(src_path, dst_path, quality=95)
            times_rows.append((idx, ts))

    # times.txt exposure policy
    if exposure_policy == "disable":
        _write_times_txt(out_dir, times_rows, exposure=None)
    elif exposure_policy == "constant":
        _write_times_txt(out_dir, times_rows, exposure=float(constant_exposure))
    else:
        raise ValueError(f"unknown exposure_policy: {exposure_policy}")


def _convert_image_to_jpg_gray(src_path: Path, dst_path: Path, quality: int = 95) -> None:
    if not (1 <= quality <= 100):
        raise ValueError("quality must be 1..100")
    # Prefer Pillow; fallback to OpenCV.
    try:
        from PIL import Image  # type: ignore

        img = Image.open(str(src_path))
        img = img.convert("L")
        img.save(str(dst_path), format="JPEG", quality=int(quality), optimize=True)
        return
    except Exception:
        pass

    try:
        import cv2  # type: ignore
        import numpy as np  # type: ignore

        m = cv2.imread(str(src_path), cv2.IMREAD_GRAYSCALE)
        if m is None:
            raise RuntimeError("cv2.imread failed")
        ok = cv2.imwrite(str(dst_path), m, [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)])
        if not ok:
            raise RuntimeError("cv2.imwrite failed")
        return
    except Exception:
        raise RuntimeError(
            "非YUV输入转 jpg 需要 Pillow 或 opencv-python(+numpy)。请安装其一：\n"
            "  - pip install pillow\n"
            "  - 或 pip install opencv-python numpy"
        )


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="将 BSD bsd_1030 (YUV420, 文件名为时间戳) 转成 DSO 兼容的 TUM 风格序列目录。"
    )
    p.add_argument("image_dir", type=str, help="BSD 图片目录（例如 data/bsd_1030/left_images）")
    p.add_argument("calib_yaml", type=str, help="标定文件（例如 data/bsd_1030/calibration_config.yaml）")
    p.add_argument("--out", type=str, default="", help="输出目录（默认 data/bsd_tum_YYYYMMDD）")
    p.add_argument("--ext", type=str, default="jpg", choices=["jpg", "pgm"], help="输出图像格式（默认 jpg）")
    p.add_argument(
        "--skip-bad-size",
        action="store_true",
        help="遇到大小不匹配的 yuv 时跳过该帧（默认遇到即报错退出）",
    )
    p.add_argument("--photometric", type=str, default="skip", choices=["skip", "fake"], help="光度标定文件策略：skip 或 fake")
    p.add_argument("--pcalib", type=str, default="copy_seq08", choices=["copy_seq08", "identity", "skip"], help="pcalib.txt 生成策略（当 photometric=fake 时）")
    p.add_argument("--exposure-policy", type=str, default="constant", choices=["constant", "disable"], help="times.txt 曝光策略")
    p.add_argument("--constant-exposure", type=float, default=0.01, help="统一曝光值（当 exposure-policy=constant）")
    p.add_argument("--gui", action="store_true", help="强制启动 GUI（忽略位置参数）")
    return p


def _has_cli_positionals(argv: Sequence[str]) -> bool:
    # if user passed 2 positionals (image_dir calib_yaml)
    # We'll detect by counting non-flag args after parsing isn't available without errors.
    nonflags = [a for a in argv if not a.startswith("-")]
    return len(nonflags) >= 2


def main(argv: Optional[Sequence[str]] = None) -> int:
    argv = list(argv) if argv is not None else sys.argv[1:]
    # GUI-first: if no args, or user explicitly asked for --gui, start GUI.
    if len(argv) == 0 or "--gui" in argv:
        return _run_gui()

    # Otherwise, keep CLI behavior: require image_dir calib_yaml.
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
            exposure=0.0,
            ext=str(args.ext),
            strict_size=(not bool(args.skip_bad_size)),
            photometric_policy=str(args.photometric),
            exposure_policy=str(args.exposure_policy),
            constant_exposure=float(args.constant_exposure),
            pcalib_policy=str(args.pcalib),
        )
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    print(f"OK: 输出到 {out_dir}")
    return 0


def _run_gui() -> int:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    repo = _repo_root_from_this_file()

    class Gui(tk.Tk):
        def __init__(self) -> None:
            super().__init__()
            self.title("BSD data -> TUM (DSO) converter")
            self.geometry("1100x860")
            self.minsize(980, 760)

            self.var_image_dir = tk.StringVar(value=str(repo / "data" / "bsd_1030" / "left_images"))
            self.var_calib_yaml = tk.StringVar(value=str(repo / "data" / "bsd_1030" / "calibration_config.yaml"))
            self.var_out_dir = tk.StringVar(value="")

            self.var_ext = tk.StringVar(value="jpg")

            # photometric policies
            self.var_photo = tk.StringVar(value="skip")  # skip/fake
            self.var_pcalib = tk.StringVar(value="copy_seq08")  # copy_seq08/identity/skip

            # exposure policies
            self.var_exposure_policy = tk.StringVar(value="constant")  # constant/disable
            self.var_constant_exposure = tk.DoubleVar(value=0.01)

            self.var_skip_bad_size = tk.BooleanVar(value=False)

            self._q: "queue.Queue[str]" = queue.Queue()
            self._worker: Optional[threading.Thread] = None

            self._build_ui()
            self.after(100, self._tick)

        def _build_ui(self) -> None:
            frm = ttk.Frame(self, padding=10)
            frm.pack(fill=tk.BOTH, expand=True)

            grid = ttk.Frame(frm)
            grid.pack(fill=tk.X)
            grid.columnconfigure(1, weight=1)

            def add_row(r: int, label: str, var: tk.StringVar, choose_dir: bool) -> None:
                ttk.Label(grid, text=label).grid(row=r, column=0, sticky="w", padx=(0, 6), pady=3)
                ttk.Entry(grid, textvariable=var, width=90).grid(row=r, column=1, sticky="we", pady=3)

                def _browse() -> None:
                    if choose_dir:
                        p = filedialog.askdirectory(initialdir=var.get() or repo)
                    else:
                        p = filedialog.askopenfilename(initialdir=var.get() or repo)
                    if p:
                        var.set(p)

                ttk.Button(grid, text="Browse", command=_browse).grid(row=r, column=2, padx=(6, 0), pady=3)

            add_row(0, "image_dir", self.var_image_dir, choose_dir=True)
            add_row(1, "calibration_config.yaml", self.var_calib_yaml, choose_dir=False)
            add_row(2, "out_dir (optional)", self.var_out_dir, choose_dir=True)

            opt = ttk.LabelFrame(frm, text="Options", padding=10)
            opt.pack(fill=tk.X, pady=(10, 0))

            ttk.Label(opt, text="output images").grid(row=0, column=0, sticky="w")
            ttk.Combobox(opt, values=["jpg", "pgm"], textvariable=self.var_ext, state="readonly", width=8).grid(
                row=0, column=1, sticky="w", padx=(6, 20)
            )
            ttk.Checkbutton(opt, text="skip bad yuv size", variable=self.var_skip_bad_size).grid(row=0, column=2, sticky="w")

            photo = ttk.LabelFrame(frm, text="Photometric files", padding=10)
            photo.pack(fill=tk.X, pady=(10, 0))
            ttk.Radiobutton(photo, text="Do not generate pcalib/vignette", value="skip", variable=self.var_photo).grid(
                row=0, column=0, sticky="w"
            )
            ttk.Radiobutton(photo, text="Fake photometric files (generate)", value="fake", variable=self.var_photo).grid(
                row=1, column=0, sticky="w", pady=(6, 0)
            )
            ttk.Label(photo, text="pcalib policy:").grid(row=1, column=1, sticky="e", padx=(10, 0), pady=(6, 0))
            ttk.Combobox(photo, values=["copy_seq08", "identity", "skip"], textvariable=self.var_pcalib, state="readonly", width=12).grid(
                row=1, column=2, sticky="w", padx=(6, 0), pady=(6, 0)
            )
            ttk.Label(photo, text="vignette: flat 255").grid(row=1, column=3, sticky="w", padx=(10, 0), pady=(6, 0))

            exp = ttk.LabelFrame(frm, text="Exposure / times.txt", padding=10)
            exp.pack(fill=tk.X, pady=(10, 0))
            ttk.Radiobutton(exp, text="Constant exposure (3-column times.txt)", value="constant", variable=self.var_exposure_policy).grid(
                row=0, column=0, sticky="w"
            )
            ttk.Label(exp, text="exposure=").grid(row=0, column=1, sticky="e", padx=(10, 0))
            ttk.Entry(exp, width=10, textvariable=self.var_constant_exposure).grid(row=0, column=2, sticky="w", padx=(6, 0))
            ttk.Radiobutton(exp, text="Disable exposure (2-column times.txt)", value="disable", variable=self.var_exposure_policy).grid(
                row=1, column=0, sticky="w", pady=(6, 0)
            )

            # actions
            btns = ttk.Frame(frm)
            btns.pack(fill=tk.X, pady=(10, 0))
            ttk.Button(btns, text="Convert", command=self._start_convert).pack(side=tk.LEFT)

            # log
            logfrm = ttk.LabelFrame(frm, text="Log", padding=10)
            logfrm.pack(fill=tk.BOTH, expand=True, pady=(10, 0))
            self.txt = tk.Text(logfrm, height=18, wrap="word")
            self.txt.pack(fill=tk.BOTH, expand=True)

        def _log(self, s: str) -> None:
            self.txt.insert("end", s)
            self.txt.see("end")

        def _start_convert(self) -> None:
            if self._worker is not None and self._worker.is_alive():
                messagebox.showinfo("BSD->TUM", "Conversion is still running.")
                return

            try:
                image_dir = Path(self.var_image_dir.get()).expanduser().resolve()
                calib_yaml = Path(self.var_calib_yaml.get()).expanduser().resolve()
                out_dir = Path(self.var_out_dir.get()).expanduser().resolve() if self.var_out_dir.get().strip() else _default_out_dir(repo)
                ext = str(self.var_ext.get())
                photometric_policy = str(self.var_photo.get())
                pcalib_policy = str(self.var_pcalib.get())
                exposure_policy = str(self.var_exposure_policy.get())
                constant_exposure = float(self.var_constant_exposure.get())
                strict_size = not bool(self.var_skip_bad_size.get())
            except Exception as e:
                messagebox.showerror("Invalid config", str(e))
                return

            if not image_dir.is_dir():
                messagebox.showerror("Invalid config", f"image_dir is not a directory: {image_dir}")
                return
            if not calib_yaml.is_file():
                messagebox.showerror("Invalid config", f"calibration_config.yaml not found: {calib_yaml}")
                return

            self._log(f"[config]\nimage_dir={image_dir}\ncalib_yaml={calib_yaml}\nout_dir={out_dir}\next={ext}\n")
            self._log(f"photometric={photometric_policy}, pcalib={pcalib_policy}, exposure_policy={exposure_policy}\n\n")

            def _work() -> None:
                try:
                    convert(
                        image_dir=image_dir,
                        calib_yaml=calib_yaml,
                        out_dir=out_dir,
                        exposure=0.0,
                        ext=ext,
                        strict_size=strict_size,
                        photometric_policy=photometric_policy,
                        exposure_policy=exposure_policy,
                        constant_exposure=constant_exposure,
                        pcalib_policy=pcalib_policy,
                    )
                    self._q.put(f"OK: wrote {out_dir}\n")
                except Exception as e:
                    self._q.put(f"ERROR: {e}\n")

            self._worker = threading.Thread(target=_work, daemon=True)
            self._worker.start()

        def _tick(self) -> None:
            while True:
                try:
                    s = self._q.get_nowait()
                except queue.Empty:
                    break
                self._log(s)
            self.after(100, self._tick)

    app = Gui()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

