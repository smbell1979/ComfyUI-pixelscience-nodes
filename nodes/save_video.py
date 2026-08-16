# save_video.py
# ComfyUI node: Save Video (imageio-ffmpeg, audio as input w/ auto SR)
#
# - Inputs: images (IMAGE), optional audio (AUDIO)
# - Containers/codecs configurable (mp4/mkv/webm/mov with h264/h265/vp9/av1/prores/dnxhr)
# - Sequential filenames, optional date subfolder; can also export selected frames only
# - Loops single frame to audio length automatically
# - Uses imageio-ffmpeg (bundled FFmpeg), no system install needed
#
# pip install imageio imageio-ffmpeg

import os
import re
import sys
import math
import wave
import json
import time
import uuid
import tempfile
import subprocess
from pathlib import Path
from typing import List, Optional, Tuple, Union

import numpy as np

try:
    import imageio_ffmpeg  # type: ignore
    _IMAGEIO_FFMPEG_ERROR = None
except Exception as exc:
    imageio_ffmpeg = None  # type: ignore
    _IMAGEIO_FFMPEG_ERROR = exc

try:
    # Honours ComfyUI's --disable-metadata launch flag, like the built-in SaveVideo.
    from comfy.cli_args import args as _comfy_args  # type: ignore
except Exception:
    _comfy_args = None  # type: ignore


VALID_PRESETS = ("ultrafast","superfast","veryfast","faster","fast","medium","slow","slower","veryslow")

VIDEO_CODEC_OPTIONS = {
    "h264": {
        "label": "H.264 (libx264)",
        "ffmpeg": "libx264",
        "pix_fmt": "yuv420p",
        "args": [],
        "supports_crf": True,
        "supports_preset": True,
    },
    "h265": {
        "label": "H.265 / HEVC (libx265)",
        "ffmpeg": "libx265",
        "pix_fmt": "yuv420p",
        "args": [],
        "supports_crf": True,
        "supports_preset": True,
    },
    "vp9": {
        "label": "VP9 (libvpx-vp9)",
        "ffmpeg": "libvpx-vp9",
        "pix_fmt": "yuv420p",
        "args": [["-b:v", "0"]],
        "supports_crf": True,
        "supports_preset": False,
    },
    "av1": {
        "label": "AV1 (libaom-av1)",
        "ffmpeg": "libaom-av1",
        "pix_fmt": "yuv420p",
        "args": [["-b:v", "0"], ["-cpu-used", "6"], ["-row-mt", "1"]],
        "supports_crf": True,
        "supports_preset": False,
    },
    "prores": {
        "label": "ProRes 422 HQ (prores_ks)",
        "ffmpeg": "prores_ks",
        "pix_fmt": "yuv422p10le",
        "args": [["-profile:v", "3"]],
        "supports_crf": False,
        "supports_preset": False,
    },
    "dnxhr": {
        "label": "DNxHR HQ (dnxhr_hq)",
        "ffmpeg": "dnxhr_hq",
        "pix_fmt": "yuv422p10le",
        "args": [],
        "supports_crf": False,
        "supports_preset": False,
    },
}

CONTAINER_OPTIONS = {
    "mp4": {
        "label": "MP4",
        "extension": "mp4",
        "allowed_codecs": {"h264", "h265", "av1"},
        "audio_codec": "aac",
        "extra": [["-movflags", "+faststart"]],
        # ISO-BMFF drops non-standard tags unless the muxer is told to keep them.
        "needs_metadata_tags": True,
    },
    "mkv": {
        "label": "Matroska (MKV)",
        "extension": "mkv",
        "allowed_codecs": set(VIDEO_CODEC_OPTIONS.keys()),
        "audio_codec": "aac",
        "extra": [],
    },
    "webm": {
        "label": "WebM",
        "extension": "webm",
        "allowed_codecs": {"vp9", "av1"},
        "audio_codec": "libopus",
        "extra": [],
    },
    "mov": {
        "label": "QuickTime MOV",
        "extension": "mov",
        "allowed_codecs": {"h264", "h265", "prores", "dnxhr"},
        "audio_codec": "aac",
        "extra": [],
        # MOV shares the mov/mp4 muxer, so it needs the same flag.
        "needs_metadata_tags": True,
    },
}

# ------------------------- helpers -------------------------

def _ffmetadata_escape(text: str) -> str:
    """Escape the characters that are structural in an ffmetadata file."""
    out = text.replace("\\", "\\\\")
    for ch in ("=", ";", "#", "\n"):
        out = out.replace(ch, "\\" + ch)
    return out

def _build_embed_metadata(prompt, extra_pnginfo) -> dict:
    """Collect the tags the ComfyUI frontend looks for when restoring a workflow.

    Mirrors the built-in SaveVideo node: everything in extra_pnginfo (which is
    where 'workflow' lives) plus the raw 'prompt'.
    """
    if _comfy_args is not None and getattr(_comfy_args, "disable_metadata", False):
        return {}

    metadata = {}
    if isinstance(extra_pnginfo, dict):
        metadata.update(extra_pnginfo)
    if prompt is not None:
        metadata["prompt"] = prompt
    return metadata

def _write_ffmetadata_file(metadata: dict, temp_dir: Path) -> Optional[Path]:
    """Serialise tags to an ffmetadata file for '-i'.

    Passing these as '-metadata key=value' would break: a workflow JSON blob
    routinely exceeds the ~32k character limit on a Windows command line.
    """
    if not metadata:
        return None

    lines = [";FFMETADATA1"]
    for key, value in metadata.items():
        if not isinstance(value, str):
            value = json.dumps(value)
        lines.append(f"{_ffmetadata_escape(str(key))}={_ffmetadata_escape(value)}")

    try:
        temp_dir.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            mode="w", suffix=".ffmeta", dir=str(temp_dir),
            encoding="utf-8", delete=False, newline="\n",
        )
        with handle:
            handle.write("\n".join(lines) + "\n")
        return Path(handle.name)
    except Exception as exc:
        print(f"[SaveVideo] Warning: could not write metadata file, workflow will not be embedded: {exc}")
        return None

def _metadata_output_args(container_info: dict, metadata_index: Optional[int]) -> list:
    """Output-side args that pull global tags in from the ffmetadata input."""
    if metadata_index is None:
        return []
    out = ["-map_metadata", str(metadata_index)]
    if container_info.get("needs_metadata_tags"):
        out += ["-movflags", "+use_metadata_tags"]
    return out

class _ConsoleProgress:
    """Single-line, self-overwriting progress meter for the encode loop.

    Each redraw starts with '\\r', which ComfyUI's LogInterceptor treats as
    "replace the previous partial line" (see app/logger.py), so the web log
    panel gets one rolling entry rather than one per frame. When stdout is not
    a terminal (redirected to a file), it falls back to occasional full lines.
    """

    BAR_WIDTH = 16            # keeps the whole line under 80 cols so it cannot wrap
    MIN_INTERVAL = 0.1        # seconds between redraws on a terminal
    MIN_INTERVAL_NOTTY = 5.0  # ...and when piped to a file

    def __init__(self, total: int, label: str = "Encoding", enabled: bool = True):
        self.total = max(1, int(total))
        self.label = label
        self.enabled = enabled
        self.start = time.time()
        self._last_emit = 0.0
        self._last_len = 0
        self._closed = False
        try:
            self._tty = bool(sys.stdout.isatty())
        except Exception:
            self._tty = False

    @staticmethod
    def _clock(seconds) -> str:
        if seconds is None or seconds != seconds or seconds < 0 or seconds == float("inf"):
            return "--:--"
        seconds = int(seconds)
        if seconds >= 3600:
            return f"{seconds // 3600}:{(seconds % 3600) // 60:02d}:{seconds % 60:02d}"
        return f"{seconds // 60:02d}:{seconds % 60:02d}"

    def _emit(self, done: int, final: bool):
        done = max(0, min(int(done), self.total))
        frac = done / self.total
        filled = int(round(frac * self.BAR_WIDTH))
        bar = "#" * filled + "-" * (self.BAR_WIDTH - filled)
        elapsed = time.time() - self.start
        rate = done / elapsed if elapsed > 0 else 0.0
        if final:
            tail = f"in {self._clock(elapsed)}"
        else:
            tail = f"eta {self._clock((self.total - done) / rate if rate > 0 else None)}"
        line = (f"[SaveVideo] {self.label} [{bar}] {frac * 100:5.1f}% "
                f"{done}/{self.total} {rate:.1f}fps {tail}")

        if self._tty:
            pad = " " * max(0, self._last_len - len(line))
            self._last_len = len(line)
            sys.stdout.write("\r" + line + pad + ("\n" if final else ""))
        else:
            sys.stdout.write(line + "\n")
        sys.stdout.flush()

    def update(self, done: int):
        if not self.enabled or self._closed:
            return
        now = time.time()
        interval = self.MIN_INTERVAL if self._tty else self.MIN_INTERVAL_NOTTY
        if done < self.total and (now - self._last_emit) < interval:
            return
        self._last_emit = now
        self._emit(done, final=False)

    def close(self, done: Optional[int] = None):
        """Terminate the line. Safe to call twice, and on the error path."""
        if not self.enabled or self._closed:
            return
        self._closed = True
        self._emit(self.total if done is None else done, final=True)


def _next_seq_number(folder: Path, prefix: str, delim: str, padding: int) -> int:
    pattern = re.compile(rf"^{re.escape(prefix)}{re.escape(delim)}(\d{{{padding}}})\b")
    max_num = 0
    if folder.exists():
        for p in folder.iterdir():
            if not p.is_file():
                continue
            m = pattern.match(p.stem)
            if m:
                try:
                    n = int(m.group(1))
                    if n > max_num:
                        max_num = n
                except ValueError:
                    pass
    return max_num + 1

def _normalize_frames(images) -> List[np.ndarray]:
    try:
        import torch  # type: ignore
    except Exception:
        torch = None  # type: ignore

    data = images

    if isinstance(data, (list, tuple)) and len(data) == 1:
        single = data[0]
        if torch is not None and isinstance(single, torch.Tensor):
            single = single.detach().cpu().numpy()
        if isinstance(single, np.ndarray) and single.ndim == 4:
            data = single

    if torch is not None and isinstance(data, torch.Tensor):
        data = data.detach().cpu().numpy()

    frames_list: List[np.ndarray] = []

    if isinstance(data, np.ndarray):
        if data.ndim == 4:
            frames_list = [data[i] for i in range(data.shape[0])]
        elif data.ndim == 3:
            frames_list = [data]
        else:
            raise ValueError(f"Expected IMAGE as [N,H,W,C] or [H,W,C], got {data.shape}")
    elif isinstance(data, (list, tuple)):
        for item in data:
            if torch is not None and isinstance(item, torch.Tensor):
                item = item.detach().cpu().numpy()
            if isinstance(item, np.ndarray) and item.ndim == 4:
                frames_list.extend([item[i] for i in range(item.shape[0])])
            else:
                frames_list.append(item)
    else:
        frames_list = [data]

    out: List[np.ndarray] = []
    for f in frames_list:
        a = np.asarray(f)
        if a.ndim == 4 and a.shape[0] == 1:
            a = a[0]
        if a.ndim != 3 or a.shape[2] not in (3, 4):
            raise ValueError(f"Expected frame [H,W,3/4], got {a.shape}")
        if a.dtype != np.uint8:
            a = np.clip(a, 0.0, 1.0)
            a = (a * 255.0).round().astype(np.uint8)
        if a.shape[2] == 4:
            a = a[:, :, :3]
        out.append(a)
    return out

def _build_video_only_cmd(ffmpeg_exe: str, w: int, h: int, fps: int,
                          out_path: Path, codec_info: dict, container_info: dict,
                          crf: int, preset: str, metadata_file: Optional[Path] = None) -> list:
    cmd = [
        ffmpeg_exe, "-y", "-hide_banner", "-loglevel", "error",
        "-f", "rawvideo", "-vcodec", "rawvideo",
        "-pix_fmt", "rgb24", "-s", f"{w}x{h}", "-r", str(fps),
        "-i", "-"
    ]
    metadata_index = None
    if metadata_file is not None:
        cmd += ["-f", "ffmetadata", "-i", str(metadata_file)]
        metadata_index = 1  # input 0 is the rawvideo pipe
    vf = "pad=ceil(iw/2)*2:ceil(ih/2)*2"
    cmd += ["-vf", vf, "-c:v", codec_info["ffmpeg"]]
    if codec_info.get("supports_preset"):
        cmd += ["-preset", preset]
    if codec_info.get("supports_crf"):
        cmd += ["-crf", str(crf)]
    pix_fmt = codec_info.get("pix_fmt")
    if pix_fmt:
        cmd += ["-pix_fmt", pix_fmt]
    for extra in codec_info.get("args", []):
        cmd += extra
    cmd += ["-an"]
    for extra in container_info.get("extra", []):
        cmd += extra
    cmd += _metadata_output_args(container_info, metadata_index)
    cmd += [str(out_path)]
    return cmd

def _extract_frames_from_file(video_path: Path) -> List[np.ndarray]:
    path_str = str(video_path)
    frames: List[np.ndarray] = []
    try:
        import cv2
        cap = cv2.VideoCapture(path_str)
        if cap.isOpened():
            while True:
                ret, frame = cap.read()
                if not ret:
                    break
                frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                frames.append(frame)
            cap.release()
            if frames:
                return frames
    except Exception:
        pass

    try:
        import imageio
        reader = imageio.get_reader(path_str)
        for frame in reader:
            if frame.ndim == 3 and frame.shape[2] in (3, 4):
                if frame.shape[2] == 4:
                    frame = frame[:, :, :3]
                frames.append(frame)
        reader.close()
        if frames:
            return frames
    except Exception:
        pass

    if imageio_ffmpeg is not None:
        try:
            reader = imageio_ffmpeg.read_frames(path_str)
            meta = next(reader)
            w, h = meta["size"]
            for frame_bytes in reader:
                frame = np.frombuffer(frame_bytes, dtype=np.uint8).reshape((h, w, 3))
                frames.append(frame.copy())
            if frames:
                return frames
        except Exception:
            pass

    return frames

def _extract_audio_from_file(video_path: Path, ffmpeg_exe: str) -> Tuple[Optional[bytes], int, int]:
    sample_rate = 44100
    channels = 2
    try:
        cmd = [
            ffmpeg_exe, "-y", "-i", str(video_path),
            "-vn", "-acodec", "pcm_f32le", "-ar", str(sample_rate), "-ac", str(channels),
            "-f", "f32le", "-"
        ]
        res = subprocess.run(cmd, capture_output=True, check=False)
        if res.returncode == 0 and res.stdout and len(res.stdout) > 0:
            return res.stdout, sample_rate, channels
    except Exception:
        pass
    return None, sample_rate, channels

def _resolve_video_file_path(video_item) -> Optional[Path]:
    if isinstance(video_item, (str, Path)):
        p = Path(str(video_item)).expanduser()
        if p.exists() and p.is_file():
            return p
        try:
            import folder_paths
            for get_dir in (folder_paths.get_input_directory, folder_paths.get_output_directory, folder_paths.get_temp_directory):
                try:
                    cand = Path(get_dir()) / p
                    if cand.exists() and cand.is_file():
                        return cand
                except Exception:
                    pass
        except Exception:
            pass

    elif isinstance(video_item, dict):
        for key in ("path", "filename", "video", "video_path", "file"):
            if key in video_item and isinstance(video_item[key], (str, Path)):
                p = _resolve_video_file_path(video_item[key])
                if p:
                    return p
        if "filename" in video_item:
            fn = video_item["filename"]
            sub = video_item.get("subfolder", "")
            tp = video_item.get("type", "output")
            try:
                import folder_paths
                if tp == "input":
                    base = Path(folder_paths.get_input_directory())
                elif tp == "temp":
                    base = Path(folder_paths.get_temp_directory())
                else:
                    base = Path(folder_paths.get_output_directory())
                cand = base / sub / fn
                if cand.exists() and cand.is_file():
                    return cand
            except Exception:
                pass

    elif isinstance(video_item, (tuple, list)):
        if len(video_item) > 0 and isinstance(video_item[0], (str, Path, dict)):
            return _resolve_video_file_path(video_item[0])

    elif hasattr(video_item, "get_stream_source") and callable(getattr(video_item, "get_stream_source")):
        try:
            src = video_item.get_stream_source()
            if src and isinstance(src, (str, Path)):
                return _resolve_video_file_path(src)
        except Exception:
            pass

    return None

def _extract_frames_and_audio_from_video_input(video, ffmpeg_exe: str) -> Tuple[List[np.ndarray], Optional[bytes], int, int]:
    # 1. ComfyUI VideoFromComponents / VideoFromFile / VideoInput objects with get_components()
    if hasattr(video, "get_components") and callable(getattr(video, "get_components")):
        try:
            comp = video.get_components()
            c_images = getattr(comp, "images", None)
            c_audio = getattr(comp, "audio", None)

            frames = None
            if c_images is not None:
                frames = _normalize_frames(c_images)

            audio_bytes = None
            sr = 44100
            ch = 2

            if c_audio is not None:
                a_obj = c_audio
                if hasattr(a_obj, "get_components") and callable(getattr(a_obj, "get_components")):
                    a_obj = a_obj.get_components()

                wf_data = None
                if isinstance(a_obj, dict) and "waveform" in a_obj:
                    wf_data = a_obj["waveform"]
                    sr = int(a_obj.get("sample_rate", 44100))
                elif hasattr(a_obj, "waveform"):
                    wf_data = getattr(a_obj, "waveform")
                    sr = int(getattr(a_obj, "sample_rate", 44100))

                if wf_data is not None:
                    import torch
                    if isinstance(wf_data, torch.Tensor):
                        wf = wf_data.detach().cpu().to(torch.float32)
                        if wf.ndim == 3:
                            wf = wf.squeeze(0)
                        ch = int(wf.shape[0])
                        audio_bytes = wf.transpose(0, 1).numpy().tobytes()
                    elif isinstance(wf_data, np.ndarray):
                        a = np.squeeze(wf_data)
                        if a.ndim == 2 and a.shape[0] <= 8:
                            a = a.T
                        ch = int(a.shape[1])
                        audio_bytes = a.astype(np.float32).tobytes()

            if frames:
                return frames, audio_bytes, sr, ch
        except Exception as exc:
            print(f"[SaveVideo] Warning: Exception parsing get_components() from video input: {exc}")

    # 2. File path resolution (str, Path, dict containing path/filename, tuple, stream source)
    video_path = _resolve_video_file_path(video)
    if video_path:
        frames = _extract_frames_from_file(video_path)
        audio_bytes, sr, ch = _extract_audio_from_file(video_path, ffmpeg_exe)
        return frames, audio_bytes, sr, ch

    # 3. Dictionary containing frames and optional audio
    if isinstance(video, dict):
        frames = None
        for key in ("frames", "images", "image"):
            if key in video:
                try:
                    frames = _normalize_frames(video[key])
                    break
                except Exception:
                    pass
        audio_bytes = None
        sr = 44100
        ch = 2
        if "audio" in video:
            a_obj = video["audio"]
            if isinstance(a_obj, dict) and "waveform" in a_obj:
                try:
                    wf_data = a_obj["waveform"]
                    sr = int(a_obj.get("sample_rate", 44100))
                    import torch
                    if isinstance(wf_data, torch.Tensor):
                        wf = wf_data.detach().cpu().to(torch.float32)
                        if wf.ndim == 3:
                            wf = wf.squeeze(0)
                        ch = int(wf.shape[0])
                        audio_bytes = wf.transpose(0, 1).numpy().tobytes()
                except Exception:
                    pass
        if frames:
            return frames, audio_bytes, sr, ch

    # 4. Generic Python object attributes (.images, .frames, .video, .path)
    if not isinstance(video, (str, Path, dict, tuple, list, np.ndarray)):
        for attr in ("path", "filename", "file", "video_path"):
            val = getattr(video, attr, None)
            if val is not None:
                p = _resolve_video_file_path(val)
                if p:
                    frames = _extract_frames_from_file(p)
                    audio_bytes, sr, ch = _extract_audio_from_file(p, ffmpeg_exe)
                    if frames:
                        return frames, audio_bytes, sr, ch

        for attr in ("images", "frames", "video", "components"):
            val = getattr(video, attr, None)
            if val is not None:
                try:
                    frames = _normalize_frames(val)
                    if frames:
                        return frames, None, 44100, 2
                except Exception:
                    pass

    # 5. Direct tensor, numpy array, or list of frame tensors
    try:
        frames = _normalize_frames(video)
        return frames, None, 44100, 2
    except Exception as exc:
        type_name = type(video).__name__
        raise ValueError(f"Unsupported video input format: {type_name}") from exc


# --------------------------- node ---------------------------

class SaveVideo:
    """
    Save Video (simple) — minimal kontroller, audio og video som direkte inputs.
    """
    DESCRIPTION = (
    "Saves to ComfyUI/output by default. To allow external locations, create a file named "
	" pixelscience_save_allowed_paths.json containing for example: { \"allowed_roots\": [\"D:/ImageExports\", \"E:/TeamShare/Images\"] }. "
	"Place it in <ComfyUI>/user/config/. Read the Github repository for more info. I have placed the settings number_paddings, number_start, "
    "loop_still_to_audio, and show_progress in properties (right click) and ComfyUI settings to keep the node compact. "
    "file_path and filename_prefix accept [date_subfolder], which expands to the same rendered value as date_subfolder_pattern."
    )

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "file_path": ("STRING", {"default": "output/video", "tooltip": "Folder where the video is saved. Supports [date_subfolder] to reuse the rendered date_subfolder_pattern."}),
                "date_subfolder_pattern": ("STRING", {"default": "%Y-%m-%d", "tooltip": "Optional strftime pattern or placeholders for subfolders. Reuse the result elsewhere with the [date_subfolder] token."}),
                "filename_prefix": ("STRING", {"default": "VID", "tooltip": "Filename prefix, e.g. VID_0001.mp4. Supports [date_subfolder] to reuse the rendered date_subfolder_pattern."}),
                "filename_delimiter": ("STRING", {"default": "_", "tooltip": "Delimiter between prefix and sequence number."}),
                "preview_only": (["off", "on"], {"default": "off", "tooltip": "Skip saving video file to disk while keeping player preview functionality."}),
                "number_padding": ("INT", {"default": 4, "min": 1, "max": 10, "tooltip": "Digits in the sequence number (0001, 0002, ...).", "display": "property"}),
                "number_start": ("INT", {"default": 1, "min": 0, "max": 1_000_000, "tooltip": "Starting value for the sequence number.", "display": "property"}),
                "container": (tuple(CONTAINER_OPTIONS.keys()), {"default": "mp4", "tooltip": "Container format (mp4, mkv, webm, mov)."}),
                "video_codec": (tuple(VIDEO_CODEC_OPTIONS.keys()), {"default": "h264", "tooltip": "Video codec to use for encoding."}),
                "fps": ("FLOAT", {"default": 24.0, "min": 1.0, "max": 240.0, "step": 1.0, "tooltip": "Frames per second (CFR)."}),
                "crf": ("INT", {"default": 23, "min": 0, "max": 51, "tooltip": "Quality (lower = better, larger files). Typical 18-28 for H.264."}),
                "preset": (VALID_PRESETS, {"default": "fast", "tooltip": "Encoder speed versus quality (ultrafast ... veryslow)."}),
            },
            "optional": {
                "images": ("IMAGE", {"tooltip": "Optional frame input. Batch data supported."}),
                "audio": ("AUDIO", {"tooltip": "Optional audio track. Mono/stereo supported."}),
                "video": ("VIDEO", {"tooltip": "Optional video input (video file path, video tensor batch, video dictionary, or VHS_VIDEO)."}),
                "loop_still_to_audio": ("BOOLEAN", {"default": True, "tooltip": "If only one frame plus audio, loop the frame to match audio length.", "display": "property"}),
                "show_progress": ("BOOLEAN", {"default": True, "tooltip": "Write progress information to the console.", "display": "property"}),
                "embed_workflow": ("BOOLEAN", {"default": True, "tooltip": "Embed the workflow and prompt in the video's metadata, so the file can be dragged back into ComfyUI to restore the graph. Reliable in mp4/mov/mkv/webm."}),
            },
            "hidden": {
                "prompt": "PROMPT",
                "extra_pnginfo": "EXTRA_PNGINFO",
            },
        }

    RETURN_TYPES = ("IMAGE", "STRING",)
    RETURN_NAMES = ("images", "video_path",)
    FUNCTION = "save"
    CATEGORY = "pixelscience/💾 IO"
    OUTPUT_NODE = True

    # ----------------------- path helpers -----------------------

    def _normalize_path(self, path: Path) -> Path:
        return Path(os.path.abspath(str(path)))

    def _base_output_dir(self) -> Path:
        try:
            from folder_paths import get_output_directory  # type: ignore
            base = Path(get_output_directory()).expanduser()
        except Exception:
            base = Path.cwd() / "output"
        return self._normalize_path(base)

    def _comfy_root(self) -> Path:
        base = self._base_output_dir()
        return self._normalize_path(base.parent)

    def _load_allowed_roots(self) -> List[Path]:
        env_cfg = os.environ.get("PIXELSCIENCE_SAVE_ALLOWED_PATHS")
        candidates: List[str] = []
        if env_cfg:
            candidates.append(env_cfg)

        comfy_root = self._comfy_root()
        names = ("pixelscience_save_allowed_paths.json", "allowed_paths.json")
        for name in names:
            candidates.append(str(comfy_root / "user" / "config" / name))
            candidates.append(str(comfy_root / "user" / name))
            candidates.append(str(comfy_root / "config" / name))
            candidates.append(str(comfy_root / name))

        here = Path(__file__).resolve().parent
        for name in names:
            candidates.append(str(here / name))

        seen = set()
        for path_str in candidates:
            if not path_str:
                continue
            candidate = Path(os.path.expandvars(path_str)).expanduser()
            key = str(candidate)
            if key in seen:
                continue
            seen.add(key)
            if candidate.is_file():
                try:
                    with open(candidate, "r", encoding="utf-8") as fh:
                        raw = fh.read()
                except Exception:
                    continue
                filtered = "\n".join(
                    line for line in raw.splitlines() if not line.lstrip().startswith("#")
                )
                try:
                    data = json.loads(filtered)
                except Exception:
                    continue
                entries = []
                if isinstance(data, dict):
                    entries = data.get("allowed_roots") or data.get("roots") or []
                elif isinstance(data, list):
                    entries = data
                roots: List[Path] = []
                for entry in entries:
                    if isinstance(entry, str):
                        roots.append(self._normalize_path(Path(os.path.expandvars(entry)).expanduser()))
                if roots:
                    return roots
        return []

    def _same_drive(self, a: Path, b: Path) -> bool:
        da = os.path.splitdrive(str(self._normalize_path(a)))[0].lower()
        db = os.path.splitdrive(str(self._normalize_path(b)))[0].lower()
        return da == db

    def _is_under_dir(self, path: Path, base: Path) -> bool:
        try:
            ap = self._normalize_path(path)
            bp = self._normalize_path(base)
            if not self._same_drive(ap, bp):
                return False
            return os.path.commonpath([str(ap), str(bp)]) == str(bp)
        except Exception:
            return False

    def _validate_path_is_allowed(self, target_path: Path) -> None:
        target_abs = self._normalize_path(target_path)
        base_output = self._base_output_dir()
        if self._is_under_dir(target_abs, base_output):
            return

        for root in self._load_allowed_roots():
            if self._is_under_dir(target_abs, root):
                return

        msg = (
            "External save path is not allowed.\n"
            "This node only writes inside ComfyUI's output directory, "
            "unless the path is whitelisted offline.\n\n"
            "To allow external locations, create/edit a JSON file named "
            "'pixelscience_save_allowed_paths.json' in your ComfyUI root (or user/config) folder "
            "with content like:\n\n"
            '{\n  "allowed_roots": ["D:/VideoExports", "E:/TeamShare/Video"]\n}\n\n'
            "You can also set the PIXELSCIENCE_SAVE_ALLOWED_PATHS environment variable to point to this file."
        )
        raise PermissionError(msg)

    def _build_template_context(self) -> dict:
        return {
            "unix": str(int(time.time())),
            "guid": uuid.uuid4().hex,
            "uuid": uuid.uuid4().hex,
            "model": "unknown",
            # Filled in by save() once date_subfolder_pattern has been rendered,
            # so [date_subfolder] resolves to the exact same string as the folder.
            "date_subfolder": "",
        }

    def _expand_path_templates(self, text: str, context: dict | None = None) -> str:
        if not isinstance(text, str):
            return text

        ctx = context or {}

        def repl_time(match):
            fmt = match.group(1)
            try:
                return time.strftime(fmt)
            except Exception:
                return time.strftime("%Y%m%d_%H%M%S")

        out = re.sub(r"[[]time\[(.*?)\]\]", repl_time, text)
        out = out.replace("[date]", time.strftime("%Y-%m-%d"))
        out = out.replace("[datetime]", time.strftime("%Y-%m-%d_%H-%M-%S"))
        out = out.replace("[unix]", ctx.get("unix", str(int(time.time()))))
        out = out.replace("[guid]", ctx.get("guid", uuid.uuid4().hex))
        out = out.replace("[uuid]", ctx.get("uuid", uuid.uuid4().hex))
        out = out.replace("[model]", ctx.get("model", "unknown"))
        # Resolves to the rendered date_subfolder_pattern. Empty while that
        # pattern is itself being rendered, which makes self-reference a no-op.
        out = out.replace("[date_subfolder]", ctx.get("date_subfolder", ""))

        def repl_env(match):
            name = match.group(1) or ""
            return os.environ.get(name, "")

        out = re.sub(r"[[]env\[(.*?)\]\]", repl_env, out)
        return out

    def _render_date_subfolder(self, pattern: str, context: dict | None = None) -> str:
        expanded = self._expand_path_templates(pattern or "", context).strip()
        if not expanded:
            return ""
        try:
            return time.strftime(expanded)
        except Exception:
            return expanded

    def save(
        self,
        file_path,
        date_subfolder_pattern,
        filename_prefix,
        filename_delimiter,
        preview_only="off",
        number_padding=4,
        number_start=1,
        container="mp4",
        video_codec="h264",
        fps=24.0,
        crf=23,
        preset="fast",
        images=None,
        audio=None,
        video=None,
        loop_still_to_audio=True,
        show_progress=True,
        embed_workflow=True,
        prompt=None,
        extra_pnginfo=None,
    ):
        fps = int(round(float(fps)))
        
        if imageio_ffmpeg is None:
            msg = (
                "Save Video node requires 'imageio' and 'imageio-ffmpeg'. "
                "Install with: pip install imageio imageio-ffmpeg."
            )
            if _IMAGEIO_FFMPEG_ERROR:
                msg += f" Import error: {_IMAGEIO_FFMPEG_ERROR}"
            raise RuntimeError(msg)

        ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
        is_preview_only = (str(preview_only).lower() == "on")

        # --- Get Frames & Video/Audio Extraction ---
        frames: List[np.ndarray] = []
        extracted_audio_bytes = None
        extracted_sr = 44100
        extracted_ch = 2

        if video is not None:
            frames, extracted_audio_bytes, extracted_sr, extracted_ch = (
                _extract_frames_and_audio_from_video_input(video, ffmpeg_exe)
            )
        elif images is not None:
            frames = _normalize_frames(images)
        else:
            raise ValueError("Save Video requires either 'images' or 'video' input.")

        if not frames:
            raise ValueError("No frames provided or extracted from input.")

        container_key = str(container).lower()
        codec_key = str(video_codec).lower()
        container_info = CONTAINER_OPTIONS.get(container_key)
        codec_info = VIDEO_CODEC_OPTIONS.get(codec_key)

        if container_info is None: raise ValueError(f"Unsupported container '{container}'.")
        if codec_info is None: raise ValueError(f"Unsupported video codec '{video_codec}'.")
        if codec_key not in container_info["allowed_codecs"]:
            allowed = ", ".join(sorted(container_info["allowed_codecs"]))
            raise ValueError(f"Codec '{codec_key}' is not supported in '{container_key}'. Allowed: {allowed}.")

        extension = container_info.get("extension", "mp4")

        try:
            from folder_paths import get_temp_directory  # type: ignore
            temp_dir = Path(get_temp_directory())
        except Exception:
            temp_dir = Path(tempfile.gettempdir())

        preview_filename = f"dh_savevideo_preview.{extension}"
        temp_preview = temp_dir / preview_filename

        if is_preview_only:
            out_path = self._normalize_path(temp_preview)
        else:
            # --- Path Setup & Validation ---
            context = self._build_template_context()
            # Render the date subfolder first so [date_subfolder] can reuse the
            # exact same string in file_path and filename_prefix.
            subfolder = self._render_date_subfolder(date_subfolder_pattern, context)
            context["date_subfolder"] = subfolder
            expanded_file_path = self._expand_path_templates(file_path, context)
            expanded_prefix = self._expand_path_templates(filename_prefix, context)

            user_path = Path(str(expanded_file_path or "")).expanduser()
            if user_path.is_absolute():
                base_dir = user_path
            else:
                base_output = self._base_output_dir()
                rel_parts = [p for p in user_path.parts if p and p != "."]
                if rel_parts and rel_parts[0].lower() in ("output", "outputs"):
                    rel_parts = rel_parts[1:]
                rel_path = Path(*rel_parts) if rel_parts else Path()
                base_dir = base_output / rel_path

            if subfolder:
                base_dir = base_dir / Path(subfolder)

            prefix_dir_part = os.path.dirname(expanded_prefix)
            if prefix_dir_part:
                base_dir = base_dir / Path(prefix_dir_part)

            final_video_dir = self._normalize_path(base_dir)
            final_video_dir.mkdir(parents=True, exist_ok=True)

            base_prefix = os.path.basename(expanded_prefix)

            # --- Sequence Numbering ---
            seq = _next_seq_number(final_video_dir, base_prefix, filename_delimiter, number_padding)
            if number_start > 0:
                seq = max(seq, number_start)
            stem = f"{base_prefix}{filename_delimiter}{seq:0{number_padding}d}"

            out_path = self._normalize_path(final_video_dir / f"{stem}.{extension}")
            self._validate_path_is_allowed(out_path)

        # --- Audio Extraction (VHS method) ---
        audio_bytes = None
        sample_rate = 44100
        channels = 2
        acodec = container_info.get("audio_codec")

        if audio is not None and acodec:
            try:
                if isinstance(audio, dict) and "waveform" in audio:
                    wf_data = audio["waveform"]
                    sr_data = audio.get("sample_rate", 44100)
                    if hasattr(sr_data, "item"):
                        sample_rate = int(sr_data.item())
                    else:
                        sample_rate = int(sr_data)

                    import torch
                    if isinstance(wf_data, torch.Tensor):
                        wf = wf_data.detach().cpu().to(torch.float32)
                        if wf.ndim == 3:
                            wf = wf.squeeze(0)  # [channels, samples]
                        channels = int(wf.shape[0])
                        audio_bytes = wf.transpose(0, 1).numpy().tobytes()
                    elif isinstance(wf_data, np.ndarray):
                        a = np.squeeze(wf_data)
                        if a.ndim == 2 and a.shape[0] <= 8:
                            a = a.T
                        channels = int(a.shape[1])
                        audio_bytes = a.astype(np.float32).tobytes()
                else:
                    from .save_audio_mp3 import _normalize_audio_input
                    pcm, sr = _normalize_audio_input(audio)
                    sample_rate = int(sr)
                    channels = int(pcm.shape[1])
                    # pcm is int16 -> convert to float32 bytes for VHS f32le pipe
                    a_float = pcm.astype(np.float32) / 32767.0
                    audio_bytes = a_float.tobytes()

                if show_progress and audio_bytes:
                    dur_s = (len(audio_bytes) / (4 * channels)) / sample_rate if sample_rate else 0
                    print(f"[SaveVideo] Audio extracted: {channels}ch @ {sample_rate}Hz (~{dur_s:.2f}s, {len(audio_bytes)} bytes)")
            except Exception as exc:
                print(f"[SaveVideo] ERROR extracting audio: {exc}")
                import traceback
                traceback.print_exc()

        elif extracted_audio_bytes is not None and acodec:
            audio_bytes = extracted_audio_bytes
            sample_rate = extracted_sr
            channels = extracted_ch
            if show_progress and audio_bytes:
                dur_s = (len(audio_bytes) / (4 * channels)) / sample_rate if sample_rate else 0
                print(f"[SaveVideo] Using audio extracted from video input: {channels}ch @ {sample_rate}Hz (~{dur_s:.2f}s, {len(audio_bytes)} bytes)")

        total_frames = len(frames)
        ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()

        if len(frames) == 1 and audio_bytes is not None and loop_still_to_audio:
            dur = (len(audio_bytes) / (4 * channels)) / sample_rate if sample_rate else 0
            if dur > 0:
                total_frames = int(math.ceil(dur * fps))
                if show_progress: print(f"[SaveVideo] Looping single frame for {dur:.2f}s -> {total_frames} frames @ {fps} fps")

        h, w, _ = frames[0].shape

        # --- Workflow / prompt metadata ---
        metadata_file = None
        if embed_workflow:
            embed_metadata = _build_embed_metadata(prompt, extra_pnginfo)
            metadata_file = _write_ffmetadata_file(embed_metadata, temp_dir)
            if show_progress:
                if metadata_file is not None:
                    print(f"[SaveVideo] Embedding metadata tags: {', '.join(sorted(embed_metadata.keys()))}")
                else:
                    print("[SaveVideo] No workflow metadata available to embed.")

        def _cleanup_metadata_file():
            if metadata_file is not None:
                try: os.unlink(metadata_file)
                except Exception: pass

        # Pass 1: Encode Video Only (VHS approach)
        if audio_bytes is not None:
            tmp_video = tempfile.NamedTemporaryFile(
                delete=False, suffix=f".{container_info.get('extension', 'mp4')}")
            tmp_video.close()
            video_target = Path(tmp_video.name)
        else:
            video_target = out_path

        # Only tag here when pass 1 writes the final file; otherwise pass 2 does it.
        cmd1 = _build_video_only_cmd(ffmpeg_exe, w, h, fps, video_target,
                                     codec_info, container_info, crf, preset,
                                     metadata_file=None if audio_bytes is not None else metadata_file)
        if show_progress:
            print(f"[SaveVideo] Output: {out_path}")
            print(f"[SaveVideo] Pass 1 (video): {' '.join(cmd1)}")

        proc = subprocess.Popen(cmd1, stdin=subprocess.PIPE, stderr=subprocess.PIPE, text=False)
        looping_still = len(frames) == 1 and total_frames > 1
        progress = _ConsoleProgress(
            total_frames if looping_still else len(frames),
            label="Looping" if looping_still else "Encoding",
            enabled=bool(show_progress),
        )
        written = 0
        try:
            if looping_still:
                buf = frames[0].tobytes()
                for _ in range(total_frames):
                    proc.stdin.write(buf)
                    written += 1
                    progress.update(written)
            else:
                for f in frames:
                    proc.stdin.write(f.tobytes())
                    written += 1
                    progress.update(written)
        finally:
            # Terminate the line before anything else prints, including on error.
            progress.close(written)
            if proc.stdin: proc.stdin.close()
            stderr1 = proc.stderr.read() if proc.stderr else b""
            if proc.stderr: proc.stderr.close()
            ret1 = proc.wait()

        if ret1 != 0 or not video_target.exists():
            stderr_text = stderr1.decode("utf-8", errors="ignore")
            if audio_bytes is not None:
                try: os.unlink(video_target)
                except Exception: pass
            _cleanup_metadata_file()
            raise RuntimeError(f"FFmpeg Pass 1 failed (code {ret1}).\nCmd: {' '.join(cmd1)}\nStderr:\n{stderr_text.strip()}")

        # Pass 2: Mux Audio (VHS approach)
        if audio_bytes is not None:
            min_audio_dur = total_frames / fps + 1
            cmd2 = [
                ffmpeg_exe, "-y", "-hide_banner", "-loglevel", "error",
                "-i", str(video_target),
                "-ar", str(sample_rate),
                "-ac", str(channels),
                "-f", "f32le",
                "-i", "-",
            ]
            metadata_index2 = None
            if metadata_file is not None:
                cmd2 += ["-f", "ffmetadata", "-i", str(metadata_file)]
                metadata_index2 = 2  # input 0 is the video, input 1 the audio pipe
            cmd2 += [
                "-c:v", "copy",
                "-c:a", acodec,
                "-af", f"apad=whole_dur={min_audio_dur:.3f}",
                "-shortest",
            ]
            cmd2 += _metadata_output_args(container_info, metadata_index2)
            cmd2 += [str(out_path)]
            if show_progress:
                print(f"[SaveVideo] Pass 2 (audio mux): {' '.join(cmd2)}")

            try:
                res = subprocess.run(cmd2, input=audio_bytes, capture_output=True, check=True)
                if show_progress and res.stderr:
                    print(f"[SaveVideo] Pass 2 stderr: {res.stderr.decode('utf-8', errors='ignore').strip()}")
            except subprocess.CalledProcessError as exc:
                err = exc.stderr.decode("utf-8", errors="ignore")
                raise RuntimeError(f"FFmpeg Pass 2 (audio mux) failed.\nCmd: {' '.join(cmd2)}\nStderr:\n{err.strip()}")
            finally:
                try: os.unlink(video_target)
                except Exception: pass
                _cleanup_metadata_file()

        _cleanup_metadata_file()

        out_exists = out_path.exists() and out_path.stat().st_size > 0
        if not out_exists:
            raise RuntimeError(f"Output file missing or empty: {out_path}")

        if is_preview_only:
            abs_path = "[Preview Only]"
            if show_progress: print(f"[SaveVideo] Preview generated (Preview Only mode, not saved to disk)")
        else:
            video_path_str = str(out_path.resolve())
            if show_progress: print(f"[SaveVideo] Done: {video_path_str} ({out_path.stat().st_size} bytes)")
            abs_path = video_path_str
            import shutil
            shutil.copy2(str(out_path), str(temp_preview))

        ui = {
            "text": abs_path,
            "video_preview": [{
                "filename": preview_filename,
                "subfolder": "",
                "type": "temp",
                "format": f"video/{extension}",
            }],
        }

        out_images = images
        if out_images is None and frames:
            try:
                import torch
                arr = np.stack(frames).astype(np.float32) / 255.0
                out_images = torch.from_numpy(arr)
            except Exception:
                out_images = None

        return {"ui": ui, "result": (out_images, abs_path,)}
