#!/usr/bin/env python3
from __future__ import annotations

import os
import queue
import re
import shlex
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple

import tkinter as tk
from tkinter import filedialog, messagebox, ttk


def _repo_root_from_this_file() -> Path:
    return Path(__file__).resolve().parents[1]


def _is_image_file(p: Path) -> bool:
    return p.is_file() and p.suffix.lower() in {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".pgm"}


def _parse_timestamp_from_stem(stem: str) -> float:
    # BSD style: "1752468078.268287181854248"
    try:
        return float(stem)
    except ValueError:
        # fallback: try to find first float in string
        m = re.search(r"([+-]?\d+(?:\.\d+)?)", stem)
        if not m:
            raise ValueError(f"Cannot parse timestamp from filename: {stem!r}")
        return float(m.group(1))


def _sorted_images(images_dir: Path) -> List[Path]:
    files = [p for p in images_dir.iterdir() if _is_image_file(p)]
    files.sort(key=lambda p: p.name)
    return files


def _guess_images_dir(root: Path) -> Optional[Path]:
    for name in ("images", "left_images", "right_images"):
        cand = root / name
        if cand.is_dir():
            imgs = _sorted_images(cand)
            if imgs:
                return cand
    return None


def _guess_file(root: Path, filename: str) -> Optional[Path]:
    cand = root / filename
    return cand if cand.is_file() else None


def _read_times_any(path: Path) -> List[Tuple[int, float]]:
    """
    Returns list of (id, timestamp). Accepts 2 or 3 columns.
    """
    out: List[Tuple[int, float]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        try:
            idx = int(parts[0])
            ts = float(parts[1])
        except ValueError:
            continue
        out.append((idx, ts))
    return out


def _generate_times_from_filenames(images_dir: Path) -> List[Tuple[int, float]]:
    imgs = _sorted_images(images_dir)
    out: List[Tuple[int, float]] = []
    for i, p in enumerate(imgs):
        ts = _parse_timestamp_from_stem(p.stem)
        out.append((i, ts))
    return out


def _write_times(path: Path, rows: Sequence[Tuple[int, float]], exposure: Optional[float]) -> None:
    lines: List[str] = []
    if exposure is None:
        for idx, ts in rows:
            lines.append(f"{idx:06d} {ts:.16f}")
    else:
        if exposure <= 0:
            raise ValueError("Constant exposure must be > 0")
        for idx, ts in rows:
            lines.append(f"{idx:06d} {ts:.16f} {exposure}")
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


@dataclass
class RunConfig:
    dataset_root: Path
    images_dir: Path
    camera_txt: Path
    pcalib_txt: Optional[Path]
    vignette_png: Optional[Path]
    times_txt: Optional[Path]
    output_dir: Optional[Path]
    keep_result_txt: bool
    save_log: bool
    preset: int
    mode: int
    nogui: bool
    reverse: bool
    start: int
    end: int
    speed: float
    quiet: bool
    sampleoutput: bool
    extra_kv: str
    exposure_policy: str  # "use_original" | "constant" | "disable"
    constant_exposure: float


def _recommend_mode(pcalib: Optional[Path], vignette: Optional[Path]) -> int:
    if pcalib is not None and vignette is not None:
        return 0
    return 1


def _build_command(exe: Path, files_dir: Path, cfg: RunConfig, calib: Path, gamma: Optional[Path], vignette: Optional[Path]) -> List[str]:
    args: List[str] = [str(exe)]
    args.append(f"files={str(files_dir)}")
    args.append(f"calib={str(calib)}")
    if gamma is not None:
        args.append(f"gamma={str(gamma)}")
    if vignette is not None:
        args.append(f"vignette={str(vignette)}")
    args.append(f"preset={int(cfg.preset)}")
    args.append(f"mode={int(cfg.mode)}")
    args.append(f"start={int(cfg.start)}")
    args.append(f"end={int(cfg.end)}")
    if cfg.nogui:
        args.append("nogui=1")
    if cfg.reverse:
        args.append("reverse=1")
    if cfg.speed != 0:
        args.append(f"speed={float(cfg.speed)}")
    if cfg.quiet:
        args.append("quiet=1")
    if cfg.sampleoutput:
        args.append("sampleoutput=1")

    extra = (cfg.extra_kv or "").strip()
    if extra:
        # allow user to paste: key=value key2=value2 ...
        for token in shlex.split(extra):
            if token:
                args.append(token)
    return args


class ProcRunner:
    def __init__(self) -> None:
        self._proc: Optional[subprocess.Popen[str]] = None
        self._thread: Optional[threading.Thread] = None
        self._q: "queue.Queue[str]" = queue.Queue()
        self._log_fp: Optional[object] = None

    def is_running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self, cmd: List[str], cwd: Path, log_path: Optional[Path] = None) -> None:
        if self.is_running():
            raise RuntimeError("Process is still running")

        if log_path is not None:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            self._log_fp = open(log_path, "w", encoding="utf-8", buffering=1)
        else:
            self._log_fp = None

        self._proc = subprocess.Popen(
            cmd,
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            universal_newlines=True,
        )

        def _reader() -> None:
            assert self._proc is not None
            try:
                if self._proc.stdout is None:
                    return
                for line in self._proc.stdout:
                    self._q.put(line)
                    if self._log_fp is not None:
                        try:
                            self._log_fp.write(line)
                        except Exception:
                            pass
            finally:
                rc = self._proc.poll()
                self._q.put(f"\n[process exited] code={rc}\n")
                if self._log_fp is not None:
                    try:
                        self._log_fp.write(f"\n[process exited] code={rc}\n")
                        self._log_fp.flush()
                        self._log_fp.close()
                    except Exception:
                        pass
                    self._log_fp = None

        self._thread = threading.Thread(target=_reader, daemon=True)
        self._thread.start()

    def terminate(self) -> None:
        if self._proc is None:
            return
        if self._proc.poll() is not None:
            return
        try:
            self._proc.terminate()
        except Exception:
            pass

    def drain(self) -> List[str]:
        out: List[str] = []
        while True:
            try:
                out.append(self._q.get_nowait())
            except queue.Empty:
                break
        return out


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("DSO Runner")
        self.geometry("1100x1000")
        self.minsize(980, 760)

        self.repo_root = _repo_root_from_this_file()
        self.runner = ProcRunner()

        # state vars
        self.var_root = tk.StringVar(value=str(self.repo_root / "data" / "bsd_forest"))
        self.var_images = tk.StringVar(value="")
        self.var_camera = tk.StringVar(value="")
        self.var_pcalib = tk.StringVar(value="")
        self.var_vignette = tk.StringVar(value="")
        self.var_times = tk.StringVar(value="")
        self.var_output_dir = tk.StringVar(value="")
        self.var_keep_result = tk.BooleanVar(value=True)
        self.var_save_log = tk.BooleanVar(value=True)

        self.var_preset = tk.IntVar(value=0)
        self.var_mode = tk.IntVar(value=1)
        self.var_nogui = tk.BooleanVar(value=False)
        self.var_reverse = tk.BooleanVar(value=False)
        self.var_start = tk.IntVar(value=0)
        self.var_end = tk.IntVar(value=100000)
        self.var_speed = tk.DoubleVar(value=0.0)
        self.var_quiet = tk.BooleanVar(value=False)
        self.var_sampleoutput = tk.BooleanVar(value=False)

        self.var_exposure_policy = tk.StringVar(value="disable")
        self.var_constant_exposure = tk.DoubleVar(value=0.01)

        self.var_extra = tk.StringVar(value="")
        self.var_reco = tk.StringVar(value="")

        self._tmp_parent: Optional[Path] = None
        self._suppress_preview: bool = False
        self._preview_dirty: bool = False
        self._cmd_text: Optional[tk.Text] = None
        self._result_hint_var = tk.StringVar(value="")
        self._log_hint_var = tk.StringVar(value="")

        self._build_ui()
        self._autodetect_from_root(Path(self.var_root.get()))
        self._refresh_preview()
        self.after(100, self._tick)

    def _build_ui(self) -> None:
        frm = ttk.Frame(self, padding=10)
        frm.pack(fill=tk.BOTH, expand=True)

        # top inputs
        grid = ttk.Frame(frm)
        grid.pack(fill=tk.X)

        def add_path_row(r: int, label: str, var: tk.StringVar, choose_dir: bool = False, optional: bool = False) -> None:
            ttk.Label(grid, text=label).grid(row=r, column=0, sticky="w", padx=(0, 6), pady=3)
            ent = ttk.Entry(grid, textvariable=var, width=90)
            ent.grid(row=r, column=1, sticky="we", pady=3)

            def _browse() -> None:
                if choose_dir:
                    p = filedialog.askdirectory(initialdir=var.get() or self.repo_root)
                else:
                    p = filedialog.askopenfilename(initialdir=var.get() or self.repo_root)
                if p:
                    var.set(p)
                    if label.startswith("dataset_root"):
                        self._autodetect_from_root(Path(p))
                    self._refresh_preview()

            ttk.Button(grid, text="Browse", command=_browse).grid(row=r, column=2, padx=(6, 0), pady=3)
            if optional:
                ttk.Label(grid, text="(optional)").grid(row=r, column=3, sticky="w", padx=(6, 0))

        grid.columnconfigure(1, weight=1)

        add_path_row(0, "dataset_root", self.var_root, choose_dir=True)
        add_path_row(1, "images_dir", self.var_images, choose_dir=True)
        add_path_row(2, "camera.txt", self.var_camera, choose_dir=False)
        add_path_row(3, "pcalib.txt", self.var_pcalib, choose_dir=False, optional=True)
        add_path_row(4, "vignette.png", self.var_vignette, choose_dir=False, optional=True)
        add_path_row(5, "times.txt", self.var_times, choose_dir=False, optional=True)

        # output dir + result.txt handling
        def _browse_out() -> None:
            p = filedialog.askdirectory(initialdir=self.var_output_dir.get() or self.repo_root)
            if p:
                self.var_output_dir.set(p)
                self._refresh_preview()

        ttk.Label(grid, text="output_dir").grid(row=6, column=0, sticky="w", padx=(0, 6), pady=3)
        ttk.Entry(grid, textvariable=self.var_output_dir, width=90).grid(row=6, column=1, sticky="we", pady=3)
        ttk.Button(grid, text="Browse", command=_browse_out).grid(row=6, column=2, padx=(6, 0), pady=3)
        ttk.Label(grid, text="(optional)").grid(row=6, column=3, sticky="w", padx=(6, 0))

        ttk.Checkbutton(grid, text="keep result.txt", variable=self.var_keep_result, command=self._refresh_preview).grid(
            row=7, column=1, sticky="w", pady=(3, 0)
        )
        ttk.Label(grid, textvariable=self._result_hint_var, foreground="#555555").grid(
            row=8, column=1, columnspan=3, sticky="w", pady=(0, 4)
        )

        ttk.Checkbutton(grid, text="save log to file", variable=self.var_save_log, command=self._refresh_preview).grid(
            row=9, column=1, sticky="w", pady=(3, 0)
        )
        ttk.Label(grid, textvariable=self._log_hint_var, foreground="#555555").grid(
            row=10, column=1, columnspan=3, sticky="w", pady=(0, 4)
        )

        # options row
        opt = ttk.LabelFrame(frm, text="Run options", padding=10)
        opt.pack(fill=tk.X, pady=(10, 0))

        ttk.Label(opt, text="preset").grid(row=0, column=0, sticky="w")
        ttk.Combobox(opt, values=[0, 1, 2, 3], width=6, textvariable=self.var_preset, state="readonly").grid(
            row=0, column=1, sticky="w", padx=(6, 20)
        )
        ttk.Label(opt, text="mode").grid(row=0, column=2, sticky="w")
        ttk.Combobox(opt, values=[0, 1, 2], width=6, textvariable=self.var_mode, state="readonly").grid(
            row=0, column=3, sticky="w", padx=(6, 20)
        )

        ttk.Checkbutton(opt, text="nogui", variable=self.var_nogui, command=self._refresh_preview).grid(
            row=0, column=4, sticky="w", padx=(0, 12)
        )
        ttk.Checkbutton(opt, text="reverse", variable=self.var_reverse, command=self._refresh_preview).grid(
            row=0, column=5, sticky="w", padx=(0, 12)
        )
        ttk.Checkbutton(opt, text="quiet", variable=self.var_quiet, command=self._refresh_preview).grid(
            row=0, column=6, sticky="w", padx=(0, 12)
        )
        ttk.Checkbutton(opt, text="sampleoutput", variable=self.var_sampleoutput, command=self._refresh_preview).grid(
            row=0, column=7, sticky="w"
        )

        ttk.Label(opt, text="start").grid(row=1, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(opt, width=8, textvariable=self.var_start).grid(row=1, column=1, sticky="w", padx=(6, 20), pady=(8, 0))
        ttk.Label(opt, text="end").grid(row=1, column=2, sticky="w", pady=(8, 0))
        ttk.Entry(opt, width=8, textvariable=self.var_end).grid(row=1, column=3, sticky="w", padx=(6, 20), pady=(8, 0))
        ttk.Label(opt, text="speed(0=fast)").grid(row=1, column=4, sticky="w", pady=(8, 0))
        ttk.Entry(opt, width=10, textvariable=self.var_speed).grid(row=1, column=5, sticky="w", padx=(6, 0), pady=(8, 0))

        # exposure policy
        exp = ttk.LabelFrame(frm, text="Exposure policy (times.txt third column)", padding=10)
        exp.pack(fill=tk.X, pady=(10, 0))

        def _on_exp_change() -> None:
            self._refresh_preview()

        ttk.Radiobutton(exp, text="Disable exposure (generate 2-column times)", value="disable", variable=self.var_exposure_policy, command=_on_exp_change).grid(
            row=0, column=0, sticky="w"
        )
        ttk.Radiobutton(exp, text="Use original times.txt (copy as-is)", value="use_original", variable=self.var_exposure_policy, command=_on_exp_change).grid(
            row=1, column=0, sticky="w", pady=(6, 0)
        )
        ttk.Radiobutton(exp, text="Constant exposure (generate 3-column times)", value="constant", variable=self.var_exposure_policy, command=_on_exp_change).grid(
            row=2, column=0, sticky="w", pady=(6, 0)
        )
        ttk.Label(exp, text="exposure=").grid(row=2, column=1, sticky="e", padx=(10, 0), pady=(6, 0))
        ttk.Entry(exp, width=10, textvariable=self.var_constant_exposure).grid(row=2, column=2, sticky="w", padx=(6, 0), pady=(6, 0))

        # extra args
        extra = ttk.LabelFrame(frm, text="Extra args (optional, space-separated, e.g. prefetch=1 rescale=1)", padding=10)
        extra.pack(fill=tk.X, pady=(10, 0))
        ttk.Entry(extra, textvariable=self.var_extra).pack(fill=tk.X)

        # recommendation and command preview
        rec = ttk.Frame(frm)
        rec.pack(fill=tk.X, pady=(10, 0))
        ttk.Label(rec, textvariable=self.var_reco, foreground="#1f6feb").pack(anchor="w")
        ttk.Label(rec, text="Command preview (copy to terminal):").pack(anchor="w", pady=(6, 0))
        cmd_box = ttk.Frame(rec)
        cmd_box.pack(fill=tk.X)
        yscroll = ttk.Scrollbar(cmd_box, orient="vertical")
        yscroll.pack(side=tk.RIGHT, fill=tk.Y)
        txt = tk.Text(cmd_box, height=3, wrap="word", yscrollcommand=yscroll.set)
        txt.pack(side=tk.LEFT, fill=tk.X, expand=True)
        yscroll.config(command=txt.yview)
        self._cmd_text = txt

        # If user edits the preview manually, don't overwrite it automatically.
        def _on_preview_edited(_evt: object) -> None:
            if self._suppress_preview:
                return
            self._preview_dirty = True

        txt.bind("<KeyRelease>", _on_preview_edited)

        # run buttons
        btns = ttk.Frame(frm)
        btns.pack(fill=tk.X, pady=(10, 0))
        ttk.Button(btns, text="Refresh Preview", command=self._refresh_preview).pack(side=tk.LEFT)
        ttk.Button(btns, text="Run", command=self._run).pack(side=tk.LEFT, padx=(10, 0))
        ttk.Button(btns, text="Stop", command=self._stop).pack(side=tk.LEFT, padx=(10, 0))

        # log output
        logfrm = ttk.LabelFrame(frm, text="Output log", padding=10)
        logfrm.pack(fill=tk.BOTH, expand=True, pady=(10, 0))
        self.txt = tk.Text(logfrm, height=18, wrap="word")
        self.txt.pack(fill=tk.BOTH, expand=True)

        # bindings
        for v in (
            self.var_root,
            self.var_images,
            self.var_camera,
            self.var_pcalib,
            self.var_vignette,
            self.var_times,
            self.var_output_dir,
            self.var_preset,
            self.var_mode,
            self.var_start,
            self.var_end,
            self.var_speed,
            self.var_extra,
        ):
            v.trace_add("write", lambda *_: (None if self._preview_dirty else self._refresh_preview()))

    def _log(self, s: str) -> None:
        self.txt.insert("end", s)
        self.txt.see("end")

    def _autodetect_from_root(self, root: Path) -> None:
        try:
            root = root.expanduser().resolve()
        except Exception:
            return

        images = _guess_images_dir(root)
        if images is not None:
            self.var_images.set(str(images))

        cam = _guess_file(root, "camera.txt")
        if cam is not None:
            self.var_camera.set(str(cam))

        pc = _guess_file(root, "pcalib.txt")
        if pc is not None:
            self.var_pcalib.set(str(pc))

        vig = _guess_file(root, "vignette.png")
        if vig is not None:
            self.var_vignette.set(str(vig))

        t = _guess_file(root, "times.txt")
        if t is not None:
            self.var_times.set(str(t))

        # recommend mode
        rec_mode = _recommend_mode(pc, vig)
        self.var_mode.set(int(rec_mode))

    def _collect_config(self) -> RunConfig:
        root = Path(self.var_root.get()).expanduser().resolve()
        images = Path(self.var_images.get()).expanduser().resolve()
        camera = Path(self.var_camera.get()).expanduser().resolve()

        pcalib_s = self.var_pcalib.get().strip()
        vignette_s = self.var_vignette.get().strip()
        times_s = self.var_times.get().strip()
        out_s = self.var_output_dir.get().strip()

        pcalib = Path(pcalib_s).expanduser().resolve() if pcalib_s else None
        vignette = Path(vignette_s).expanduser().resolve() if vignette_s else None
        times = Path(times_s).expanduser().resolve() if times_s else None
        out_dir = Path(out_s).expanduser().resolve() if out_s else None

        return RunConfig(
            dataset_root=root,
            images_dir=images,
            camera_txt=camera,
            pcalib_txt=pcalib,
            vignette_png=vignette,
            times_txt=times,
            output_dir=out_dir,
            keep_result_txt=bool(self.var_keep_result.get()),
            save_log=bool(self.var_save_log.get()),
            preset=int(self.var_preset.get()),
            mode=int(self.var_mode.get()),
            nogui=bool(self.var_nogui.get()),
            reverse=bool(self.var_reverse.get()),
            start=int(self.var_start.get()),
            end=int(self.var_end.get()),
            speed=float(self.var_speed.get()),
            quiet=bool(self.var_quiet.get()),
            sampleoutput=bool(self.var_sampleoutput.get()),
            extra_kv=str(self.var_extra.get()),
            exposure_policy=str(self.var_exposure_policy.get()),
            constant_exposure=float(self.var_constant_exposure.get()),
        )

    def _validate(self, cfg: RunConfig) -> Optional[str]:
        if not cfg.images_dir.is_dir():
            return f"images_dir is not a directory: {cfg.images_dir}"
        if not _sorted_images(cfg.images_dir):
            return f"No image files found in images_dir: {cfg.images_dir}"
        if not cfg.camera_txt.is_file():
            return f"camera.txt not found: {cfg.camera_txt}"
        if cfg.pcalib_txt is not None and not cfg.pcalib_txt.is_file():
            return f"Invalid pcalib.txt path: {cfg.pcalib_txt}"
        if cfg.vignette_png is not None and not cfg.vignette_png.is_file():
            return f"Invalid vignette.png path: {cfg.vignette_png}"
        if cfg.times_txt is not None and not cfg.times_txt.is_file():
            return f"Invalid times.txt path: {cfg.times_txt}"
        if cfg.output_dir is not None and not cfg.output_dir.is_dir():
            return f"output_dir is not a directory: {cfg.output_dir}"
        if cfg.mode == 0 and (cfg.pcalib_txt is None or cfg.vignette_png is None):
            return "mode=0 requires BOTH pcalib.txt and vignette.png; otherwise use mode=1 or mode=2"
        if cfg.start < 0 or cfg.end < 0:
            return "start/end must be non-negative"
        return None

    def _prepare_temp_dataset(self, cfg: RunConfig) -> Tuple[Path, Path]:
        """
        Creates a temporary parent directory containing:
          - images/ -> symlink to cfg.images_dir
          - times.txt -> generated per exposure policy
        Returns (temp_parent, temp_images_dir)
        """
        tmp_parent = Path(tempfile.mkdtemp(prefix="dso_run_"))
        tmp_images = tmp_parent / "images"
        os.symlink(str(cfg.images_dir), str(tmp_images))

        # Determine times base rows
        rows: List[Tuple[int, float]]
        if cfg.times_txt is not None and cfg.times_txt.is_file():
            rows = _read_times_any(cfg.times_txt)
            if not rows:
                rows = _generate_times_from_filenames(cfg.images_dir)
        else:
            rows = _generate_times_from_filenames(cfg.images_dir)

        if cfg.exposure_policy == "use_original":
            if cfg.times_txt is not None and cfg.times_txt.is_file():
                (tmp_parent / "times.txt").write_bytes(cfg.times_txt.read_bytes())
            else:
                # no original times; fall back to disable exposure
                _write_times(tmp_parent / "times.txt", rows, exposure=None)
        elif cfg.exposure_policy == "constant":
            _write_times(tmp_parent / "times.txt", rows, exposure=float(cfg.constant_exposure))
        else:  # disable
            _write_times(tmp_parent / "times.txt", rows, exposure=None)

        return tmp_parent, tmp_images

    def _refresh_preview(self) -> None:
        if self._suppress_preview:
            return
        try:
            cfg = self._collect_config()
        except Exception:
            return

        rec_mode = _recommend_mode(cfg.pcalib_txt, cfg.vignette_png)

        # If user selected mode=0 but photometric files are missing, auto-switch.
        if cfg.mode == 0 and rec_mode != 0:
            self._log("[auto] mode switched to 1 because pcalib.txt/vignette.png is missing.\n\n")
            self._suppress_preview = True
            try:
                self.var_mode.set(int(rec_mode))
            finally:
                self._suppress_preview = False
            cfg = self._collect_config()

        reco = f"Recommended: mode={rec_mode} ({'with' if rec_mode==0 else 'without'} photometric calibration)"
        if cfg.mode != rec_mode:
            reco += f"; current mode={cfg.mode}"
        self.var_reco.set(reco)

        # If user edited preview manually, keep it until they click Refresh Preview.
        # Refresh Preview explicitly regenerates, so clear dirty flag here.
        self._preview_dirty = False

        exe = self.repo_root / "build" / "bin" / "dso_dataset"
        # We preview using the temp images dir path placeholder
        preview_files = "<temp>/images"
        args = [str(exe), f"files={preview_files}", f"calib={cfg.camera_txt}"]
        if cfg.pcalib_txt is not None:
            args.append(f"gamma={cfg.pcalib_txt}")
        if cfg.vignette_png is not None:
            args.append(f"vignette={cfg.vignette_png}")
        args += [
            f"preset={cfg.preset}",
            f"mode={cfg.mode}",
            f"start={cfg.start}",
            f"end={cfg.end}",
        ]
        if cfg.nogui:
            args.append("nogui=1")
        if cfg.reverse:
            args.append("reverse=1")
        if cfg.speed != 0:
            args.append(f"speed={cfg.speed}")
        if cfg.quiet:
            args.append("quiet=1")
        if cfg.sampleoutput:
            args.append("sampleoutput=1")

        extra = (cfg.extra_kv or "").strip()
        if extra:
            args.append(extra)

        self._suppress_preview = True
        try:
            if self._cmd_text is not None:
                self._cmd_text.delete("1.0", "end")
                self._cmd_text.insert("1.0", " ".join(shlex.quote(a) for a in args))
        finally:
            self._suppress_preview = False

        # result.txt hint (DSO always writes to cwd/result.txt)
        out_dir = cfg.output_dir
        if not cfg.keep_result_txt:
            self._result_hint_var.set("result.txt will NOT be kept (run in temp directory).")
        else:
            if out_dir is None:
                out_dir = self.repo_root
            rp = out_dir / "result.txt"
            if rp.exists():
                self._result_hint_var.set(f"result.txt exists: {rp} (will be overwritten).")
            else:
                self._result_hint_var.set(f"result.txt will be written to: {rp}")

        # log hint
        if cfg.save_log:
            if cfg.keep_result_txt:
                log_dir = cfg.output_dir if cfg.output_dir is not None else self.repo_root
                self._log_hint_var.set(f"log will be written to: {log_dir / 'dso_run.log'}")
            else:
                self._log_hint_var.set("log will be written to a temp directory (see [cwd] after Run).")
        else:
            self._log_hint_var.set("log file is disabled.")

    def _command_from_preview(self, preview: str, tmp_images: Path) -> List[str]:
        tokens = shlex.split(preview.strip())
        if not tokens:
            raise ValueError("Command preview is empty")

        out: List[str] = []
        has_files = False
        for t in tokens:
            if t.startswith("files="):
                out.append(f"files={str(tmp_images)}")
                has_files = True
            elif "<temp>/images" in t:
                out.append(t.replace("<temp>/images", str(tmp_images)))
            else:
                out.append(t)

        if not has_files:
            out.insert(1, f"files={str(tmp_images)}")
        return out

    def _run(self) -> None:
        if self.runner.is_running():
            messagebox.showinfo("DSO Runner", "Process is still running. Please Stop it first.")
            return

        cfg = self._collect_config()
        err = self._validate(cfg)
        if err:
            messagebox.showerror("Invalid config", err)
            return

        exe = self.repo_root / "build" / "bin" / "dso_dataset"
        if not exe.is_file():
            messagebox.showerror("Missing executable", f"Not found: {exe}\nBuild dso_dataset under build/ first.")
            return

        if cfg.mode == 0 and (cfg.pcalib_txt is None or cfg.vignette_png is None):
            messagebox.showerror("Invalid config", "mode=0 requires pcalib.txt and vignette.png; otherwise use mode=1/2.")
            return

        if cfg.mode != 0 and (cfg.pcalib_txt is None or cfg.vignette_png is None):
            self._log("[hint] Missing photometric calibration files; consider mode=1 or mode=2.\n\n")

        # prepare temp dataset holder
        try:
            tmp_parent, tmp_images = self._prepare_temp_dataset(cfg)
            self._tmp_parent = tmp_parent
        except Exception as e:
            messagebox.showerror("Failed to prepare temp dataset", str(e))
            return

        run_cwd: Path
        if cfg.keep_result_txt:
            run_cwd = cfg.output_dir if cfg.output_dir is not None else self.repo_root
            run_cwd.mkdir(parents=True, exist_ok=True)
        else:
            # isolate outputs
            run_cwd = Path(tempfile.mkdtemp(prefix="dso_cwd_"))

        # If user manually edited the preview, use it as the command (with files=<temp>/images fixed up).
        if self._preview_dirty:
            try:
                if self._cmd_text is None:
                    raise ValueError("Command preview widget not initialized")
                cmd = self._command_from_preview(self._cmd_text.get("1.0", "end").strip(), tmp_images)
            except Exception as e:
                messagebox.showerror("Invalid command preview", str(e))
                return
        else:
            cmd = _build_command(
                exe=exe,
                files_dir=tmp_images,
                cfg=cfg,
                calib=cfg.camera_txt,
                gamma=cfg.pcalib_txt,
                vignette=cfg.vignette_png,
            )

        self._log(f"[cmd]\n{' '.join(shlex.quote(x) for x in cmd)}\n\n")
        self._log(f"[temp_dataset]\n{self._tmp_parent}\n\n")
        self._log(f"[cwd]\n{run_cwd}\n\n")

        log_path: Optional[Path]
        if cfg.save_log:
            log_path = run_cwd / "dso_run.log"
            self._log(f"[log_file]\n{log_path}\n\n")
        else:
            log_path = None

        try:
            self.runner.start(cmd, cwd=run_cwd, log_path=log_path)
        except Exception as e:
            messagebox.showerror("Failed to start", str(e))

    def _stop(self) -> None:
        self.runner.terminate()

    def _tick(self) -> None:
        for s in self.runner.drain():
            self._log(s)
        self.after(100, self._tick)


def main(argv: Optional[Sequence[str]] = None) -> int:
    _ = argv
    app = App()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

