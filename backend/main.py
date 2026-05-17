"""
Pseudo-3D from Video — FastAPI Backend
Нарезка видео на кадры через FFmpeg, аннотации, экспорт ZIP.
Центрирование объекта (OpenCV tracker + warpAffine).
"""

import os
import json
import uuid
import subprocess
import zipfile
import shutil
import hashlib
from pathlib import Path
from datetime import datetime
from typing import Optional

try:
    import cv2
    import numpy as np
    _HAS_CV2 = True
except ImportError:
    cv2 = None  # type: ignore
    np = None   # type: ignore
    _HAS_CV2 = False

try:
    from PIL import Image, ImageDraw, ImageFont
    _HAS_PIL = True
except ImportError:
    _HAS_PIL = False


# ── TrueType font with Cyrillic support (cached) ────────────────────────

# Project root: one level up from backend/
_PROJECT_ROOT = Path(__file__).resolve().parent.parent

def _find_truetype_font(size: int) -> "ImageFont.FreeTypeFont | None":
    """
    Find a TrueType font that supports Cyrillic.
    Search order: bundled font in assets/fonts → Windows → Linux → macOS → Pillow default.
    """
    if not _HAS_PIL:
        return None

    # Bundled font in project assets directory
    _bundled = _PROJECT_ROOT / "assets" / "fonts" / "DejaVuSans.ttf"

    candidates = [
        str(_bundled),
        # Also check backend/fonts/ for backward compat
        str(Path(__file__).resolve().parent / "fonts" / "DejaVuSans.ttf"),
        # Windows
        r"C:\Windows\Fonts\arial.ttf",
        r"C:\Windows\Fonts\segoeui.ttf",
        r"C:\Windows\Fonts\tahoma.ttf",
        # Linux
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
        # macOS
        "/System/Library/Fonts/Helvetica.ttc",
        "/System/Library/Fonts/SFNSText.ttf",
        "/Library/Fonts/Arial.ttf",
    ]
    for path in candidates:
        try:
            if os.path.isfile(path):
                return ImageFont.truetype(path, size)
        except Exception:
            continue

    # Last resort: Pillow default (may not support Cyrillic, but better than nothing)
    try:
        return ImageFont.load_default()
    except Exception:
        return None


# Font cache: size → font object
_font_cache: dict[int, "ImageFont.FreeTypeFont | None"] = {}


def _get_font(size: int) -> "ImageFont.FreeTypeFont | None":
    if size not in _font_cache:
        _font_cache[size] = _find_truetype_font(size)
    # #region agent log
    import json as _json_dbg; open(r"d:\projects\3Dps\.cursor\debug.log", "a", encoding="utf-8").write(_json_dbg.dumps({"location":"main.py:_get_font","message":"Font lookup result","data":{"size":size,"result_is_none":_font_cache[size] is None,"result_type":type(_font_cache[size]).__name__},"hypothesisId":"B","timestamp":__import__('time').time()},ensure_ascii=False)+"\n")
    # #endregion
    return _font_cache[size]

from fastapi import (
    FastAPI, UploadFile, File, Form,
    HTTPException, BackgroundTasks, Body, Request,
)
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
import uvicorn

# ── Paths ────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent.parent
PROJECTS_DIR = BASE_DIR / "projects"
FRONTEND_DIR = BASE_DIR / "frontend"
PROJECTS_DIR.mkdir(exist_ok=True)


app = FastAPI(title="Pseudo-3D from Video")

# ── In-memory progress / cancellation store (single-process) ────────────
generation_progress: dict = {}
centering_progress: dict = {}
# per-project cancellation flag for frame generation
generation_cancel_flags: dict[str, bool] = {}


# ── Project metadata helpers ────────────────────────────────────────────

def _metadata_path(pdir: Path) -> Path:
    return pdir / "metadata.json"


def _compute_frames_count(pdir: Path) -> int:
    """Best-effort frames_count from index.json or frames directory."""
    index_fp = pdir / "index.json"
    if index_fp.exists():
        try:
            idx = json.loads(index_fp.read_text(encoding="utf-8"))
            if "num_frames" in idx:
                return int(idx["num_frames"])
            if "frames" in idx and isinstance(idx["frames"], list):
                return len(idx["frames"])
        except Exception:
            pass

    frames_dir = pdir / "frames"
    if frames_dir.exists():
        try:
            return sum(1 for f in frames_dir.glob("frame_*.jpg"))
        except Exception:
            pass
    return 0


def ensure_metadata(pdir: Path) -> dict:
    """
    Load project metadata, creating metadata.json from legacy project.json
    if needed. Always returns a dict with at least required keys.
    """
    mpath = _metadata_path(pdir)
    if mpath.exists():
        try:
            meta = json.loads(mpath.read_text(encoding="utf-8"))
            changed = False
            # guarantee project_name for older metadata files
            if "project_name" not in meta:
                original = meta.get("original_filename") or ""
                meta["project_name"] = original
                changed = True
            # guarantee frame generation status fields
            if "frames_status" not in meta:
                meta["frames_status"] = "not_started"
                changed = True
            if "frames_expected" not in meta:
                meta["frames_expected"] = 0
                changed = True
            if "frames_generated" not in meta:
                meta["frames_generated"] = int(meta.get("frames_count") or 0)
                changed = True
            if "frames_settings_hash" not in meta:
                meta["frames_settings_hash"] = None
                changed = True
            if "frames_generated_at" not in meta:
                meta["frames_generated_at"] = None
                changed = True
            if changed:
                mpath.write_text(json.dumps(meta, indent=2), encoding="utf-8")
            return meta
        except Exception:
            # fall back to reconstruction below
            pass

    pinfo_fp = pdir / "project.json"
    if not pinfo_fp.exists():
        raise FileNotFoundError(f"project.json not found in {pdir}")

    info = json.loads(pinfo_fp.read_text(encoding="utf-8"))
    frames_count = _compute_frames_count(pdir)

    resolution = None
    w = info.get("width")
    h = info.get("height")
    if isinstance(w, (int, float)) and isinstance(h, (int, float)):
        resolution = f"{int(w)}x{int(h)}"

    fps_val = info.get("fps")
    if fps_val is None:
        # Try to recover fps from original video using ffprobe
        try:
            video_name = info.get("video_file")
            if video_name:
                vpath = pdir / video_name
                if vpath.exists():
                    fps_val = round(video_fps(str(vpath)), 3)
        except Exception:
            fps_val = None

    original_filename = info.get("original_filename", "")
    meta = {
        "project_id": info.get("id") or pdir.name,
        "created_at": info.get("created") or datetime.now().isoformat(),
        "original_filename": original_filename,
        "project_name": original_filename,
        "duration_sec": info.get("duration"),
        "resolution": resolution,
        "frames_count": frames_count,
        # default frame-generation metadata for legacy projects
        "frames_status": "not_started",
        "frames_expected": 0,
        "frames_generated": frames_count,
        "frames_settings_hash": None,
        "frames_generated_at": None,
    }
    if fps_val is not None:
        meta["fps"] = fps_val

    mpath.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return meta


def update_metadata_frames(pdir: Path, frames_count: int):
    """Update frames_count in metadata.json (create file if missing)."""
    try:
        meta = ensure_metadata(pdir)
        meta["frames_count"] = int(frames_count)
        _metadata_path(pdir).write_text(json.dumps(meta, indent=2), encoding="utf-8")
    except Exception:
        # Non-critical; listing will recompute best-effort.
        pass


def _set_frames_generation_metadata(
    pdir: Path,
    *,
    status: str,
    expected: Optional[int] = None,
    generated: Optional[int] = None,
    settings_hash: Optional[str] = None,
    frames_count: Optional[int] = None,
) -> None:
    """
    Helper to update frame-generation related fields in metadata.json.

    - status: one of not_started, generating, completed, stopped, failed
    - expected: planned number of frames (N)
    - generated: actually generated frames so far (K)
    - settings_hash: hash of (start, end, density, quality)
    - frames_count: convenience mirror of K for legacy UI
    """
    try:
        meta = ensure_metadata(pdir)
        meta["frames_status"] = status
        if expected is not None:
            meta["frames_expected"] = int(expected)
        if generated is not None:
            meta["frames_generated"] = int(generated)
        if settings_hash is not None:
            meta["frames_settings_hash"] = settings_hash
        if frames_count is not None:
            meta["frames_count"] = int(frames_count)
        if status in ("completed", "stopped", "failed"):
            meta["frames_generated_at"] = datetime.now().isoformat()
        elif status in ("generating", "not_started"):
            meta["frames_generated_at"] = None
        _metadata_path(pdir).write_text(json.dumps(meta, indent=2), encoding="utf-8")
    except Exception:
        # Failure to persist metadata should not break core workflow.
        pass


def compute_frames_settings_hash(
    start: float,
    end: float,
    num_frames: int,
    user_quality: int,
) -> str:
    """
    Stable hash for current slicing settings (Start, End, density, quality).
    Used only for metadata/consistency checks; no security assumptions.
    """
    payload = f"{start:.6f}|{end:.6f}|{int(num_frames)}|{int(user_quality)}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

# ── FFmpeg / FFprobe helpers ─────────────────────────────────────────────

def _find_bin(name: str) -> str:
    """Return path to ffmpeg / ffprobe binary."""
    try:
        subprocess.run(
            [name, "-version"],
            capture_output=True, check=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return name
    except (FileNotFoundError, subprocess.CalledProcessError):
        pass
    ext = ".exe" if os.name == "nt" else ""
    local = BASE_DIR / "bin" / (name + ext)
    if local.exists():
        return str(local)
    raise RuntimeError(
        f"{name} not found. Install FFmpeg (https://ffmpeg.org) and add to PATH, "
        f"or put {name}{ext} into {BASE_DIR / 'bin'}/"
    )

_FFMPEG: Optional[str] = None
_FFPROBE: Optional[str] = None


# ── Error / exception handlers ──────────────────────────────────────────────


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    """
    Return structured JSON for all HTTP errors.

    Shape:
      { "error": { "code": "http_<status>", "message": "...", "details": ... } }
    """
    # Normalise detail to string for message, keep original in details.
    detail = exc.detail
    if isinstance(detail, (dict, list)):
        message = json.dumps(detail, ensure_ascii=False)
    else:
        message = str(detail)

    payload = {
        "error": {
            "code": f"http_{exc.status_code}",
            "message": message or f"HTTP {exc.status_code}",
            "details": detail,
        }
    }
    # Print for debugging of 4xx/5xx flows.
    print(f"[backend] HTTPException {exc.status_code} at {request.url}: {message}")
    return JSONResponse(status_code=exc.status_code, content=payload)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    """
    Catch-all handler: always return structured JSON and log full stacktrace.
    """
    import traceback

    print(f"[backend] Unhandled exception at {request.url}: {exc}")
    traceback.print_exc()

    payload = {
        "error": {
            "code": "internal_error",
            "message": "Internal server error",
            "details": str(exc),
        }
    }
    return JSONResponse(status_code=500, content=payload)

def ffmpeg() -> str:
    global _FFMPEG
    if _FFMPEG is None:
        _FFMPEG = _find_bin("ffmpeg")
    return _FFMPEG

def ffprobe() -> str:
    global _FFPROBE
    if _FFPROBE is None:
        _FFPROBE = _find_bin("ffprobe")
    return _FFPROBE

_CF = getattr(subprocess, "CREATE_NO_WINDOW", 0)

def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True,
                          check=True, creationflags=_CF)

def video_duration(path: str) -> float:
    r = _run([ffprobe(), "-v", "error",
              "-show_entries", "format=duration",
              "-of", "default=noprint_wrappers=1:nokey=1", path])
    return float(r.stdout.strip())

def video_resolution(path: str) -> tuple[int, int]:
    r = _run([ffprobe(), "-v", "error", "-select_streams", "v:0",
              "-show_entries", "stream=width,height",
              "-of", "csv=p=0", path])
    w, h = r.stdout.strip().split(",")
    return int(w), int(h)


def video_fps(path: str) -> float:
    """Return frames per second for the first video stream.

    Prefers avg_frame_rate (accurate for VFR content) over r_frame_rate
    (which may report codec-level tbr, e.g. 300 for a 30 fps phone video).
    Falls back to r_frame_rate if avg_frame_rate is unavailable or zero.
    """
    def _parse_fps(value: str) -> float:
        value = value.strip()
        if "/" in value:
            num, den = value.split("/", 1)
            num_f = float(num)
            den_f = float(den)
            if den_f == 0:
                return num_f
            return num_f / den_f
        return float(value)

    # Try avg_frame_rate first (more reliable for VFR / phone videos)
    try:
        r = _run(
            [
                ffprobe(),
                "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=avg_frame_rate",
                "-of", "default=noprint_wrappers=1:nokey=1",
                path,
            ]
        )
        avg = _parse_fps(r.stdout)
        if avg > 0:
            return avg
    except Exception:
        pass

    # Fallback: r_frame_rate
    try:
        r = _run(
            [
                ffprobe(),
                "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=r_frame_rate",
                "-of", "default=noprint_wrappers=1:nokey=1",
                path,
            ]
        )
        return _parse_fps(r.stdout)
    except Exception:
        return 25.0

def extract_frame(video: str, t: float, out: str, quality: int = 2):
    """
    Extract a single JPEG frame at timestamp t using given FFmpeg quality (qscale).
    Always keeps the original resolution of the source video (no resize).
    """
    cmd = [
        ffmpeg(),
        "-y",
        "-ss",
        str(t),
        "-i",
        video,
        "-frames:v",
        "1",
        "-q:v",
        str(quality),
        out,
    ]
    _run(cmd)

def resize_image(src: str, out: str, width: int = 160, quality: int = 8):
    """Create small thumbnail from existing JPEG."""
    _run([ffmpeg(), "-y", "-i", src,
          "-vf", f"scale={width}:-2", "-q:v", str(quality), out])


def _map_user_quality_to_qscale(user_quality: int) -> int:
    """
    Map UI quality 0..100 to FFmpeg qscale 1..31 (1 = best, 31 = worst).
    This helper is intentionally simple and duplicated logic from /estimate
    and /generate to avoid changing existing behaviour.
    """
    uq = max(0, min(100, int(user_quality)))
    qscale = round(31 - (uq / 100.0) * 30)
    if qscale < 1:
        qscale = 1
    if qscale > 31:
        qscale = 31
    return qscale

# ── Change-based frame selection (OpenCV) ────────────────────────────────

# Configurable constants for change-based mode
CHANGE_ANALYSIS_WIDTH = 320          # downscale width for analysis
CHANGE_SCORE_NOISE_THRESHOLD = 0.5   # scores below this are treated as 0
CHANGE_STATIC_TOTAL_THRESHOLD = 1.0  # if total cumulative_change < this, fallback to time-based
MAX_FPS_VIRTUAL = 20                 # upper density bound (frames per second)
HARD_CAP_FRAMES = 2000              # absolute max frames


def _compute_change_scores(
    video_path: str,
    start_sec: float,
    end_sec: float,
    fps: float,
    analysis_width: int = CHANGE_ANALYSIS_WIDTH,
) -> list[dict]:
    """
    Analyse video fragment and compute per-frame-pair change scores.

    Returns list of dicts:
      [{ "index": i, "timestamp": t, "score": float }, ...]
    where score[0] = 0 (first frame has no predecessor).

    Uses grayscale + blur + mean absolute difference.
    """
    _require_cv2()

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise RuntimeError(f"Cannot open video: {video_path}")

    # Seek to start
    cap.set(cv2.CAP_PROP_POS_MSEC, start_sec * 1000.0)

    # Compute timestamps for every source frame in the fragment
    if fps <= 0:
        fps = 25.0
    frame_interval = 1.0 / fps
    timestamps = []
    t = start_sec
    while t <= end_sec + frame_interval * 0.5:
        timestamps.append(t)
        t += frame_interval

    if not timestamps:
        cap.release()
        return []

    results = []
    prev_gray = None

    for i, ts in enumerate(timestamps):
        cap.set(cv2.CAP_PROP_POS_MSEC, ts * 1000.0)
        ret, frame = cap.read()
        if not ret or frame is None:
            # If we can't read, treat as zero change
            results.append({"index": i, "timestamp": round(ts, 6), "score": 0.0})
            continue

        # Downscale
        h, w = frame.shape[:2]
        if w > analysis_width and w > 0:
            scale = analysis_width / w
            new_w = analysis_width
            new_h = max(1, int(h * scale))
            frame = cv2.resize(frame, (new_w, new_h), interpolation=cv2.INTER_AREA)

        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        gray = cv2.GaussianBlur(gray, (5, 5), 0)
        # Normalize brightness: subtract mean to reduce auto-exposure flicker
        gray = gray.astype(np.float32)
        gray -= gray.mean()

        if prev_gray is None:
            results.append({"index": i, "timestamp": round(ts, 6), "score": 0.0})
        else:
            diff = np.abs(gray - prev_gray)
            score = float(np.mean(diff))
            # Apply noise threshold
            if score < CHANGE_SCORE_NOISE_THRESHOLD:
                score = 0.0
            results.append({"index": i, "timestamp": round(ts, 6), "score": round(score, 4)})

        prev_gray = gray

    cap.release()
    return results


def _select_frames_by_change(
    scores: list[dict],
    target_n: int,
) -> list[int]:
    """
    Given per-frame scores, select target_n frame indices so that
    cumulative change is divided into equal intervals.

    Always includes first and last frame.
    Falls back to uniform time-based if video is nearly static.
    """
    if not scores:
        return []
    total = len(scores)
    if target_n <= 1:
        return [0]
    if target_n >= total:
        return list(range(total))

    # Compute cumulative change
    cumulative = [0.0] * total
    for i in range(1, total):
        cumulative[i] = cumulative[i - 1] + scores[i]["score"]

    total_change = cumulative[-1]

    # Fallback: if video is nearly static, use uniform spacing
    if total_change < CHANGE_STATIC_TOTAL_THRESHOLD:
        print(f"[backend] change-based: cumulative_change={total_change:.4f} < threshold={CHANGE_STATIC_TOTAL_THRESHOLD}, falling back to time-based")
        step = (total - 1) / (target_n - 1)
        indices = [round(i * step) for i in range(target_n)]
        indices[0] = 0
        indices[-1] = total - 1
        return indices

    # Select frames at equal cumulative-change intervals
    change_step = total_change / (target_n - 1)
    selected = [0]

    for k in range(1, target_n - 1):
        target_cum = k * change_step
        # Find closest frame index
        best_idx = selected[-1] + 1
        best_dist = abs(cumulative[best_idx] - target_cum)
        for j in range(best_idx + 1, total):
            dist = abs(cumulative[j] - target_cum)
            if dist < best_dist:
                best_dist = dist
                best_idx = j
            elif dist > best_dist:
                break  # cumulative is monotonic, so we can stop
        selected.append(best_idx)

    selected.append(total - 1)

    # Deduplicate while preserving order
    seen = set()
    unique = []
    for idx in selected:
        if idx not in seen:
            seen.add(idx)
            unique.append(idx)

    return unique


# ── Routes ───────────────────────────────────────────────────────────────

@app.get("/health")
async def health_check():
    """Health check endpoint used by start/stop scripts."""
    # #region agent log
    try:
        import json as _json_dbg
        with open(str(BASE_DIR / ".cursor" / "debug.log"), "a", encoding="utf-8") as _dbg_f:
            _dbg_f.write(_json_dbg.dumps({"timestamp": int(datetime.now().timestamp() * 1000), "location": "main.py:health_check", "message": "Health check called", "data": {"status": "ok"}, "hypothesisId": "H2"}) + "\n")
    except Exception:
        pass
    # #endregion
    return {"status": "ok"}


@app.get("/")
async def index():
    resp = FileResponse(str(FRONTEND_DIR / "index.html"))
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    return resp


@app.get("/project/{pid}")
async def project_spa_route(pid: str):
    """
    SPA catch-all: serve the same index.html for /project/<id> URLs.
    The frontend JavaScript reads window.location to restore the correct screen.
    """
    resp = FileResponse(str(FRONTEND_DIR / "index.html"))
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    return resp

# ─── Upload ──────────────────────────────────────────────────────────────

@app.post("/api/upload")
async def upload_video(file: UploadFile = File(...)):
    pid = uuid.uuid4().hex[:8]
    pdir = PROJECTS_DIR / pid
    pdir.mkdir(parents=True)

    safe = file.filename.replace(" ", "_")
    vpath = pdir / f"original_{safe}"
    vpath.write_bytes(await file.read())

    vpath_str = str(vpath)
    dur = video_duration(vpath_str)
    w, h = video_resolution(vpath_str)
    fps = video_fps(vpath_str)
    file_size = os.path.getsize(vpath_str)
    total_frames_video = max(1, round(dur * fps))

    info = {
        "id": pid,
        "original_filename": file.filename,
        "video_file": vpath.name,
        "duration": round(dur, 3),
        "width": w,
        "height": h,
        "fps": round(fps, 3),
        "file_size_bytes": file_size,
        "total_frames_video": total_frames_video,
        "created": datetime.now().isoformat(),
    }
    (pdir / "project.json").write_text(json.dumps(info, indent=2), encoding="utf-8")

    # metadata.json — файловое описание проекта для списка/открытия
    meta = {
        "project_id": pid,
        "created_at": info["created"],
        "original_filename": info["original_filename"],
        "project_name": info["original_filename"],
        "duration_sec": info["duration"],
        "resolution": f"{w}x{h}",
        "frames_count": 0,
        "fps": round(fps, 3),
        "file_size_bytes": file_size,
        "total_frames_video": total_frames_video,
        # frame-generation metadata
        "frames_status": "not_started",
        "frames_expected": 0,
        "frames_generated": 0,
        "frames_settings_hash": None,
        "frames_generated_at": None,
    }
    (_metadata_path(pdir)).write_text(json.dumps(meta, indent=2), encoding="utf-8")

    return info

# ─── Video preview stream ────────────────────────────────────────────────

@app.get("/api/projects/{pid}/video")
async def serve_video(pid: str):
    pdir = PROJECTS_DIR / pid
    if not pdir.exists():
        raise HTTPException(404)
    info = json.loads((pdir / "project.json").read_text(encoding="utf-8"))
    vfile = info.get("video_file", "")
    if not vfile:
        raise HTTPException(404, "Исходное видео недоступно (проект импортирован из ZIP)")
    vpath = pdir / vfile
    if not vpath.exists():
        raise HTTPException(404, "Исходное видео не найдено")
    return FileResponse(str(vpath))


@app.get("/api/projects/{pid}/preview_frame")
async def preview_frame(pid: str, time_ms: int):
    """
    Lightweight frame preview endpoint for UI scrubbing on the clip timeline.
    Kept for backwards compatibility; internally delegates to get_preview_frame
    with a default thumbnail width.
    """
    return await get_preview_frame(pid, time_ms=time_ms, max_width=320)


@app.get("/api/projects/{pid}/get_preview_frame")
async def get_preview_frame(pid: str, time_ms: int, max_width: int = 240):
    """
    Extract and cache a small JPEG preview frame for the given project.

    - time_ms: timestamp in milliseconds (rounded to 100 ms buckets)
    - max_width: desired thumbnail width in pixels (clamped to 80..640)

    The preview is cached on disk inside the project folder, so repeated
    requests with the same (pid, time_ms_bucket, max_width) are instant.
    """
    pdir = PROJECTS_DIR / pid
    if not pdir.exists():
        raise HTTPException(404, "Project not found")

    # Normalise and round timestamp: buckets of 100 ms are enough for UI
    if time_ms < 0:
        time_ms = 0
    bucket_ms = int(round(time_ms / 100.0) * 100)

    # Clamp width to a reasonable range
    try:
        max_width = int(max_width)
    except Exception:
        max_width = 240
    max_width = max(80, min(max_width, 640))

    previews_dir = pdir / "preview_cache"
    previews_dir.mkdir(exist_ok=True)
    out_fp = previews_dir / f"preview_{bucket_ms:09d}_w{max_width}.jpg"

    if not out_fp.exists():
        # Read project video path
        pinfo_fp = pdir / "project.json"
        if not pinfo_fp.exists():
            raise HTTPException(404, "project.json not found")
        info = json.loads(pinfo_fp.read_text(encoding="utf-8"))
        vpath = str(pdir / info["video_file"])

        # Clamp timestamp to slightly before the end of the video to avoid
        # seeking past the last frame.
        try:
            dur = video_duration(vpath)
        except Exception:
            # fallback to metadata duration (seconds) if available
            dur = info.get("duration") or 0

        t_sec = bucket_ms / 1000.0
        if isinstance(dur, (int, float)) and dur > 0:
            t_sec = max(0.0, min(t_sec, dur - 0.05))
        else:
            t_sec = max(0.0, t_sec)

        # Extract and resize in a single FFmpeg call
        try:
            cmd = [
                ffmpeg(),
                "-y",
                "-ss",
                str(t_sec),
                "-i",
                vpath,
                "-frames:v",
                "1",
                "-vf",
                f"scale={max_width}:-2",
                "-q:v",
                "4",
                str(out_fp),
            ]
            _run(cmd)
        except subprocess.CalledProcessError as e:
            raise HTTPException(500, f"FFmpeg error: {e.stderr}")

    return FileResponse(str(out_fp), media_type="image/jpeg")


def _ensure_quality_preview_file(
    pid: str,
    time_ms: int,
    user_quality: int,
    max_width: int,
) -> tuple[Path, int]:
    """
    Internal helper to generate (or load from cache) a JPEG preview frame
    for the quality comparison block.

    Caching key (on disk):
      project_id + time_ms_bucket + quality_bucket + width
    where:
      - time_ms_bucket is rounded to 100 ms
      - quality_bucket is rounded to steps of 5 (0..100)
    """
    pdir = PROJECTS_DIR / pid
    if not pdir.exists():
        raise HTTPException(404, "Project not found")

    # timestamp bucket (100 ms)
    if time_ms < 0:
        time_ms = 0
    bucket_ms = int(round(time_ms / 100.0) * 100)

    # width clamp 80..640
    try:
        max_width_int = int(max_width)
    except Exception:
        max_width_int = 240
    max_width_int = max(80, min(max_width_int, 640))

    # quality bucket (step = 5)
    try:
        uq = int(user_quality)
    except Exception:
        uq = 80
    if uq < 0:
        uq = 0
    if uq > 100:
        uq = 100
    quality_bucket = int(round(uq / 5.0) * 5)
    if quality_bucket < 0:
        quality_bucket = 0
    if quality_bucket > 100:
        quality_bucket = 100

    previews_dir = pdir / "quality_preview_cache"
    previews_dir.mkdir(exist_ok=True)
    out_fp = previews_dir / f"qprev_{bucket_ms:09d}_q{quality_bucket:03d}_w{max_width_int}.jpg"

    if not out_fp.exists():
        # project video
        pinfo_fp = pdir / "project.json"
        if not pinfo_fp.exists():
            raise HTTPException(404, "project.json not found")
        info = json.loads(pinfo_fp.read_text(encoding="utf-8"))
        vpath = str(pdir / info["video_file"])

        # clamp time to video duration
        try:
            dur = video_duration(vpath)
        except Exception:
            dur = info.get("duration") or 0

        t_sec = bucket_ms / 1000.0
        if isinstance(dur, (int, float)) and dur > 0:
            t_sec = max(0.0, min(t_sec, dur - 0.05))
        else:
            t_sec = max(0.0, t_sec)

        qscale = _map_user_quality_to_qscale(quality_bucket)

        try:
            cmd = [
                ffmpeg(),
                "-y",
                "-ss",
                str(t_sec),
                "-i",
                vpath,
                "-frames:v",
                "1",
                "-vf",
                f"scale={max_width_int}:-2",
                "-q:v",
                str(qscale),
                str(out_fp),
            ]
            _run(cmd)
        except subprocess.CalledProcessError as e:
            raise HTTPException(500, f"FFmpeg error: {e.stderr}")

    try:
        size_bytes = out_fp.stat().st_size
    except OSError:
        size_bytes = 0

    return out_fp, size_bytes


@app.get("/api/projects/{pid}/quality_preview_frame")
async def quality_preview_frame(
    pid: str,
    time_ms: int,
    user_quality: int,
    max_width: int = 240,
):
    """
    Generate (with caching) a JPEG frame for quality comparison.

    Query parameters:
      - time_ms: timestamp in milliseconds (float/int)
      - user_quality: UI quality 0..100
      - max_width: desired thumbnail width (80..640)

    Returns JSON:
      - preview_url: URL that serves the cached JPEG
      - file_size_bytes: size of the JPEG on disk
    """
    out_fp, size_bytes = _ensure_quality_preview_file(
        pid=pid,
        time_ms=time_ms,
        user_quality=user_quality,
        max_width=max_width,
    )

    # We expose a dedicated image endpoint that uses the same bucketing rules.
    preview_url = (
        f"/api/projects/{pid}/quality_preview_image"
        f"?time_ms={time_ms}&user_quality={user_quality}&max_width={max_width}"
    )
    return {
        "preview_url": preview_url,
        "file_size_bytes": int(size_bytes),
    }


@app.get("/api/projects/{pid}/quality_preview_image")
async def quality_preview_image(
    pid: str,
    time_ms: int,
    user_quality: int,
    max_width: int = 240,
):
    """
    Serve the cached JPEG for the quality comparison block. If it does not
    exist yet, it will be generated using the same caching rules as
    /quality_preview_frame.
    """
    out_fp, _ = _ensure_quality_preview_file(
        pid=pid,
        time_ms=time_ms,
        user_quality=user_quality,
        max_width=max_width,
    )
    return FileResponse(str(out_fp), media_type="image/jpeg")

# ─── Analyse changes for change-based frame selection ─────────────────────

# In-memory cache for analysis results (per project + clip range)
_change_analysis_cache: dict[str, dict] = {}


@app.post("/api/projects/{pid}/analyze_changes")
async def analyze_changes(
    pid: str,
    start: float = Form(...),
    end: float = Form(...),
    target_n: int = Form(...),
):
    """
    Analyse video fragment for visual changes and return frame indices
    selected by the change-based algorithm.

    Returns:
      - scores: per-source-frame change scores (for debugging/visualisation)
      - selected_indices: indices into the scores array for the target N frames
      - selected_timestamps: timestamps of selected frames
      - total_frames_analyzed: number of source frames in the fragment
      - fallback_to_time: whether the algorithm fell back to time-based
    """
    _require_cv2()

    pdir = PROJECTS_DIR / pid
    if not pdir.exists():
        raise HTTPException(404, "Project not found")

    pinfo_fp = pdir / "project.json"
    if not pinfo_fp.exists():
        raise HTTPException(404, "project.json not found")
    info = json.loads(pinfo_fp.read_text(encoding="utf-8"))

    vfile = info.get("video_file", "")
    if not vfile:
        raise HTTPException(400, "No original video (ZIP import?)")
    vpath = pdir / vfile
    if not vpath.exists():
        raise HTTPException(404, "Original video not found")

    fps = info.get("fps") or 25.0

    # Cache key: project + start + end (scores don't depend on target_n)
    cache_key = f"{pid}_{start:.3f}_{end:.3f}"

    if cache_key in _change_analysis_cache:
        scores = _change_analysis_cache[cache_key]["scores"]
        print(f"[backend] analyze_changes: using cached scores for {cache_key}, {len(scores)} frames")
    else:
        print(f"[backend] analyze_changes: computing scores for pid={pid} start={start} end={end} fps={fps}")
        scores = _compute_change_scores(str(vpath), start, end, fps)
        _change_analysis_cache[cache_key] = {"scores": scores}
        print(f"[backend] analyze_changes: computed {len(scores)} frame scores")

    # Select frames
    target_n = max(3, min(target_n, len(scores) or 1))
    score_values = scores  # list of dicts with "score" key
    selected_indices = _select_frames_by_change(score_values, target_n)

    # Check if fallback occurred
    cumulative_total = sum(s["score"] for s in scores)
    fallback = cumulative_total < CHANGE_STATIC_TOTAL_THRESHOLD

    selected_timestamps = [scores[i]["timestamp"] for i in selected_indices if i < len(scores)]

    return {
        "total_frames_analyzed": len(scores),
        "selected_indices": selected_indices,
        "selected_timestamps": selected_timestamps,
        "num_selected": len(selected_indices),
        "fallback_to_time": fallback,
        "cumulative_change": round(cumulative_total, 4),
    }


# ─── Estimate ────────────────────────────────────────────────────────────

@app.post("/api/projects/{pid}/estimate")
async def estimate(
    pid: str,
    start: float = Form(...),
    end: float = Form(...),
    num_frames: int = Form(...),
    user_quality: int = Form(...),
):
    pdir = PROJECTS_DIR / pid
    if not pdir.exists():
        raise HTTPException(404, "Project not found")

    info = json.loads((pdir / "project.json").read_text(encoding="utf-8"))
    vpath = str(pdir / info["video_file"])

    # Map user quality 0..100 → FFmpeg qscale 1..31 (1 = best, 31 = worst)
    uq = max(0, min(100, int(user_quality)))
    qscale = round(31 - (uq / 100.0) * 30)
    if qscale < 1:
        qscale = 1
    if qscale > 31:
        qscale = 31

    test_path = str(pdir / "test_frame.jpg")
    mid = (start + end) / 2.0
    try:
        extract_frame(vpath, mid, test_path, qscale)
    except subprocess.CalledProcessError as e:
        raise HTTPException(500, f"FFmpeg error: {e.stderr}")

    test_size = os.path.getsize(test_path)
    fw, fh = video_resolution(test_path)
    original_bytes = os.path.getsize(vpath)

    return {
        "duration": round(end - start, 3),
        "num_frames": num_frames,
        "single_frame_bytes": test_size,
        "estimated_total_bytes": test_size * num_frames,
        "original_bytes": original_bytes,
        "frame_width": fw,
        "frame_height": fh,
        "test_frame_url": f"/api/projects/{pid}/test_frame",
        "user_quality": uq,
        "ffmpeg_qscale": qscale,
    }

@app.get("/api/projects/{pid}/test_frame")
async def test_frame(pid: str):
    p = PROJECTS_DIR / pid / "test_frame.jpg"
    if not p.exists():
        raise HTTPException(404)
    return FileResponse(str(p), media_type="image/jpeg")

# ─── Generate frames (background) ────────────────────────────────────────

def _generate_task(
    pid: str,
    start: float,
    end: float,
    num_frames: int,
    user_quality: int,
    ffmpeg_qscale: int,
    frames_settings_hash: str,
    frame_selection_mode: str = "time",
    custom_timestamps: Optional[list[float]] = None,
) -> None:
    """
    Background task: extract frames from video.

    Atomic generation strategy:
    1. Generate frames into temporary directories (_frames_gen_<id>, _thumbs_gen_<id>).
    2. On success: remove old frames/thumbs, rename temp dirs to frames/thumbs.
    3. On failure/cancel: remove temp dirs, old frames remain untouched.
    """
    pdir = PROJECTS_DIR / pid
    generation_id = uuid.uuid4().hex[:12]
    print(f"[backend] _generate_task: start pid={pid} generation_id={generation_id} "
          f"start={start} end={end} num_frames={num_frames} "
          f"user_quality={user_quality} mode={frame_selection_mode}")

    info = json.loads((pdir / "project.json").read_text(encoding="utf-8"))
    vpath = str(pdir / info["video_file"])
    print(f"[backend] _generate_task: video path = {vpath}")

    # Use temporary directories for atomic generation
    temp_frames_dir = pdir / f"_frames_gen_{generation_id}"
    temp_thumbs_dir = pdir / f"_thumbs_gen_{generation_id}"
    temp_frames_dir.mkdir(exist_ok=True)
    temp_thumbs_dir.mkdir(exist_ok=True)
    print(f"[backend] _generate_task: temp dirs frames={temp_frames_dir} thumbs={temp_thumbs_dir}")

    # Get actual video duration for safe clamping
    try:
        dur = video_duration(vpath)
        print(f"[backend] _generate_task: probed video duration = {dur}")
    except Exception as exc:
        print(f"[backend] _generate_task: failed to probe duration, fallback, error={exc}")
        dur = end + 10  # fallback

    if custom_timestamps and len(custom_timestamps) > 0:
        # Change-based mode: use provided timestamps
        timestamps = custom_timestamps[:num_frames]
        num_frames = len(timestamps)
        print(f"[backend] _generate_task: using {num_frames} custom timestamps (change-based)")
    elif num_frames <= 1:
        timestamps = [start]
    else:
        step = (end - start) / (num_frames - 1)
        timestamps = [start + i * step for i in range(num_frames)]
    # Clamp: don't seek beyond last available frame
    timestamps = [min(t, dur - 0.05) for t in timestamps]

    # reset cancellation flag and progress at the very start
    generation_cancel_flags[pid] = False
    generation_progress[pid] = {
        "total": num_frames,
        "done": 0,
        "status": "generating",
    }
    _set_frames_generation_metadata(
        pdir,
        status="generating",
        expected=num_frames,
        generated=0,
        settings_hash=frames_settings_hash,
    )

    frames_list = []
    single_frame_size_bytes: Optional[int] = None
    import traceback

    def _cleanup_temp():
        """Remove temporary generation directories."""
        for d in (temp_frames_dir, temp_thumbs_dir):
            if d.exists():
                shutil.rmtree(str(d), ignore_errors=True)

    # Check if there were already completed frames before this generation
    old_frames_dir = pdir / "frames"
    old_index_fp = pdir / "index.json"
    had_old_frames = old_frames_dir.exists() and old_index_fp.exists()

    for i, ts in enumerate(timestamps):
        # cooperative cancellation: stop before generating next frame
        if generation_cancel_flags.get(pid):
            generation_progress[pid]["status"] = "stopped"
            generation_progress[pid]["done"] = i
            # Cancelled: remove temp dirs, keep old frames intact
            _cleanup_temp()
            if had_old_frames:
                # Revert metadata to "completed" since old frames are still valid
                try:
                    old_idx = json.loads(old_index_fp.read_text(encoding="utf-8"))
                    old_count = old_idx.get("num_frames", 0)
                except Exception:
                    old_count = 0
                _set_frames_generation_metadata(
                    pdir,
                    status="completed",
                    expected=old_count,
                    generated=old_count,
                    frames_count=old_count,
                )
            else:
                _set_frames_generation_metadata(
                    pdir,
                    status="stopped",
                    expected=num_frames,
                    generated=i,
                    settings_hash=frames_settings_hash,
                )
            return
        fname = f"frame_{i:05d}.jpg"
        frame_path = str(temp_frames_dir / fname)
        try:
            extract_frame(vpath, ts, frame_path, ffmpeg_qscale)
            # thumbnail
            thumb_path = str(temp_thumbs_dir / f"thumb_{i:05d}.jpg")
            resize_image(frame_path, thumb_path, 160, 8)
        except Exception as e:
            print(f"[backend] _generate_task: exception on frame {i} at ts={ts}: {e}")
            traceback.print_exc()
            generation_progress[pid]["status"] = f"error: {e}"
            # Error: remove temp dirs, keep old frames intact
            _cleanup_temp()
            if had_old_frames:
                # Revert metadata to "completed" since old frames are still valid
                try:
                    old_idx = json.loads(old_index_fp.read_text(encoding="utf-8"))
                    old_count = old_idx.get("num_frames", 0)
                except Exception:
                    old_count = 0
                _set_frames_generation_metadata(
                    pdir,
                    status="completed",
                    expected=old_count,
                    generated=old_count,
                    frames_count=old_count,
                )
            else:
                _set_frames_generation_metadata(
                    pdir,
                    status="failed",
                    expected=num_frames,
                    generated=i,
                    settings_hash=frames_settings_hash,
                )
            return

        # Remember size of the first generated frame for index.json
        if single_frame_size_bytes is None:
            try:
                single_frame_size_bytes = os.path.getsize(frame_path)
            except OSError:
                single_frame_size_bytes = None

        m, s = divmod(ts, 60)
        frames_list.append({
            "index": i,
            "filename": fname,
            "timecode": f"{int(m):02d}:{s:06.3f}",
            "timestamp": round(ts, 3),
        })
        generation_progress[pid]["done"] = i + 1

    # ── Atomic swap: replace old frames/thumbs with new ones ──
    print(f"[backend] _generate_task: all {num_frames} frames generated, performing atomic swap")
    frames_dir = pdir / "frames"
    thumbs_dir = pdir / "thumbs"
    try:
        # Remove old directories
        if frames_dir.exists():
            shutil.rmtree(str(frames_dir))
        if thumbs_dir.exists():
            shutil.rmtree(str(thumbs_dir))
        # Also remove old index.json before writing new one
        old_index = pdir / "index.json"
        if old_index.exists():
            old_index.unlink()
        # Remove related caches that reference old frames
        for cache_dir_name in ("preview_cache", "quality_preview_cache"):
            cache_dir = pdir / cache_dir_name
            if cache_dir.exists():
                shutil.rmtree(str(cache_dir), ignore_errors=True)
        test_frame_fp = pdir / "test_frame.jpg"
        if test_frame_fp.exists():
            test_frame_fp.unlink(missing_ok=True)
        # Rename temp dirs to active
        temp_frames_dir.rename(frames_dir)
        temp_thumbs_dir.rename(thumbs_dir)
    except Exception as e:
        print(f"[backend] _generate_task: atomic swap failed: {e}")
        traceback.print_exc()
        _cleanup_temp()
        generation_progress[pid]["status"] = f"error: {e}"
        _set_frames_generation_metadata(
            pdir,
            status="failed",
            expected=num_frames,
            generated=0,
            settings_hash=frames_settings_hash,
        )
        return

    clip_duration = max(0.0, end - start)
    time_step = clip_duration / (num_frames - 1) if num_frames > 1 else 0.0
    fps_val = info.get("fps")

    index_data = {
        "project_id": pid,
        "generation_id": generation_id,
        "start": start,
        "end": end,
        "duration": round(clip_duration, 6),
        "num_frames": num_frames,
        "time_step": round(time_step, 6),
        "fps": fps_val,
        "user_quality": int(user_quality),
        "ffmpeg_qscale": int(ffmpeg_qscale),
        "single_frame_size_bytes": single_frame_size_bytes,
        "frame_selection_mode": frame_selection_mode,
        "frames": frames_list,
    }
    (pdir / "index.json").write_text(json.dumps(index_data, indent=2), encoding="utf-8")

    # Clear all marker and zone instances on regeneration (new frame set = clean annotations).
    # Future: could add "regeneration history" (archive frames with annotations by iteration, optional manual transfer).
    _save_markers(pdir, [])
    _save_zones(pdir, [])

    # Save last_generated_settings so the frontend can detect draft changes
    last_generated_settings = {
        "start": start,
        "end": end,
        "num_frames": num_frames,
        "user_quality": int(user_quality),
        "frame_selection_mode": frame_selection_mode,
    }

    # обновляем metadata.json количеством кадров и финальный статус генерации
    update_metadata_frames(pdir, num_frames)
    _set_frames_generation_metadata(
        pdir,
        status="completed",
        expected=num_frames,
        generated=num_frames,
        settings_hash=frames_settings_hash,
        frames_count=num_frames,
    )
    # Persist generation settings and generation_id into metadata
    try:
        meta = ensure_metadata(pdir)
        meta["generation_id"] = generation_id
        meta["last_generated_settings"] = last_generated_settings
        _metadata_path(pdir).write_text(json.dumps(meta, indent=2), encoding="utf-8")
    except Exception:
        pass  # non-critical

    generation_progress[pid]["status"] = "done"


@app.post("/api/projects/{pid}/generate")
async def generate(
    pid: str,
    bg: BackgroundTasks,
    start: float = Form(...),
    end: float = Form(...),
    num_frames: int = Form(...),
    user_quality: int = Form(...),
    frame_selection_mode: str = Form("time"),
    change_timestamps: str = Form(""),
):
    pdir = PROJECTS_DIR / pid
    if not pdir.exists():
        raise HTTPException(404, "Project not found")

    # Basic logging of incoming parameters
    print(
        "[backend] /generate: request",
        json.dumps(
            {
                "pid": pid,
                "start": start,
                "end": end,
                "num_frames": num_frames,
                "user_quality": user_quality,
            },
            ensure_ascii=False,
        ),
    )

    # Ensure core project files exist
    pinfo_fp = pdir / "project.json"
    if not pinfo_fp.exists():
        raise HTTPException(404, "project.json not found")
    info = json.loads(pinfo_fp.read_text(encoding="utf-8"))

    vpath = pdir / info.get("video_file", "")
    if not vpath.exists():
        raise HTTPException(500, "Original video file not found")

    # Detect already running generation for this project
    existing = generation_progress.get(pid)
    if existing and str(existing.get("status", "")).startswith(("generating", "starting")):
        print(f"[backend] /generate: already running for pid={pid}, state={existing}")
        return {
            "status": "already_running",
            "already_running": True,
            "progress": existing,
        }

    # Map user quality 0..100 → FFmpeg qscale 1..31 (1 = best, 31 = worst)
    uq = max(0, min(100, int(user_quality)))
    qscale = round(31 - (uq / 100.0) * 30)
    if qscale < 1:
        qscale = 1
    if qscale > 31:
        qscale = 31

    settings_hash = compute_frames_settings_hash(start, end, num_frames, uq)

    # Reset cancellation flag and mark metadata as "generating" for this run.
    generation_cancel_flags[pid] = False
    _set_frames_generation_metadata(
        pdir,
        status="generating",
        expected=num_frames,
        generated=0,
        settings_hash=settings_hash,
    )

    # Pre-populate in-memory progress so that the very first /progress call
    # already returns a meaningful status instead of "unknown".
    generation_progress[pid] = {
        "total": int(num_frames),
        "done": 0,
        "status": "generating",
    }

    # Parse custom timestamps for change-based mode
    custom_ts: Optional[list[float]] = None
    mode = frame_selection_mode.strip().lower() if frame_selection_mode else "time"
    if mode not in ("time", "change"):
        mode = "time"
    if mode == "change" and change_timestamps:
        try:
            custom_ts = [float(x.strip()) for x in change_timestamps.split(",") if x.strip()]
        except (ValueError, TypeError):
            custom_ts = None

    print(
        "[backend] /generate: starting background task",
        json.dumps(
            {
                "pid": pid,
                "start": start,
                "end": end,
                "num_frames": num_frames,
                "user_quality": uq,
                "ffmpeg_qscale": qscale,
                "settings_hash": settings_hash,
                "frame_selection_mode": mode,
                "custom_timestamps_count": len(custom_ts) if custom_ts else 0,
            },
            ensure_ascii=False,
        ),
    )
    bg.add_task(
        _generate_task, pid, start, end, num_frames, uq, qscale, settings_hash,
        frame_selection_mode=mode,
        custom_timestamps=custom_ts,
    )
    return {"status": "started"}


@app.get("/api/projects/{pid}/progress")
async def progress(pid: str):
    """
    Return current generation progress for given project.

    If in‑memory state is missing (e.g. after restart), fall back to metadata
    to infer a best-effort status instead of returning a vague "unknown".
    """
    state = generation_progress.get(pid)
    if state is not None:
        return state

    pdir = PROJECTS_DIR / pid
    if not pdir.exists():
        raise HTTPException(404, "Project not found")

    try:
        meta = ensure_metadata(pdir)
    except Exception as exc:
        # Metadata issues should not completely break UI; log and return idle.
        print(f"[backend] /progress: failed to load metadata for pid={pid}: {exc}")
        return {"status": "idle"}

    frames_status = str(meta.get("frames_status", "not_started"))
    expected = int(meta.get("frames_expected") or 0)
    generated = int(meta.get("frames_generated") or 0)

    if frames_status == "generating":
        return {"status": "generating", "total": expected, "done": generated}
    if frames_status == "completed":
        total = expected or generated
        return {"status": "done", "total": total, "done": generated or total}
    if frames_status == "stopped":
        return {"status": "stopped", "total": expected, "done": generated}
    if frames_status == "failed":
        return {
            "status": "error",
            "total": expected,
            "done": generated,
        }

    # Default: no generation has been run yet.
    return {"status": "idle", "total": expected, "done": generated}


@app.post("/api/projects/{pid}/frames/stop")
async def stop_frames(pid: str):
    """
    Request cooperative cancellation of the current frame generation task.
    The background loop checks the flag between frames and stops gracefully.
    """
    pdir = PROJECTS_DIR / pid
    if not pdir.exists():
        raise HTTPException(404, "Project not found")

    # If nothing is generating, we still respond with a success indicator,
    # but the flag will have no effect.
    generation_cancel_flags[pid] = True
    state = generation_progress.get(pid) or {"status": "unknown"}
    return {"status": "cancel_requested", "progress": state}

# ─── Frames access ───────────────────────────────────────────────────────

@app.get("/api/projects/{pid}/frames")
async def list_frames(pid: str):
    """
    Return frames index.json as-is.

    Instrumented with debug logs to understand missing/invalid index.json
    behaviour before applying fixes.
    """
    pdir = PROJECTS_DIR / pid
    p = pdir / "index.json"
    if not p.exists():
        raise HTTPException(404, "Frames not generated")

    try:
        raw = p.read_text(encoding="utf-8")
        data = json.loads(raw)
    except Exception as exc:
        raise

    return data


@app.get("/api/projects/{pid}/frames/{idx}")
async def get_frame(pid: str, idx: int):
    p = PROJECTS_DIR / pid / "frames" / f"frame_{idx:05d}.jpg"
    if not p.exists():
        raise HTTPException(404)
    return FileResponse(str(p), media_type="image/jpeg")


@app.get("/api/projects/{pid}/thumbs/{idx}")
async def get_thumb(pid: str, idx: int):
    p = PROJECTS_DIR / pid / "thumbs" / f"thumb_{idx:05d}.jpg"
    if not p.exists():
        p = PROJECTS_DIR / pid / "frames" / f"frame_{idx:05d}.jpg"
    if not p.exists():
        raise HTTPException(404)
    return FileResponse(str(p), media_type="image/jpeg")


# ─── Project listing / metadata ──────────────────────────────────────────


@app.get("/api/projects")
async def list_projects():
    """
    Вернуть упрощённый список проектов для главной страницы.
    Источник — metadata.json в каждой папке проекта или восстановление
    из project.json / index.json при отсутствии metadata.json.
    """
    items: list[dict] = []
    if not PROJECTS_DIR.exists():
        return []

    for pdir in PROJECTS_DIR.iterdir():
        if not pdir.is_dir():
            continue
        try:
            meta = ensure_metadata(pdir)
        except Exception:
            # пропускаем битые проекты
            continue

        has_annotations = (
            (pdir / "markers.json").exists()
            or (pdir / "annotations").exists()
            or (pdir / "annotations_index.json").exists()
        )
        items.append(
            {
                "project_id": meta.get("project_id", pdir.name),
                "created_at": meta.get("created_at"),
                "original_filename": meta.get("original_filename"),
                "project_name": meta.get(
                    "project_name", meta.get("original_filename") or ""
                ),
                "duration_sec": meta.get("duration_sec"),
                "frames_count": int(meta.get("frames_count") or 0),
                "has_annotations": has_annotations,
                "frames_status": meta.get("frames_status", "not_started"),
                "frames_expected": int(meta.get("frames_expected") or 0),
                "frames_generated": int(meta.get("frames_generated") or 0),
                "read_only": bool(meta.get("read_only", False)),
            }
        )

    # сортируем по дате создания по убыванию
    items.sort(key=lambda x: (x.get("created_at") or ""), reverse=True)

    return items


@app.get("/api/projects/{pid}")
async def get_project(pid: str):
    """
    Вернуть полные метаданные проекта.
    Совместимо с форматом /api/upload, чтобы фронт мог переиспользовать логику.
    """
    pdir = PROJECTS_DIR / pid
    if not pdir.exists():
        raise HTTPException(404, "Project not found")

    pinfo_fp = pdir / "project.json"
    if not pinfo_fp.exists():
        raise HTTPException(404, "project.json not found")

    info = json.loads(pinfo_fp.read_text(encoding="utf-8"))
    meta = ensure_metadata(pdir)

    has_annotations = (
        (pdir / "markers.json").exists()
        or (pdir / "annotations").exists()
        or (pdir / "annotations_index.json").exists()
    )

    # гарантируем наличие ожидаемых фронтом полей
    info.setdefault("id", pid)

    # Ensure file_size_bytes and total_frames_video are in metadata
    # (backfill for projects created before these fields existed).
    # Also re-probe fps if it was stored from r_frame_rate (can be wrong for VFR).
    needs_save = False
    try:
        vpath_str = str(pdir / info["video_file"])

        if "file_size_bytes" not in meta:
            meta["file_size_bytes"] = os.path.getsize(vpath_str)
            needs_save = True

        # Re-probe fps using avg_frame_rate for accuracy (old projects may have r_frame_rate)
        if "total_frames_video" not in meta:
            try:
                fps_val = round(video_fps(vpath_str), 3)
                meta["fps"] = fps_val
                info["fps"] = fps_val  # update response too
            except Exception:
                fps_val = meta.get("fps")

            dur_val = meta.get("duration_sec") or info.get("duration") or 0
            if fps_val and dur_val:
                meta["total_frames_video"] = max(1, round(dur_val * fps_val))
            else:
                meta["total_frames_video"] = 0
            needs_save = True

        if needs_save:
            _metadata_path(pdir).write_text(
                json.dumps(meta, indent=2), encoding="utf-8"
            )
    except Exception:
        pass

    # Восстановление сохранённых настроек проекта (clip/density/quality/autotune)
    settings = meta.get("settings") or {}

    # плоские поля для обратной совместимости с существующим фронтендом
    if "start_sec" in settings and "clip_start" not in meta:
        meta["clip_start"] = settings["start_sec"]
    if "end_sec" in settings and "clip_end" not in meta:
        meta["clip_end"] = settings["end_sec"]
    if "density_value" in settings and "density_frames" not in meta:
        meta["density_frames"] = settings["density_value"]
    if "quality_value" in settings and "user_quality" not in meta:
        meta["user_quality"] = settings["quality_value"]
    if "auto_tune_enabled" in settings and "autotune_enabled" not in meta:
        meta["autotune_enabled"] = settings["auto_tune_enabled"]
    if "frame_selection_mode" in settings and "frame_selection_mode" not in meta:
        meta["frame_selection_mode"] = settings["frame_selection_mode"]

    # обогащаем данными из metadata.json
    info.update(
        {
            "project_id": meta.get("project_id", pid),
            "created_at": meta.get("created_at"),
            "project_name": meta.get(
                "project_name", meta.get("original_filename", "")
            ),
            "frames_count": int(meta.get("frames_count") or 0),
            "has_annotations": has_annotations,
            "duration_sec": meta.get("duration_sec"),
            "resolution": meta.get("resolution"),
            "fps": meta.get("fps"),
            "file_size_bytes": meta.get("file_size_bytes"),
            "total_frames_video": meta.get("total_frames_video"),
            # frame-generation extended metadata
            "frames_status": meta.get("frames_status", "not_started"),
            "frames_expected": int(meta.get("frames_expected") or 0),
            "frames_generated": int(meta.get("frames_generated") or 0),
            "frames_settings_hash": meta.get("frames_settings_hash"),
            "frames_generated_at": meta.get("frames_generated_at"),
            # сохранённые настройки клипа / плотности / качества / автонстройки
            "clip_start": meta.get("clip_start"),
            "clip_end": meta.get("clip_end"),
            "density_frames": meta.get("density_frames"),
            "user_quality": meta.get("user_quality"),
            "autotune_enabled": meta.get("autotune_enabled"),
            "frame_selection_mode": meta.get("frame_selection_mode", "time"),
            "settings": settings,
            "read_only": bool(meta.get("read_only", False)),
            # generation versioning
            "generation_id": meta.get("generation_id"),
            "last_generated_settings": meta.get("last_generated_settings"),
            # viewer settings
            "max_sensitivity_project": meta.get("max_sensitivity_project"),
            "current_sensitivity": meta.get("current_sensitivity"),
            "markers_show_always": meta.get("markers_show_always", False),
        }
    )

    return info


# ─── Project rename / delete ─────────────────────────────────────────────


def _safe_project_dir(pid: str) -> Path:
    """
    Return a safe project directory path, preventing path traversal.
    Only simple folder names inside PROJECTS_DIR are allowed.
    """
    if any(sep in pid for sep in ("/", "\\")) or ".." in pid:
        raise HTTPException(400, "Invalid project_id")
    pdir = PROJECTS_DIR / pid
    try:
        resolved = pdir.resolve()
    except FileNotFoundError:
        # still enforce base directory
        resolved = pdir
    base = PROJECTS_DIR.resolve()
    if not str(resolved).startswith(str(base)):
        raise HTTPException(400, "Invalid project_id path")
    return resolved


@app.patch("/api/project/{pid}/name")
async def rename_project(pid: str, payload: dict = Body(...)):
    """
    Update project_name field in metadata.json for the given project.
    """
    project_name = payload.get("project_name")
    if not isinstance(project_name, str):
        raise HTTPException(400, "project_name must be a string")
    project_name = project_name.strip()
    if not project_name:
        raise HTTPException(400, "project_name must not be empty")
    if len(project_name) > 256:
        raise HTTPException(400, "project_name is too long")

    pdir = _safe_project_dir(pid)
    if not pdir.exists() or not pdir.is_dir():
        raise HTTPException(404, "Project not found")

    meta = ensure_metadata(pdir)
    meta["project_name"] = project_name
    _metadata_path(pdir).write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return {"status": "ok", "project_id": pid, "project_name": project_name}


@app.patch("/api/projects/{pid}/metadata")
async def update_project_metadata(pid: str, payload: dict = Body(...)):
    """
    Update metadata.json fields for the given project.
    Supports updating max_sensitivity_project and other viewer settings.
    """
    pdir = _safe_project_dir(pid)
    if not pdir.exists() or not pdir.is_dir():
        raise HTTPException(404, "Project not found")

    meta = ensure_metadata(pdir)
    changed = False

    # Update max_sensitivity_project if provided
    if "max_sensitivity_project" in payload:
        max_sens = payload["max_sensitivity_project"]
        if max_sens is None:
            # Remove the setting (use global default)
            if "max_sensitivity_project" in meta:
                del meta["max_sensitivity_project"]
                changed = True
        elif isinstance(max_sens, int) and 20 <= max_sens <= 200:
            meta["max_sensitivity_project"] = max_sens
            changed = True
        else:
            raise HTTPException(400, "max_sensitivity_project must be between 20 and 200, or null")

    # Update current_sensitivity if provided
    if "current_sensitivity" in payload:
        cur_sens = payload["current_sensitivity"]
        if isinstance(cur_sens, (int, float)):
            cur_sens = int(cur_sens)
            if 1 <= cur_sens <= 200:
                meta["current_sensitivity"] = cur_sens
                changed = True
            else:
                raise HTTPException(400, "current_sensitivity must be between 1 and 200")

    # Update markers_show_always if provided
    if "markers_show_always" in payload:
        val = payload["markers_show_always"]
        if isinstance(val, bool):
            meta["markers_show_always"] = val
            changed = True

    if changed:
        _metadata_path(pdir).write_text(json.dumps(meta, indent=2), encoding="utf-8")

    return {"status": "ok", "project_id": pid}


@app.delete("/api/project/{pid}")
async def delete_project(pid: str):
    """
    Recursively delete project directory from disk.
    Only directories inside PROJECTS_DIR are allowed.
    """
    pdir = _safe_project_dir(pid)
    if not pdir.exists():
        raise HTTPException(404, "Project not found")
    if not pdir.is_dir():
        raise HTTPException(400, "Project path is not a directory")

    try:
        shutil.rmtree(str(pdir))
    except Exception as exc:
        raise HTTPException(
            500,
            f"Не удалось удалить проект: {exc}",
        )
    return {"status": "deleted", "project_id": pid}


@app.post("/api/project/{pid}/save")
async def save_project(pid: str, payload: dict = Body(...)):
    """
    Explicitly persist current project settings to metadata.json.

    Frontend may send (all fields optional):
      - project_name: str
      - clip_start: float (seconds)
      - clip_end: float (seconds)
      - density_frames: int (planned frames count for current fragment)
      - user_quality: int (1..100, UI quality slider)
      - autotune_enabled: bool

    The handler merges provided fields into metadata.json and also updates
    last_saved_at timestamp for basic audit / debugging.
    """
    pdir = _safe_project_dir(pid)
    if not pdir.exists() or not pdir.is_dir():
        raise HTTPException(404, "Project not found")

    try:
        meta = ensure_metadata(pdir)
    except Exception as exc:
        raise HTTPException(500, f"Failed to load metadata: {exc}")

    # project_name
    name = payload.get("project_name")
    if isinstance(name, str):
        name = name.strip()
        if name:
            meta["project_name"] = name

    # numeric settings: clip_start / clip_end (seconds)
    def _as_float(val):
        try:
            return float(val)
        except Exception:
            return None

    clip_start = _as_float(payload.get("clip_start"))
    clip_end = _as_float(payload.get("clip_end"))

    # density: number of frames planned for current fragment
    density_frames_raw = payload.get("density_frames")
    density_frames: int | None
    if isinstance(density_frames_raw, (int, float)):
        try:
            density_frames = int(density_frames_raw)
        except Exception:
            density_frames = None
    else:
        density_frames = None

    # quality: UI slider 1..100
    user_quality_val = payload.get("user_quality")
    uq: int | None
    if isinstance(user_quality_val, (int, float)):
        try:
            uq = int(user_quality_val)
            if uq < 1:
                uq = 1
            if uq > 100:
                uq = 100
        except Exception:
            uq = None
    else:
        uq = None

    # autotune flag
    autotune_enabled = payload.get("autotune_enabled")
    if not isinstance(autotune_enabled, bool):
        autotune_enabled = None

    # frame selection mode: "time" or "change"
    fsm = payload.get("frame_selection_mode")
    if isinstance(fsm, str) and fsm in ("time", "change"):
        frame_selection_mode_val: Optional[str] = fsm
    else:
        frame_selection_mode_val = None

    # ── Unified settings block in metadata.json ──────────────────────────
    settings = meta.get("settings") or {}
    if clip_start is not None:
        settings["start_sec"] = clip_start
        meta["clip_start"] = clip_start
    if clip_end is not None:
        settings["end_sec"] = clip_end
        meta["clip_end"] = clip_end
    if density_frames is not None:
        settings["density_value"] = int(density_frames)
        meta["density_frames"] = int(density_frames)
    if uq is not None:
        settings["quality_value"] = int(uq)
        meta["user_quality"] = int(uq)
    if autotune_enabled is not None:
        settings["auto_tune_enabled"] = bool(autotune_enabled)
        meta["autotune_enabled"] = bool(autotune_enabled)
    if frame_selection_mode_val is not None:
        settings["frame_selection_mode"] = frame_selection_mode_val
        meta["frame_selection_mode"] = frame_selection_mode_val

    meta["settings"] = settings

    # technical timestamps
    now_iso = datetime.now().isoformat()
    meta.setdefault("created_at", now_iso)
    meta["updated_at"] = now_iso
    meta["last_saved_at"] = now_iso

    _metadata_path(pdir).write_text(json.dumps(meta, indent=2), encoding="utf-8")

    return {"status": "ok", "project_id": pid, "metadata": meta}


@app.post("/api/projects/{pid}/frames/clear")
async def clear_frames(pid: str):
    """
    Remove previously generated frames and related cached data for a project.

    Used when the user changes slicing settings or wants to re-generate frames
    from scratch. Manual artefacts like annotations are intentionally kept.
    """
    pdir = _safe_project_dir(pid)
    if not pdir.exists() or not pdir.is_dir():
        raise HTTPException(404, "Project not found")

    # auto-generated frame artefacts
    for dirname in ("frames", "thumbs", "preview_cache", "quality_preview_cache", "stabilized_frames", "centered_frames"):
        d = pdir / dirname
        if d.exists():
            shutil.rmtree(str(d), ignore_errors=True)

    # Clean up any leftover temporary generation directories
    for d in pdir.iterdir():
        if d.is_dir() and (d.name.startswith("_frames_gen_") or d.name.startswith("_thumbs_gen_")):
            shutil.rmtree(str(d), ignore_errors=True)

    for filename in ("index.json", "centered_index.json", "test_frame.jpg"):
        fp = pdir / filename
        if fp.exists():
            fp.unlink()

    # Reset in-memory progress / cancellation and metadata
    generation_progress.pop(pid, None)
    generation_cancel_flags.pop(pid, None)

    _set_frames_generation_metadata(
        pdir,
        status="not_started",
        expected=0,
        generated=0,
        settings_hash=None,
        frames_count=0,
    )

    return {"status": "cleared", "project_id": pid}

# ─── Point Markers (annotations) ─────────────────────────────────────────

def _load_markers(pdir: Path) -> list:
    fp = pdir / "markers.json"
    if not fp.exists():
        return []
    try:
        data = json.loads(fp.read_text(encoding="utf-8"))
        markers = data.get("markers", [])
        # Миграция: добавляем pin_show_text_always для существующих пометок
        needs_save = False
        for m in markers:
            if "pin_show_text_always" not in m:
                m["pin_show_text_always"] = False
                needs_save = True
        if needs_save:
            _save_markers(pdir, markers)
        return markers
    except Exception:
        return []


def _save_markers(pdir: Path, markers: list):
    (pdir / "markers.json").write_text(
        json.dumps({"markers": markers}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


@app.get("/api/projects/{pid}/markers")
async def list_markers(pid: str, generation_id: str = None):
    pdir = _safe_project_dir(pid)
    markers = _load_markers(pdir)
    if generation_id:
        markers = [m for m in markers if m.get("generation_id") == generation_id]
    return {"markers": markers}


def _get_default_marker_type_color(pdir: Path) -> str:
    """Return default marker type color for new default markers."""
    data = _load_marker_types_data(pdir)
    return data.get("default", {}).get("color", "#3fb950")


@app.post("/api/projects/{pid}/markers")
async def create_marker(pid: str, request: Request):
    pdir = _safe_project_dir(pid)
    body = await request.json()
    type_id = body.get("type_id", "")
    type_title = body.get("type_title", "")
    type_color = body.get("type_color", "")
    # Для дефолтного типа: если type_color не передан, берём из настроек типа
    if (not type_id or type_id == DEFAULT_MARKER_TYPE_ID) and not type_color:
        type_color = _get_default_marker_type_color(pdir)
        if not type_id:
            type_id = DEFAULT_MARKER_TYPE_ID
        if not type_title:
            type_title = "Метки"
    marker = {
        "id": uuid.uuid4().hex[:12],
        "frame_index": int(body["frame_index"]),
        "generation_id": body.get("generation_id", ""),
        "view_mode": body.get("view_mode", "original"),
        "x": float(body["x"]),
        "y": float(body["y"]),
        "text": body.get("text", ""),
        "display_mode": body.get("display_mode", "inherit"),
        "pin_show_text_always": bool(body.get("pin_show_text_always", False)),
        "type_id": type_id,
        "type_title": type_title,
        "type_color": type_color,
        "title": body.get("title", ""),  # Заголовок для пометок по умолчанию
        "created": datetime.now().isoformat(),
    }
    if "color" in body and body["color"]:
        marker["color"] = body["color"]
    markers = _load_markers(pdir)
    markers.append(marker)
    _save_markers(pdir, markers)
    return marker


@app.put("/api/projects/{pid}/markers/{mid}")
async def update_marker(pid: str, mid: str, request: Request):
    pdir = _safe_project_dir(pid)
    body = await request.json()
    markers = _load_markers(pdir)
    for m in markers:
        if m["id"] == mid:
            if "text" in body:
                m["text"] = body["text"]
            if "title" in body:
                m["title"] = body["title"]
            if "x" in body:
                m["x"] = float(body["x"])
            if "y" in body:
                m["y"] = float(body["y"])
            if "display_mode" in body:
                m["display_mode"] = body["display_mode"]
            if "pin_show_text_always" in body:
                m["pin_show_text_always"] = bool(body["pin_show_text_always"])
            if "color" in body:
                if body["color"]:
                    m["color"] = body["color"]
                elif "color" in m:
                    del m["color"]
            _save_markers(pdir, markers)
            return m
    raise HTTPException(404, "Marker not found")


@app.delete("/api/projects/{pid}/markers/{mid}")
async def delete_marker(pid: str, mid: str):
    pdir = _safe_project_dir(pid)
    markers = _load_markers(pdir)
    markers = [m for m in markers if m["id"] != mid]
    _save_markers(pdir, markers)
    return {"status": "deleted"}


# ─── Marker Types (per-project) ──────────────────────────────────────────

DEFAULT_MARKER_TYPE_ID = "__default__"

def _load_marker_types_data(pdir: Path) -> dict:
    """Load marker_types.json. Returns { "default": {...}, "marker_types": [...] }."""
    fp = pdir / "marker_types.json"
    if not fp.exists():
        return {"default": {}, "marker_types": []}
    try:
        data = json.loads(fp.read_text(encoding="utf-8"))
        # Legacy: file may be just a list
        if isinstance(data, list):
            return {"default": {}, "marker_types": data}
        return {
            "default": data.get("default", {}),
            "marker_types": data.get("marker_types", []),
        }
    except Exception:
        return {"default": {}, "marker_types": []}


def _save_marker_types_data(pdir: Path, data: dict):
    out = {"marker_types": data["marker_types"]}
    if data.get("default"):
        out["default"] = data["default"]
    (pdir / "marker_types.json").write_text(
        json.dumps(out, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def _normalize_marker_type(t: dict) -> dict:
    t = dict(t)
    if "show_editor_on_create" not in t:
        t["show_editor_on_create"] = True
    return t


def _default_marker_type_obj(default_settings: dict) -> dict:
    return {
        "id": DEFAULT_MARKER_TYPE_ID,
        "title": "Метки",
        "color": default_settings.get("color", "#3fb950"),
        "require_comment": False,
        "show_editor_on_create": default_settings.get("show_editor_on_create", True),
        "allow_instance_color": bool(default_settings.get("allow_instance_color", False)),
        "use_default_point_on_dblclick": default_settings.get("use_default_point_on_dblclick", True),
    }


@app.get("/api/projects/{pid}/marker_types")
async def list_marker_types(pid: str):
    pdir = _safe_project_dir(pid)
    data = _load_marker_types_data(pdir)
    default_obj = _default_marker_type_obj(data["default"])
    custom = [_normalize_marker_type(t) for t in data["marker_types"]]
    return {"marker_types": [default_obj] + custom}


@app.post("/api/projects/{pid}/marker_types")
async def create_marker_type(pid: str, request: Request):
    pdir = _safe_project_dir(pid)
    body = await request.json()
    mt = {
        "id": uuid.uuid4().hex[:12],
        "title": body.get("title", "").strip(),
        "color": body.get("color", "#3fb950"),
        "require_comment": bool(body.get("require_comment", False)),
        "show_editor_on_create": bool(body.get("show_editor_on_create", True)),
    }
    if not mt["title"]:
        raise HTTPException(400, "Title is required")
    data = _load_marker_types_data(pdir)
    data["marker_types"].append(mt)
    _save_marker_types_data(pdir, data)
    return mt


@app.put("/api/projects/{pid}/marker_types/{tid}")
async def update_marker_type(pid: str, tid: str, request: Request):
    pdir = _safe_project_dir(pid)
    body = await request.json()
    if tid == DEFAULT_MARKER_TYPE_ID:
        data = _load_marker_types_data(pdir)
        default_data = data.get("default", {})
        if "show_editor_on_create" in body:
            default_data = {**default_data, "show_editor_on_create": bool(body["show_editor_on_create"])}
        if "color" in body:
            default_data = {**default_data, "color": body["color"]}
        if "allow_instance_color" in body:
            default_data = {**default_data, "allow_instance_color": bool(body["allow_instance_color"])}
        if "use_default_point_on_dblclick" in body:
            default_data = {**default_data, "use_default_point_on_dblclick": bool(body["use_default_point_on_dblclick"])}
        data["default"] = default_data
        _save_marker_types_data(pdir, data)
        return _default_marker_type_obj(data["default"])
    data = _load_marker_types_data(pdir)
    for t in data["marker_types"]:
        if t["id"] == tid:
            if "title" in body:
                t["title"] = body["title"].strip()
            if "color" in body:
                t["color"] = body["color"]
            if "require_comment" in body:
                t["require_comment"] = bool(body["require_comment"])
            if "show_editor_on_create" in body:
                t["show_editor_on_create"] = bool(body["show_editor_on_create"])
            _save_marker_types_data(pdir, data)
            return _normalize_marker_type(t)
    raise HTTPException(404, "Marker type not found")


@app.delete("/api/projects/{pid}/marker_types/{tid}")
async def delete_marker_type(pid: str, tid: str):
    if tid == DEFAULT_MARKER_TYPE_ID:
        raise HTTPException(403, "Cannot delete default marker type")
    pdir = _safe_project_dir(pid)
    data = _load_marker_types_data(pdir)
    data["marker_types"] = [t for t in data["marker_types"] if t["id"] != tid]
    _save_marker_types_data(pdir, data)
    return {"status": "deleted"}


# ─── Zone Markers (rectangular annotations) ──────────────────────────────

def _load_zones(pdir: Path) -> list:
    fp = pdir / "zones.json"
    if not fp.exists():
        return []
    try:
        data = json.loads(fp.read_text(encoding="utf-8"))
        return data.get("zones", [])
    except Exception:
        return []


def _save_zones(pdir: Path, zones: list):
    (pdir / "zones.json").write_text(
        json.dumps({"zones": zones}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


@app.get("/api/projects/{pid}/zones")
async def list_zones(pid: str):
    pdir = _safe_project_dir(pid)
    zones = _load_zones(pdir)
    return {"zones": zones}


@app.post("/api/projects/{pid}/zones")
async def create_zone(pid: str, request: Request):
    pdir = _safe_project_dir(pid)
    body = await request.json()
    type_id = body.get("type_id", "")
    type_title = body.get("type_title", "")
    type_color = body.get("type_color", "")
    if (not type_id or type_id == DEFAULT_ZONE_TYPE_ID) and not type_color:
        type_color = _get_default_zone_type_color(pdir)
        if not type_id:
            type_id = DEFAULT_ZONE_TYPE_ID
        if not type_title:
            type_title = "Зоны"
    zone = {
        "id": uuid.uuid4().hex[:12],
        "frame_index": int(body["frame_index"]),
        "generation_id": body.get("generation_id", ""),
        "view_mode": body.get("view_mode", "original"),
        "x": float(body["x"]),
        "y": float(body["y"]),
        "w": float(body["w"]),
        "h": float(body["h"]),
        "text": body.get("text", ""),
        "display_mode": body.get("display_mode", "inherit"),
        "pin_show_text_always": bool(body.get("pin_show_text_always", False)),
        "type_id": type_id,
        "type_title": type_title,
        "type_color": type_color,
        "title": body.get("title", ""),
        "created": datetime.now().isoformat(),
    }
    if "color" in body and body["color"]:
        zone["color"] = body["color"]
    zones = _load_zones(pdir)
    zones.append(zone)
    _save_zones(pdir, zones)
    return zone


@app.put("/api/projects/{pid}/zones/{zid}")
async def update_zone(pid: str, zid: str, request: Request):
    pdir = _safe_project_dir(pid)
    body = await request.json()
    zones = _load_zones(pdir)
    for z in zones:
        if z["id"] == zid:
            if "text" in body:
                z["text"] = body["text"]
            if "title" in body:
                z["title"] = body["title"]
            if "x" in body:
                z["x"] = float(body["x"])
            if "y" in body:
                z["y"] = float(body["y"])
            if "w" in body:
                z["w"] = float(body["w"])
            if "h" in body:
                z["h"] = float(body["h"])
            if "display_mode" in body:
                z["display_mode"] = body["display_mode"]
            if "pin_show_text_always" in body:
                z["pin_show_text_always"] = bool(body["pin_show_text_always"])
            if "color" in body:
                if body["color"]:
                    z["color"] = body["color"]
                elif "color" in z:
                    del z["color"]
            _save_zones(pdir, zones)
            return z
    raise HTTPException(404, "Zone not found")


@app.delete("/api/projects/{pid}/zones/{zid}")
async def delete_zone(pid: str, zid: str):
    pdir = _safe_project_dir(pid)
    zones = _load_zones(pdir)
    zones = [z for z in zones if z["id"] != zid]
    _save_zones(pdir, zones)
    return {"status": "deleted"}


# ─── Zone Types (per-project) ────────────────────────────────────────────

DEFAULT_ZONE_TYPE_ID = "__default__"

def _load_zone_types_data(pdir: Path) -> dict:
    fp = pdir / "zone_types.json"
    if not fp.exists():
        return {"default": {}, "zone_types": []}
    try:
        data = json.loads(fp.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return {"default": {}, "zone_types": data}
        return {
            "default": data.get("default", {}),
            "zone_types": data.get("zone_types", []),
        }
    except Exception:
        return {"default": {}, "zone_types": []}


def _save_zone_types_data(pdir: Path, data: dict):
    out = {"zone_types": data["zone_types"]}
    if data.get("default"):
        out["default"] = data["default"]
    (pdir / "zone_types.json").write_text(
        json.dumps(out, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def _normalize_zone_type(t: dict) -> dict:
    t = dict(t)
    if "show_editor_on_create" not in t:
        t["show_editor_on_create"] = True
    return t


def _default_zone_type_obj(default_settings: dict) -> dict:
    return {
        "id": DEFAULT_ZONE_TYPE_ID,
        "title": "Зоны",
        "color": default_settings.get("color", "#3fb950"),
        "require_comment": False,
        "show_editor_on_create": default_settings.get("show_editor_on_create", True),
        "allow_instance_color": bool(default_settings.get("allow_instance_color", False)),
    }


def _get_default_zone_type_color(pdir: Path) -> str:
    """Return default zone type color for new default zones."""
    data = _load_zone_types_data(pdir)
    return data.get("default", {}).get("color", "#3fb950")


@app.get("/api/projects/{pid}/zone_types")
async def list_zone_types(pid: str):
    pdir = _safe_project_dir(pid)
    data = _load_zone_types_data(pdir)
    default_obj = _default_zone_type_obj(data["default"])
    custom = [_normalize_zone_type(t) for t in data["zone_types"]]
    return {"zone_types": [default_obj] + custom}


@app.post("/api/projects/{pid}/zone_types")
async def create_zone_type(pid: str, request: Request):
    pdir = _safe_project_dir(pid)
    body = await request.json()
    zt = {
        "id": uuid.uuid4().hex[:12],
        "title": body.get("title", "").strip(),
        "color": body.get("color", "#3fb950"),
        "require_comment": bool(body.get("require_comment", False)),
        "show_editor_on_create": bool(body.get("show_editor_on_create", True)),
    }
    if not zt["title"]:
        raise HTTPException(400, "Title is required")
    data = _load_zone_types_data(pdir)
    data["zone_types"].append(zt)
    _save_zone_types_data(pdir, data)
    return zt


@app.put("/api/projects/{pid}/zone_types/{tid}")
async def update_zone_type(pid: str, tid: str, request: Request):
    pdir = _safe_project_dir(pid)
    body = await request.json()
    if tid == DEFAULT_ZONE_TYPE_ID:
        data = _load_zone_types_data(pdir)
        default_data = data.get("default", {})
        if "show_editor_on_create" in body:
            default_data = {**default_data, "show_editor_on_create": bool(body["show_editor_on_create"])}
        if "color" in body:
            default_data = {**default_data, "color": body["color"]}
        if "allow_instance_color" in body:
            default_data = {**default_data, "allow_instance_color": bool(body["allow_instance_color"])}
        data["default"] = default_data
        _save_zone_types_data(pdir, data)
        return _default_zone_type_obj(data["default"])
    data = _load_zone_types_data(pdir)
    for t in data["zone_types"]:
        if t["id"] == tid:
            if "title" in body:
                t["title"] = body["title"].strip()
            if "color" in body:
                t["color"] = body["color"]
            if "require_comment" in body:
                t["require_comment"] = bool(body["require_comment"])
            if "show_editor_on_create" in body:
                t["show_editor_on_create"] = bool(body["show_editor_on_create"])
            _save_zone_types_data(pdir, data)
            return _normalize_zone_type(t)
    raise HTTPException(404, "Zone type not found")


@app.delete("/api/projects/{pid}/zone_types/{tid}")
async def delete_zone_type(pid: str, tid: str):
    if tid == DEFAULT_ZONE_TYPE_ID:
        raise HTTPException(403, "Cannot delete default zone type")
    pdir = _safe_project_dir(pid)
    data = _load_zone_types_data(pdir)
    data["zone_types"] = [t for t in data["zone_types"] if t["id"] != tid]
    _save_zone_types_data(pdir, data)
    return {"status": "deleted"}


# Legacy annotation endpoints (backward compatibility for old projects)
@app.get("/api/projects/{pid}/annotations")
async def list_annotations(pid: str):
    p = PROJECTS_DIR / pid / "annotations_index.json"
    if not p.exists():
        return {"annotations": []}
    return json.loads(p.read_text(encoding="utf-8"))

# ─── Export ZIP ──────────────────────────────────────────────────────────

@app.get("/api/projects/{pid}/export")
async def export_zip(pid: str):
    pdir = PROJECTS_DIR / pid
    if not pdir.exists():
        raise HTTPException(404)

    # Load project metadata for export bundle
    meta: dict = {}
    try:
        meta = ensure_metadata(pdir)
    except Exception:
        pass

    zip_path = pdir / f"export_{pid}.zip"

    with zipfile.ZipFile(str(zip_path), "w", zipfile.ZIP_DEFLATED) as zf:
        # ── frames ──
        fd = pdir / "frames"
        if fd.exists():
            for f in sorted(fd.iterdir()):
                if f.suffix == ".jpg":
                    zf.write(str(f), f"frames/{f.name}")

        # ── thumbs ──
        td = pdir / "thumbs"
        if td.exists():
            for f in sorted(td.iterdir()):
                if f.suffix == ".jpg":
                    zf.write(str(f), f"thumbs/{f.name}")

        # ── annotations ──
        ad = pdir / "annotations"
        if ad.exists():
            for f in sorted(ad.iterdir()):
                zf.write(str(f), f"annotations/{f.name}")

        # ── centered frames ──
        cfd = pdir / "centered_frames"
        if cfd.exists():
            for f in sorted(cfd.iterdir()):
                if f.suffix == ".jpg":
                    zf.write(str(f), f"centered_frames/{f.name}")

        # ── centered annotations ──
        cad = pdir / "annotations_centered"
        if cad.exists():
            for f in sorted(cad.iterdir()):
                zf.write(str(f), f"annotations_centered/{f.name}")

        # ── markers (point annotations) ──
        markers_fp = pdir / "markers.json"
        if markers_fp.exists():
            zf.write(str(markers_fp), "markers.json")

        # ── marker types ──
        mt_fp = pdir / "marker_types.json"
        if mt_fp.exists():
            zf.write(str(mt_fp), "marker_types.json")

        # ── index / data files ──
        for name in ("index.json", "annotations_index.json",
                      "centered_index.json", "annotations_centered_index.json",
                      "roi.json"):
            fp = pdir / name
            if fp.exists():
                zf.write(str(fp), name)

        # ── project_metadata.json — full project settings bundle ──
        project_metadata = {
            "project_id": meta.get("project_id", pid),
            "created_at": meta.get("created_at"),
            "original_filename": meta.get("original_filename"),
            "project_name": meta.get("project_name"),
            "duration_sec": meta.get("duration_sec"),
            "resolution": meta.get("resolution"),
            "fps": meta.get("fps"),
            "frames_count": int(meta.get("frames_count") or 0),
            "frames_status": meta.get("frames_status", "not_started"),
            "frames_expected": int(meta.get("frames_expected") or 0),
            "frames_generated": int(meta.get("frames_generated") or 0),
            "frames_settings_hash": meta.get("frames_settings_hash"),
            "frames_generated_at": meta.get("frames_generated_at"),
            "settings": meta.get("settings") or {},
            "clip_start": meta.get("clip_start"),
            "clip_end": meta.get("clip_end"),
            "density_frames": meta.get("density_frames"),
            "user_quality": meta.get("user_quality"),
            "autotune_enabled": meta.get("autotune_enabled"),
            "file_size_bytes": meta.get("file_size_bytes"),
            "total_frames_video": meta.get("total_frames_video"),
            # viewer settings
            "max_sensitivity_project": meta.get("max_sensitivity_project"),
            "current_sensitivity": meta.get("current_sensitivity"),
            "markers_show_always": meta.get("markers_show_always", False),
        }
        zf.writestr(
            "project_metadata.json",
            json.dumps(project_metadata, indent=2, ensure_ascii=False),
        )

        # ── viewer_metadata.json — viewer-step parameters ──
        viewer_meta: dict = {}
        index_fp = pdir / "index.json"
        if index_fp.exists():
            try:
                idx_data = json.loads(index_fp.read_text(encoding="utf-8"))
                viewer_meta["start"] = idx_data.get("start")
                viewer_meta["end"] = idx_data.get("end")
                viewer_meta["duration"] = idx_data.get("duration")
                viewer_meta["num_frames"] = idx_data.get("num_frames")
                viewer_meta["time_step"] = idx_data.get("time_step")
                viewer_meta["fps"] = idx_data.get("fps")
                viewer_meta["user_quality"] = idx_data.get("user_quality")
                viewer_meta["ffmpeg_qscale"] = idx_data.get("ffmpeg_qscale")
            except Exception:
                pass
        roi_fp = pdir / "roi.json"
        if roi_fp.exists():
            try:
                viewer_meta["roi"] = json.loads(roi_fp.read_text(encoding="utf-8"))
            except Exception:
                pass
        ci_fp = pdir / "centered_index.json"
        if ci_fp.exists():
            try:
                ci = json.loads(ci_fp.read_text(encoding="utf-8"))
                viewer_meta["tracker_type"] = ci.get("tracker_type")
                viewer_meta["stabilized"] = ci.get("stabilized")
            except Exception:
                pass
        zf.writestr(
            "viewer_metadata.json",
            json.dumps(viewer_meta, indent=2, ensure_ascii=False),
        )

    return FileResponse(str(zip_path), media_type="application/zip",
                        filename=f"project_{pid}.zip")


# ─── Advanced Export ZIP (modal) ─────────────────────────────────────────

def _draw_markers_on_frame(img: "np.ndarray", markers: list,
                           markers_show_always: bool,
                           mode: str = "labels_comments") -> "np.ndarray":
    """
    Draw marker dots and optionally labels/comments on a copy of the image.

    mode:
      "points"           – circles only (no text at all)
      "labels"           – circles + type/title label
      "labels_comments"  – circles + label + comment (if non-empty)

    Uses OpenCV for circles and Pillow (PIL) for Unicode text rendering
    so that Cyrillic and other non-ASCII characters display correctly.
    """
    out = img.copy()
    h, w = out.shape[:2]

    draw_labels = mode in ("labels", "labels_comments")
    draw_comments = mode == "labels_comments"

    # Font size proportional to image
    font_size = max(12, int(min(w, h) * 0.022))
    num_font_size = max(9, int(min(w, h) * 0.014))
    pil_font = _get_font(font_size) if (_HAS_PIL and draw_labels) else None
    pil_num_font = _get_font(num_font_size) if _HAS_PIL else None

    # #region agent log
    import json as _json_dbg; open(r"d:\projects\3Dps\.cursor\debug.log", "a", encoding="utf-8").write(_json_dbg.dumps({"location":"main.py:_draw_markers_on_frame:entry","message":"_draw_markers_on_frame called","data":{"mode":mode,"markers_count":len(markers),"img_shape":[h,w],"_HAS_PIL":_HAS_PIL,"draw_labels":draw_labels,"draw_comments":draw_comments,"font_size":font_size,"num_font_size":num_font_size,"pil_font_is_none":pil_font is None,"pil_num_font_is_none":pil_num_font is None},"hypothesisId":"A,B,E","timestamp":__import__('time').time()},ensure_ascii=False)+"\n")
    # #endregion

    # Draw circles with OpenCV (better anti-aliasing for shapes)
    for idx, m in enumerate(markers):
        cx = int(m["x"] * w)
        cy = int(m["y"] * h)
        color_hex = (m.get("type_color") or "#58a6ff").lstrip("#")
        if len(color_hex) == 6:
            cr, cg, cb = int(color_hex[0:2], 16), int(color_hex[2:4], 16), int(color_hex[4:6], 16)
        else:
            cr, cg, cb = 88, 166, 255
        bgr = (cb, cg, cr)

        radius = max(8, int(min(w, h) * 0.012))
        cv2.circle(out, (cx, cy), radius, bgr, -1, cv2.LINE_AA)
        cv2.circle(out, (cx, cy), radius, (255, 255, 255), 1, cv2.LINE_AA)

    # #region agent log
    import json as _json_dbg; open(r"d:\projects\3Dps\.cursor\debug.log", "a", encoding="utf-8").write(_json_dbg.dumps({"location":"main.py:_draw_markers_on_frame:before_pil_block","message":"About to enter PIL block","data":{"_HAS_PIL":_HAS_PIL,"pil_num_font_is_none":pil_num_font is None,"will_enter_pil_block":(_HAS_PIL and pil_num_font is not None)},"hypothesisId":"A,B","timestamp":__import__('time').time()},ensure_ascii=False)+"\n")
    # #endregion

    # Convert to PIL for text rendering (number inside dots + optional labels)
    if _HAS_PIL and pil_num_font is not None:
        pil_img = Image.fromarray(cv2.cvtColor(out, cv2.COLOR_BGR2RGB))
        draw = ImageDraw.Draw(pil_img)

        # #region agent log
        import json as _json_dbg; open(r"d:\projects\3Dps\.cursor\debug.log", "a", encoding="utf-8").write(_json_dbg.dumps({"location":"main.py:_draw_markers_on_frame:inside_pil_block","message":"PIL block entered, drawing markers","data":{"markers_sample":[{"id":m.get("id","?"),"title":m.get("title",""),"text":m.get("text",""),"type_id":m.get("type_id",""),"type_title":m.get("type_title",""),"frame_index":m.get("frame_index")} for m in markers[:5]],"draw_labels":draw_labels,"draw_comments":draw_comments},"hypothesisId":"C,D","timestamp":__import__('time').time()},ensure_ascii=False)+"\n")
        # #endregion

        for idx, m in enumerate(markers):
            cx = int(m["x"] * w)
            cy = int(m["y"] * h)
            color_hex = (m.get("type_color") or "#58a6ff").lstrip("#")
            if len(color_hex) == 6:
                cr, cg, cb = int(color_hex[0:2], 16), int(color_hex[2:4], 16), int(color_hex[4:6], 16)
            else:
                cr, cg, cb = 88, 166, 255
            rgb = (cr, cg, cb)

            radius = max(8, int(min(w, h) * 0.012))

            # Number inside dot
            num_text = str(idx + 1)
            try:
                nb = draw.textbbox((0, 0), num_text, font=pil_num_font)
                ntw, nth = nb[2] - nb[0], nb[3] - nb[1]
            except Exception:
                ntw, nth = num_font_size // 2, num_font_size
            draw.text((cx - ntw // 2, cy - nth // 2), num_text,
                      fill=(255, 255, 255), font=pil_num_font)

            # Skip label/comment drawing in points-only mode
            if not draw_labels or pil_font is None:
                # #region agent log
                import json as _json_dbg; open(r"d:\projects\3Dps\.cursor\debug.log", "a", encoding="utf-8").write(_json_dbg.dumps({"location":"main.py:_draw_markers_on_frame:skip_labels","message":"Skipping label drawing","data":{"draw_labels":draw_labels,"pil_font_is_none":pil_font is None,"marker_id":m.get("id","?")},"hypothesisId":"A,B,E","timestamp":__import__('time').time()},ensure_ascii=False)+"\n")
                # #endregion
                continue

            # #region agent log
            import json as _json_dbg; open(r"d:\projects\3Dps\.cursor\debug.log", "a", encoding="utf-8").write(_json_dbg.dumps({"location":"main.py:_draw_markers_on_frame:drawing_label","message":"Drawing label for marker","data":{"marker_id":m.get("id","?"),"title":m.get("title",""),"text":m.get("text",""),"type_title":m.get("type_title","")},"hypothesisId":"C,D","timestamp":__import__('time').time()},ensure_ascii=False)+"\n")
            # #endregion

            # Determine label text
            has_type = bool(m.get("type_id") or m.get("type_title"))
            raw_comment = (m.get("text") or "").strip()

            # Build badge text (always shown in labels mode)
            badge_text = ""
            if has_type:
                badge_text = m.get("type_title") or "(тип удалён)"
            else:
                badge_text = m.get("title") or "Метка"

            # Comment: only in labels_comments mode and only if non-empty
            comment_text = ""
            if draw_comments and raw_comment:
                comment_text = raw_comment

            # --- Draw badge (type name or title) ---
            pad = 4
            gap = 2
            bx = cx + radius + 4
            by = cy - font_size // 2

            try:
                bb = draw.textbbox((0, 0), badge_text, font=pil_font)
                btw, bth = bb[2] - bb[0], bb[3] - bb[1]
            except Exception:
                btw, bth = len(badge_text) * font_size // 2, font_size

            # Clamp horizontally
            if bx + btw + pad * 2 > w:
                bx = cx - radius - 4 - btw - pad * 2
            if bx < 0:
                bx = pad
            # Clamp vertically
            if by - pad < 0:
                by = pad
            if by + bth + pad > h:
                by = h - bth - pad

            # Badge background
            draw.rectangle(
                [(bx - pad, by - pad), (bx + btw + pad, by + bth + pad)],
                fill=rgb,
            )
            draw.text((bx, by), badge_text, fill=(255, 255, 255), font=pil_font)

            # --- Draw comment below badge (if present) ---
            if comment_text:
                cmt_y = by + bth + pad + gap
                try:
                    cb_box = draw.textbbox((0, 0), comment_text, font=pil_font)
                    ctw, cth = cb_box[2] - cb_box[0], cb_box[3] - cb_box[1]
                except Exception:
                    ctw, cth = len(comment_text) * font_size // 2, font_size

                cmt_x = bx
                if cmt_x + ctw + pad * 2 > w:
                    cmt_x = w - ctw - pad * 2
                if cmt_x < 0:
                    cmt_x = pad
                if cmt_y + cth + pad > h:
                    cmt_y = h - cth - pad

                draw.rectangle(
                    [(cmt_x - pad, cmt_y - pad), (cmt_x + ctw + pad, cmt_y + cth + pad)],
                    fill=(0, 0, 0),
                )
                draw.rectangle(
                    [(cmt_x - pad, cmt_y - pad), (cmt_x + ctw + pad, cmt_y + cth + pad)],
                    outline=rgb, width=1,
                )
                draw.text((cmt_x, cmt_y), comment_text, fill=(255, 255, 255), font=pil_font)

        # Convert back to OpenCV BGR
        out = cv2.cvtColor(np.array(pil_img), cv2.COLOR_RGB2BGR)

    return out


@app.post("/api/projects/{pid}/export_advanced")
async def export_advanced(pid: str, payload: dict = Body(...)):
    """
    Advanced export with options:
      include_before          – original frames → frames_before/
      include_points          – frames with dots only → frames_points/
      include_labels          – frames with dots + type labels → frames_labels/
      include_labels_comments – frames with dots + labels + comments → frames_labels_comments/
      include_project_settings – project_settings.json
      include_annotations     – annotations.json
      paired_export           – if true, each annotated variant also gets a before copy
      frames_mode             – "annotated_only" | "all"
      jpeg_quality            – 10..100
    """
    pdir = PROJECTS_DIR / pid
    if not pdir.exists():
        raise HTTPException(404, "Project not found")

    include_before = payload.get("include_before", False)
    include_points = payload.get("include_points", False)
    include_labels = payload.get("include_labels", False)
    include_labels_comments = payload.get("include_labels_comments", False)
    include_project_settings = payload.get("include_project_settings", False)
    include_annotations = payload.get("include_annotations", False)
    paired_export = payload.get("paired_export", False)
    frames_mode = payload.get("frames_mode", "annotated_only")
    jpeg_quality = max(10, min(100, int(payload.get("jpeg_quality", 90))))

    meta: dict = {}
    try:
        meta = ensure_metadata(pdir)
    except Exception:
        pass

    # Load index for frame list
    index_data: dict = {}
    index_fp = pdir / "index.json"
    if index_fp.exists():
        try:
            index_data = json.loads(index_fp.read_text(encoding="utf-8"))
        except Exception:
            pass

    frames_list = index_data.get("frames", [])
    generation_id = index_data.get("generation_id") or meta.get("generation_id") or ""

    # Load markers
    markers_all: list = []
    markers_fp = pdir / "markers.json"
    if markers_fp.exists():
        try:
            markers_all = json.loads(markers_fp.read_text(encoding="utf-8")).get("markers", [])
        except Exception:
            pass

    # Filter markers for the current generation
    markers = [m for m in markers_all
               if m.get("generation_id") == generation_id and m.get("view_mode", "original") == "original"]

    # Build set of annotated frame indices
    annotated_indices = set()
    for m in markers:
        annotated_indices.add(m.get("frame_index"))

    # Load marker types
    marker_types: list = []
    mt_fp = pdir / "marker_types.json"
    if mt_fp.exists():
        try:
            marker_types = json.loads(mt_fp.read_text(encoding="utf-8")).get("marker_types", [])
        except Exception:
            pass

    # Determine which annotated-frame variants to render
    # Each entry: (folder_name, file_suffix, draw_mode)
    render_variants: list = []
    if include_points:
        render_variants.append(("frames_points", "_points", "points"))
    if include_labels:
        render_variants.append(("frames_labels", "_labels", "labels"))
    if include_labels_comments:
        render_variants.append(("frames_labels_comments", "_labels_comments", "labels_comments"))

    # Whether we need to iterate frames at all
    need_frames = include_before or bool(render_variants)

    # #region agent log
    import json as _json_dbg; open(r"d:\projects\3Dps\.cursor\debug.log", "a", encoding="utf-8").write(_json_dbg.dumps({"location":"main.py:export_advanced:setup","message":"Export advanced setup","data":{"render_variants":render_variants,"need_frames":need_frames,"markers_count":len(markers),"annotated_indices":sorted(list(annotated_indices)),"_HAS_PIL":_HAS_PIL,"_HAS_CV2":_HAS_CV2,"generation_id":generation_id},"hypothesisId":"C,E","timestamp":__import__('time').time()},ensure_ascii=False)+"\n")
    # #endregion

    zip_path = pdir / f"export_adv_{pid}.zip"

    with zipfile.ZipFile(str(zip_path), "w", zipfile.ZIP_DEFLATED) as zf:

        # ─────────────────────────────────────────────────────
        # 1. Frame images (before + annotated variants)
        # ─────────────────────────────────────────────────────
        if need_frames and _HAS_CV2:
            frames_dir = pdir / "frames"
            encode_params = [cv2.IMWRITE_JPEG_QUALITY, jpeg_quality]

            for frame_info in frames_list:
                fidx = frame_info["index"]
                fname = frame_info["filename"]
                fpath = frames_dir / fname
                if not fpath.exists():
                    continue

                has_markers = fidx in annotated_indices

                # In annotated_only mode skip frames without markers
                # (but still include before frames if paired_export is off
                #  and include_before is on)
                skip_annotated = (frames_mode == "annotated_only" and not has_markers)

                base_name = f"frame_{fidx+1:06d}"

                # Read original image (lazily – only if needed)
                img = None

                # --- Before (original) frames ---
                if include_before:
                    # In paired mode: only export before if there are annotated variants
                    if paired_export and skip_annotated:
                        pass  # skip before when no annotated counterpart
                    else:
                        if img is None:
                            img = cv2.imread(str(fpath))
                        if img is not None:
                            ok, buf = cv2.imencode(".jpg", img, encode_params)
                            if ok:
                                zf.writestr(
                                    f"frames_before/{base_name}_before.jpg",
                                    buf.tobytes(),
                                )

                # --- Annotated variants ---
                if not skip_annotated and render_variants:
                    if img is None:
                        img = cv2.imread(str(fpath))
                    if img is not None:
                        frame_markers = [m for m in markers if m.get("frame_index") == fidx]

                        # If paired_export and before not already written, write it once
                        if paired_export and not include_before:
                            ok_b, buf_b = cv2.imencode(".jpg", img, encode_params)
                            if ok_b:
                                zf.writestr(
                                    f"frames_before/{base_name}_before.jpg",
                                    buf_b.tobytes(),
                                )

                        for folder, suffix, draw_mode in render_variants:
                            if has_markers and frame_markers:
                                drawn = _draw_markers_on_frame(
                                    img, frame_markers,
                                    markers_show_always=False,
                                    mode=draw_mode,
                                )
                            else:
                                drawn = img
                            ok, buf = cv2.imencode(".jpg", drawn, encode_params)
                            if ok:
                                zf.writestr(
                                    f"{folder}/{base_name}{suffix}.jpg",
                                    buf.tobytes(),
                                )

        # ─────────────────────────────────────────────────────
        # 2. Project settings JSON
        # ─────────────────────────────────────────────────────
        if include_project_settings:
            proj_settings: dict = {
                "project_id": meta.get("project_id", pid),
                "project_name": meta.get("project_name"),
                "created_at": meta.get("created_at"),
                "original_filename": meta.get("original_filename"),
                "duration_sec": meta.get("duration_sec"),
                "resolution": meta.get("resolution"),
                "fps": meta.get("fps"),
                "frames_count": int(meta.get("frames_count") or 0),
                "viewer": {
                    "current_sensitivity": meta.get("current_sensitivity"),
                    "max_sensitivity_project": meta.get("max_sensitivity_project"),
                    "markers_show_always": meta.get("markers_show_always", False),
                    "infinite_rotation": meta.get("infinite_rotation", False),
                    "auto_rotate": meta.get("auto_rotate", False),
                },
                "frames": {
                    "clip_start": meta.get("clip_start"),
                    "clip_end": meta.get("clip_end"),
                    "density_frames": meta.get("density_frames"),
                    "user_quality": meta.get("user_quality"),
                    "autotune_enabled": meta.get("autotune_enabled"),
                    "frame_selection_mode": meta.get("frame_selection_mode"),
                    "settings": meta.get("settings") or {},
                },
                "marker_types": marker_types,
            }
            if index_data:
                proj_settings["index"] = {
                    "generation_id": index_data.get("generation_id"),
                    "start": index_data.get("start"),
                    "end": index_data.get("end"),
                    "duration": index_data.get("duration"),
                    "num_frames": index_data.get("num_frames"),
                    "time_step": index_data.get("time_step"),
                    "fps": index_data.get("fps"),
                    "user_quality": index_data.get("user_quality"),
                    "ffmpeg_qscale": index_data.get("ffmpeg_qscale"),
                    "frame_selection_mode": index_data.get("frame_selection_mode"),
                }
            zf.writestr("project_settings.json",
                         json.dumps(proj_settings, indent=2, ensure_ascii=False))

        # ─────────────────────────────────────────────────────
        # 3. Annotations JSON
        # ─────────────────────────────────────────────────────
        if include_annotations:
            frames_annotations: list = []
            for frame_info in frames_list:
                fidx = frame_info["index"]
                frame_markers = [m for m in markers if m.get("frame_index") == fidx]
                if not frame_markers:
                    continue
                annots = []
                for m in frame_markers:
                    has_type = bool(m.get("type_id") or m.get("type_title"))
                    annot: dict = {
                        "id": m.get("id"),
                        "x": m.get("x"),
                        "y": m.get("y"),
                        "type_id": m.get("type_id", ""),
                        "type_title": m.get("type_title", ""),
                        "type_color": m.get("type_color", ""),
                        "comment": (m.get("text") or "").strip(),
                        "pin_show_text_always": m.get("pin_show_text_always", False),
                        "display_mode": m.get("display_mode", "inherit"),
                    }
                    if not has_type:
                        annot["title"] = m.get("title") or "Метка"
                    annots.append(annot)
                frames_annotations.append({
                    "frame_index": fidx,
                    "filename": frame_info.get("filename"),
                    "canonical_name": f"frame_{fidx+1:06d}.jpg",
                    "timecode": frame_info.get("timecode"),
                    "timestamp": frame_info.get("timestamp"),
                    "markers": annots,
                })

            annotations_export = {
                "project_id": pid,
                "generation_id": generation_id,
                "total_frames": len(frames_list),
                "annotated_frames_count": len(frames_annotations),
                "total_markers": len(markers),
                "marker_types": marker_types,
                "frames": frames_annotations,
            }
            zf.writestr("annotations.json",
                         json.dumps(annotations_export, indent=2, ensure_ascii=False))

    return FileResponse(str(zip_path), media_type="application/zip",
                        filename=f"project_{pid}.zip")


# ─── Import ZIP ──────────────────────────────────────────────────────────

@app.post("/api/import-zip")
async def import_zip(file: UploadFile = File(...)):
    """
    Import a ZIP project archive exported by this app.
    Creates a new project in read_only mode (no original video).
    """
    if not file.filename or not file.filename.lower().endswith(".zip"):
        raise HTTPException(400, "Файл должен быть в формате ZIP")

    tmp_zip = PROJECTS_DIR / f"_import_tmp_{uuid.uuid4().hex[:8]}.zip"
    try:
        tmp_zip.write_bytes(await file.read())
    except Exception as exc:
        raise HTTPException(500, f"Не удалось сохранить файл: {exc}")

    pdir: Optional[Path] = None
    try:
        with zipfile.ZipFile(str(tmp_zip), "r") as zf:
            names = zf.namelist()

            has_frames = any(
                n.startswith("frames/") and n.endswith(".jpg") for n in names
            )
            has_project_meta = "project_metadata.json" in names
            has_index = "index.json" in names

            if not has_frames:
                raise HTTPException(400, "ZIP не содержит кадров (папка frames/)")
            if not has_project_meta and not has_index:
                raise HTTPException(
                    400,
                    "ZIP не содержит метаданных проекта "
                    "(project_metadata.json или index.json)",
                )

            pid = uuid.uuid4().hex[:8]
            pdir = PROJECTS_DIR / pid
            pdir.mkdir(parents=True)
            zf.extractall(str(pdir))

        # ── Build project.json & metadata.json from imported data ──
        project_meta: dict = {}
        pm_fp = pdir / "project_metadata.json"
        if pm_fp.exists():
            try:
                project_meta = json.loads(pm_fp.read_text(encoding="utf-8"))
            except Exception:
                pass

        index_data: dict = {}
        idx_fp = pdir / "index.json"
        if idx_fp.exists():
            try:
                index_data = json.loads(idx_fp.read_text(encoding="utf-8"))
            except Exception:
                pass

        now_iso = datetime.now().isoformat()
        original_filename = (
            project_meta.get("original_filename") or file.filename or "imported.zip"
        )
        project_name = project_meta.get("project_name") or original_filename
        frames_count = int(
            project_meta.get("frames_count")
            or index_data.get("num_frames")
            or 0
        )
        if frames_count == 0:
            fd = pdir / "frames"
            if fd.exists():
                frames_count = sum(1 for f in fd.glob("frame_*.jpg"))

        resolution = project_meta.get("resolution")
        fps_val = project_meta.get("fps") or index_data.get("fps")
        duration_sec = project_meta.get("duration_sec") or index_data.get("duration")

        project_json: dict = {
            "id": pid,
            "original_filename": original_filename,
            "video_file": "",
            "duration": duration_sec,
            "width": None,
            "height": None,
            "fps": fps_val,
            "created": now_iso,
            "read_only": True,
            "imported_from_zip": True,
        }
        if resolution:
            try:
                w_s, h_s = resolution.split("x")
                project_json["width"] = int(w_s)
                project_json["height"] = int(h_s)
            except Exception:
                pass

        (pdir / "project.json").write_text(
            json.dumps(project_json, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        imported_meta = {
            "project_id": pid,
            "created_at": now_iso,
            "original_filename": original_filename,
            "project_name": project_name,
            "duration_sec": duration_sec,
            "resolution": resolution,
            "fps": fps_val,
            "frames_count": frames_count,
            "frames_status": "completed" if frames_count > 0 else "not_started",
            "frames_expected": frames_count,
            "frames_generated": frames_count,
            "frames_settings_hash": project_meta.get("frames_settings_hash"),
            "frames_generated_at": project_meta.get("frames_generated_at") or now_iso,
            "read_only": True,
            # viewer settings
            "max_sensitivity_project": project_meta.get("max_sensitivity_project"),
            "current_sensitivity": project_meta.get("current_sensitivity"),
            "markers_show_always": project_meta.get("markers_show_always", False),
            "imported_from_zip": True,
            "settings": project_meta.get("settings") or {},
            "clip_start": project_meta.get("clip_start"),
            "clip_end": project_meta.get("clip_end"),
            "density_frames": project_meta.get("density_frames"),
            "user_quality": project_meta.get("user_quality"),
            "autotune_enabled": project_meta.get("autotune_enabled"),
            "file_size_bytes": project_meta.get("file_size_bytes"),
            "total_frames_video": project_meta.get("total_frames_video"),
        }
        _metadata_path(pdir).write_text(
            json.dumps(imported_meta, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        # Update index.json project_id to new pid
        if idx_fp.exists():
            try:
                idx = json.loads(idx_fp.read_text(encoding="utf-8"))
                idx["project_id"] = pid
                idx_fp.write_text(
                    json.dumps(idx, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
            except Exception:
                pass

        return {
            "status": "ok",
            "project_id": pid,
            "project_name": project_name,
            "frames_count": frames_count,
            "read_only": True,
        }

    except HTTPException:
        raise
    except Exception as exc:
        if pdir is not None and pdir.exists():
            shutil.rmtree(str(pdir), ignore_errors=True)
        raise HTTPException(500, f"Ошибка импорта ZIP: {exc}")
    finally:
        if tmp_zip.exists():
            tmp_zip.unlink(missing_ok=True)

# ─── ROI (Region Of Interest) ─────────────────────────────────────────

@app.post("/api/projects/{pid}/roi")
async def save_roi(pid: str, roi: dict = Body(...)):
    pdir = PROJECTS_DIR / pid
    if not pdir.exists():
        raise HTTPException(404, "Project not found")
    data = {
        "frame_id": int(roi.get("frame_id", 0)),
        "x": int(roi["x"]),
        "y": int(roi["y"]),
        "w": int(roi["w"]),
        "h": int(roi["h"]),
        "created_at": datetime.now().isoformat(),
    }
    (pdir / "roi.json").write_text(json.dumps(data, indent=2), encoding="utf-8")
    return data


@app.get("/api/projects/{pid}/roi")
async def get_roi(pid: str):
    p = PROJECTS_DIR / pid / "roi.json"
    if not p.exists():
        return {"status": "not_set"}
    return json.loads(p.read_text(encoding="utf-8"))


# ─── Centering: tracker + warpAffine ─────────────────────────────────

def _require_cv2():
    """Raise if OpenCV is not available."""
    if not _HAS_CV2:
        raise HTTPException(
            500,
            "OpenCV (cv2) is not installed. "
            "Run: pip install opencv-contrib-python"
        )

def _create_tracker(tracker_type: str):
    """Create an OpenCV tracker instance (compatible with multiple cv2 versions)."""
    _require_cv2()
    t = tracker_type.upper()
    if t == "MOSSE":
        # Try different API versions
        if hasattr(cv2, 'TrackerMOSSE_create'):
            return cv2.TrackerMOSSE_create()
        if hasattr(cv2, 'legacy') and hasattr(cv2.legacy, 'TrackerMOSSE_create'):
            return cv2.legacy.TrackerMOSSE_create()
        raise RuntimeError("MOSSE tracker not available in this OpenCV build")
    # default: CSRT
    if hasattr(cv2, 'TrackerCSRT_create'):
        return cv2.TrackerCSRT_create()
    if hasattr(cv2, 'legacy') and hasattr(cv2.legacy, 'TrackerCSRT_create'):
        return cv2.legacy.TrackerCSRT_create()
    raise RuntimeError(
        "CSRT tracker not available. Install opencv-contrib-python: "
        "pip install opencv-contrib-python"
    )


def _stabilize_frames_vidstab(pdir: Path, frames_dir: Path, num_frames: int) -> Path:
    """
    Run FFmpeg vidstab on the frames: assemble tmp video,
    vidstabdetect + vidstabtransform, then re-extract stabilized frames.
    Returns path to stabilized_frames directory.
    """
    stab_dir = pdir / "stabilized_frames"
    stab_dir.mkdir(exist_ok=True)
    tmp_dir = pdir / "_stab_tmp"
    tmp_dir.mkdir(exist_ok=True)

    # Absolute paths used by Python code
    tmp_video = str(tmp_dir / "assembled.mp4")
    stab_video = str(tmp_dir / "stabilized.mp4")
    transforms_file = str(tmp_dir / "transforms.trf")

    # Local names used by FFmpeg when cwd=tmp_dir (no drive letter / colons)
    tmp_video_local = "assembled.mp4"
    stab_video_local = "stabilized.mp4"
    transforms_file_local = "transforms.trf"

    # Assemble frames into temporary video (30 fps, visually lossless)
    input_pattern = str(frames_dir / "frame_%05d.jpg")
    _run([
        ffmpeg(), "-y",
        "-framerate", "30",
        "-i", input_pattern,
        "-c:v", "libx264", "-crf", "10", "-pix_fmt", "yuv420p",
        tmp_video,
    ])

    # vidstabdetect: analyse motion and write transforms.trf in tmp_dir
    _vf_detect = (
        f"vidstabdetect=stepsize=6:shakiness=5:accuracy=15:result={transforms_file_local}"
    )
    subprocess.run(
        [ffmpeg(), "-y", "-i", tmp_video_local, "-vf", _vf_detect, "-f", "null", "-"],
        capture_output=True,
        text=True,
        check=True,
        creationflags=_CF,
        cwd=str(tmp_dir),
    )

    # vidstabtransform: apply stabilization and save stabilized.mp4 in tmp_dir
    _vf_transform = (
        f"vidstabtransform=input={transforms_file_local}:smoothing=10:crop=black:zoom=0"
    )
    subprocess.run(
        [
            ffmpeg(), "-y",
            "-i", tmp_video_local,
            "-vf", _vf_transform,
            "-c:v", "libx264", "-crf", "10", "-pix_fmt", "yuv420p",
            stab_video_local,
        ],
        capture_output=True,
        text=True,
        check=True,
        creationflags=_CF,
        cwd=str(tmp_dir),
    )

    # Re-extract frames into stabilized_frames; ensure numbering starts at 0
    _run([
        ffmpeg(), "-y",
        "-i", stab_video,
        "-q:v", "2",
        "-start_number", "0",
        str(stab_dir / "frame_%05d.jpg"),
    ])

    # cleanup tmp
    shutil.rmtree(str(tmp_dir), ignore_errors=True)
    return stab_dir


def _generate_centered_task(
    pid: str,
    tracker_type: str,
    stabilize: bool,
):
    """Background task: track object and generate centered frames."""
    pdir = PROJECTS_DIR / pid
    index_fp = pdir / "index.json"
    if not index_fp.exists():
        centering_progress[pid] = {"status": "error: index.json not found"}
        return

    roi_fp = pdir / "roi.json"
    if not roi_fp.exists():
        centering_progress[pid] = {"status": "error: roi.json not found"}
        return

    index_data = json.loads(index_fp.read_text(encoding="utf-8"))
    roi_data = json.loads(roi_fp.read_text(encoding="utf-8"))
    frames_list = index_data["frames"]
    num_frames = len(frames_list)

    centering_progress[pid] = {"total": num_frames, "done": 0, "status": "processing"}

    # Determine source frames directory
    source_frames_dir = pdir / "frames"

    # Optional stabilization
    if stabilize:
        try:
            centering_progress[pid]["status"] = "stabilizing"
            source_frames_dir = _stabilize_frames_vidstab(pdir, source_frames_dir, num_frames)
        except Exception as e:
            centering_progress[pid] = {"status": f"error: stabilization failed: {e}"}
            return

    centering_progress[pid]["status"] = "tracking"

    # Output directory
    centered_dir = pdir / "centered_frames"
    centered_dir.mkdir(exist_ok=True)

    # ROI as (x, y, w, h)
    roi_bbox = (roi_data["x"], roi_data["y"], roi_data["w"], roi_data["h"])

    # Read first frame and initialize tracker
    first_frame_path = str(source_frames_dir / frames_list[0]["filename"])
    first_frame = cv2.imread(first_frame_path)
    if first_frame is None:
        centering_progress[pid] = {"status": f"error: cannot read first frame {first_frame_path}"}
        return

    h_img, w_img = first_frame.shape[:2]
    cx_target = w_img / 2.0
    cy_target = h_img / 2.0

    tracker = _create_tracker(tracker_type)
    tracker.init(first_frame, roi_bbox)

    centered_frames_list = []

    for i, finfo in enumerate(frames_list):
        fname = finfo["filename"]
        src_path = str(source_frames_dir / fname)
        out_fname = f"centered_{i:05d}.jpg"
        out_path = str(centered_dir / out_fname)

        frame = cv2.imread(src_path)
        if frame is None:
            # copy original if can't read
            entry = {
                "index": i,
                "original_file": fname,
                "centered_file": out_fname,
                "timecode": finfo.get("timecode", ""),
                "timestamp": finfo.get("timestamp", 0),
                "tracker_type": tracker_type,
                "roi": roi_bbox,
                "bbox": None,
                "status": "read_error",
            }
            centered_frames_list.append(entry)
            centering_progress[pid]["done"] = i + 1
            continue

        if i == 0:
            ok = True
            bbox = roi_bbox
        else:
            ok, bbox = tracker.update(frame)

        if ok:
            bbox = tuple(int(v) for v in bbox)
            bx, by, bw, bh = bbox
            cx_obj = bx + bw / 2.0
            cy_obj = by + bh / 2.0
            dx = cx_target - cx_obj
            dy = cy_target - cy_obj
            # Affine transform: shift only
            M = np.float32([[1, 0, dx], [0, 1, dy]])
            centered = cv2.warpAffine(
                frame, M, (w_img, h_img),
                borderMode=cv2.BORDER_REFLECT_101,
            )
            cv2.imwrite(out_path, centered, [cv2.IMWRITE_JPEG_QUALITY, 95])
            status = "ok"
        else:
            # tracker lost — save original unchanged
            cv2.imwrite(out_path, frame, [cv2.IMWRITE_JPEG_QUALITY, 95])
            bbox = None
            status = "lost"

        entry = {
            "index": i,
            "original_file": fname,
            "centered_file": out_fname,
            "timecode": finfo.get("timecode", ""),
            "timestamp": finfo.get("timestamp", 0),
            "tracker_type": tracker_type,
            "roi": list(roi_bbox),
            "bbox": list(bbox) if bbox else None,
            "status": status,
        }
        centered_frames_list.append(entry)
        centering_progress[pid]["done"] = i + 1

    # Save centered_index.json
    centered_index = {
        "project_id": pid,
        "tracker_type": tracker_type,
        "stabilized": stabilize,
        "roi": {
            "x": roi_data["x"],
            "y": roi_data["y"],
            "w": roi_data["w"],
            "h": roi_data["h"],
        },
        "num_frames": num_frames,
        "frames": centered_frames_list,
    }
    (pdir / "centered_index.json").write_text(
        json.dumps(centered_index, indent=2), encoding="utf-8"
    )

    # Clean up stabilized_frames if they were used
    stab_dir = pdir / "stabilized_frames"
    if stab_dir.exists():
        shutil.rmtree(str(stab_dir), ignore_errors=True)

    centering_progress[pid]["status"] = "done"


@app.post("/api/projects/{pid}/generate_centered")
async def generate_centered(
    pid: str,
    bg: BackgroundTasks,
    tracker_type: str = Form("CSRT"),
    stabilize: bool = Form(False),
):
    _require_cv2()
    pdir = PROJECTS_DIR / pid
    if not pdir.exists():
        raise HTTPException(404, "Project not found")
    if not (pdir / "index.json").exists():
        raise HTTPException(400, "Frames not generated yet")
    if not (pdir / "roi.json").exists():
        raise HTTPException(400, "ROI not set")

    centering_progress[pid] = {"total": 0, "done": 0, "status": "starting"}
    bg.add_task(_generate_centered_task, pid, tracker_type, stabilize)
    return {"status": "started"}


@app.get("/api/projects/{pid}/centered_status")
async def centered_status(pid: str):
    return centering_progress.get(pid, {"status": "unknown"})


@app.get("/api/projects/{pid}/centered_frames")
async def list_centered_frames(pid: str):
    p = PROJECTS_DIR / pid / "centered_index.json"
    if not p.exists():
        raise HTTPException(404, "Centered frames not generated")
    return json.loads(p.read_text(encoding="utf-8"))


@app.get("/api/projects/{pid}/centered_frames/{idx}")
async def get_centered_frame(pid: str, idx: int):
    p = PROJECTS_DIR / pid / "centered_frames" / f"centered_{idx:05d}.jpg"
    if not p.exists():
        raise HTTPException(404)
    return FileResponse(str(p), media_type="image/jpeg")


# ─── Centered Annotations (legacy, kept for backward compatibility) ────

@app.get("/api/projects/{pid}/annotations_centered")
async def list_centered_annotations(pid: str):
    p = PROJECTS_DIR / pid / "annotations_centered_index.json"
    if not p.exists():
        return {"annotations": []}
    return json.loads(p.read_text(encoding="utf-8"))


# ─── App-level settings (global defaults) ──────────────────────────────

APP_SETTINGS_PATH = PROJECTS_DIR / "app_settings.json"

_DEFAULT_APP_SETTINGS: dict = {
    "theme": "dark",
    "defaults": {
        "auto_tune_enabled": True,
        "density_mode": "auto",
        "density_percent": 40,
        "quality_mode": "auto",
        "quality_percent": 80,
    },
    "behavior": {
        "autosave_enabled": True,
    },
    "ui": {
        "show_project_buttons": True,
        "project_buttons": {
            "save": True,
            "reset": True,
            "info": True,
        },
        "show_kpi": True,
        "kpi_items": {
            "duration": True,
            "frames": True,
            "step": True,
            "frame_size": True,
            "total_size": True,
            "percent": True,
        },
    },
    "viewer": {
        "default_sensitivity": 50,
        "max_sensitivity": 100,
        "default_infinite_rotation": False,
        "default_auto_rotate": False,
    },
}


def _load_app_settings() -> dict:
    """Load app settings from disk, falling back to defaults."""
    if APP_SETTINGS_PATH.exists():
        try:
            data = json.loads(APP_SETTINGS_PATH.read_text(encoding="utf-8"))
            # Merge with defaults to ensure all keys exist
            merged = json.loads(json.dumps(_DEFAULT_APP_SETTINGS))
            if isinstance(data.get("theme"), str):
                merged["theme"] = data["theme"]
            if isinstance(data.get("defaults"), dict):
                for k in merged["defaults"]:
                    if k in data["defaults"]:
                        merged["defaults"][k] = data["defaults"][k]
            if isinstance(data.get("behavior"), dict):
                for k in merged["behavior"]:
                    if k in data["behavior"]:
                        merged["behavior"][k] = data["behavior"][k]
            if isinstance(data.get("ui"), dict):
                for k in merged["ui"]:
                    if k in data["ui"]:
                        # Deep-merge sub-dicts (project_buttons, kpi_items)
                        if isinstance(merged["ui"][k], dict) and isinstance(data["ui"][k], dict):
                            for sk in merged["ui"][k]:
                                if sk in data["ui"][k]:
                                    merged["ui"][k][sk] = data["ui"][k][sk]
                        else:
                            merged["ui"][k] = data["ui"][k]
            if isinstance(data.get("viewer"), dict):
                for k in merged["viewer"]:
                    if k in data["viewer"]:
                        merged["viewer"][k] = data["viewer"][k]
            return merged
        except Exception:
            pass
    return json.loads(json.dumps(_DEFAULT_APP_SETTINGS))


def _save_app_settings(settings: dict) -> None:
    """Persist app settings to disk."""
    APP_SETTINGS_PATH.write_text(
        json.dumps(settings, indent=2, ensure_ascii=False), encoding="utf-8"
    )


@app.get("/api/app-settings")
async def get_app_settings():
    """Return current app-level settings."""
    return _load_app_settings()


@app.put("/api/app-settings")
async def put_app_settings(payload: dict = Body(...)):
    """
    Update app-level settings.  Accepts a full or partial settings object
    and merges it with existing settings on disk.
    """
    current = _load_app_settings()

    # theme
    if "theme" in payload and isinstance(payload["theme"], str):
        current["theme"] = payload["theme"]

    # defaults
    if isinstance(payload.get("defaults"), dict):
        defs = payload["defaults"]
        if "auto_tune_enabled" in defs and isinstance(defs["auto_tune_enabled"], bool):
            current["defaults"]["auto_tune_enabled"] = defs["auto_tune_enabled"]
        if "density_mode" in defs and defs["density_mode"] in ("auto", "manual"):
            current["defaults"]["density_mode"] = defs["density_mode"]
        if "density_percent" in defs:
            try:
                v = int(defs["density_percent"])
                current["defaults"]["density_percent"] = max(0, min(100, v))
            except (ValueError, TypeError):
                pass
        if "quality_mode" in defs and defs["quality_mode"] in ("auto", "manual"):
            current["defaults"]["quality_mode"] = defs["quality_mode"]
        if "quality_percent" in defs:
            try:
                v = int(defs["quality_percent"])
                current["defaults"]["quality_percent"] = max(0, min(100, v))
            except (ValueError, TypeError):
                pass

    # behavior
    if isinstance(payload.get("behavior"), dict):
        beh = payload["behavior"]
        if "behavior" not in current or not isinstance(current["behavior"], dict):
            current["behavior"] = {"autosave_enabled": True}
        if "autosave_enabled" in beh and isinstance(beh["autosave_enabled"], bool):
            current["behavior"]["autosave_enabled"] = beh["autosave_enabled"]

    # ui
    if isinstance(payload.get("ui"), dict):
        ui = payload["ui"]
        if "show_project_buttons" in ui and isinstance(ui["show_project_buttons"], bool):
            current["ui"]["show_project_buttons"] = ui["show_project_buttons"]
        if "show_kpi" in ui and isinstance(ui["show_kpi"], bool):
            current["ui"]["show_kpi"] = ui["show_kpi"]
        # project_buttons sub-dict
        if isinstance(ui.get("project_buttons"), dict):
            if "project_buttons" not in current["ui"] or not isinstance(current["ui"]["project_buttons"], dict):
                current["ui"]["project_buttons"] = {"save": True, "reset": True, "info": True}
            for k in ("save", "reset", "info"):
                if k in ui["project_buttons"] and isinstance(ui["project_buttons"][k], bool):
                    current["ui"]["project_buttons"][k] = ui["project_buttons"][k]
        # kpi_items sub-dict
        if isinstance(ui.get("kpi_items"), dict):
            if "kpi_items" not in current["ui"] or not isinstance(current["ui"]["kpi_items"], dict):
                current["ui"]["kpi_items"] = {"duration": True, "frames": True, "step": True, "frame_size": True, "total_size": True, "percent": True}
            for k in ("duration", "frames", "step", "frame_size", "total_size", "percent"):
                if k in ui["kpi_items"] and isinstance(ui["kpi_items"][k], bool):
                    current["ui"]["kpi_items"][k] = ui["kpi_items"][k]

    # viewer
    if isinstance(payload.get("viewer"), dict):
        vw = payload["viewer"]
        if "viewer" not in current or not isinstance(current["viewer"], dict):
            current["viewer"] = {
                "default_sensitivity": 50,
                "max_sensitivity": 100,
                "default_infinite_rotation": False,
                "default_auto_rotate": False,
            }
        if "max_sensitivity" in vw:
            try:
                v = int(vw["max_sensitivity"])
                current["viewer"]["max_sensitivity"] = max(10, min(100, v))
            except (ValueError, TypeError):
                pass
        cur_max = current["viewer"].get("max_sensitivity", 100)
        if "default_sensitivity" in vw:
            try:
                v = int(vw["default_sensitivity"])
                current["viewer"]["default_sensitivity"] = max(1, min(cur_max, v))
            except (ValueError, TypeError):
                pass
        else:
            # Clamp existing default_sensitivity to new max
            cur_def = current["viewer"].get("default_sensitivity", 50)
            if cur_def > cur_max:
                current["viewer"]["default_sensitivity"] = cur_max
        if "default_infinite_rotation" in vw and isinstance(vw["default_infinite_rotation"], bool):
            current["viewer"]["default_infinite_rotation"] = vw["default_infinite_rotation"]
        if "default_auto_rotate" in vw and isinstance(vw["default_auto_rotate"], bool):
            current["viewer"]["default_auto_rotate"] = vw["default_auto_rotate"]

    _save_app_settings(current)
    return current


# ─── Static frontend ────────────────────────────────────────────────────
app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")

# ─── Entry point ─────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys

    service_mode = "--service" in sys.argv
    if service_mode:
        sys.argv.remove("--service")

    runtime_dir = BASE_DIR / ".runtime"
    runtime_dir.mkdir(exist_ok=True)

    if service_mode:
        # Write PID file so stop_3dps.bat can find this process
        _pid_file = runtime_dir / "server.pid"
        with open(_pid_file, "w") as _f:
            _f.write(str(os.getpid()))

        # Redirect stdout/stderr to log file at OS level
        # so child processes (uvicorn workers) also write to the log
        _log_path = str(runtime_dir / "server.log")
        _fd = os.open(_log_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
        os.dup2(_fd, 1)
        os.dup2(_fd, 2)
        os.close(_fd)
        sys.stdout = open(1, "w", encoding="utf-8", buffering=1, closefd=False)
        sys.stderr = open(2, "w", encoding="utf-8", buffering=1, closefd=False)

    # #region agent log
    try:
        import json as _json_dbg2
        with open(str(BASE_DIR / ".cursor" / "debug.log"), "a", encoding="utf-8") as _dbg_f2:
            _dbg_f2.write(_json_dbg2.dumps({"timestamp": int(datetime.now().timestamp() * 1000), "location": "main.py:__main__", "message": "Server starting", "data": {"pid": os.getpid(), "service_mode": service_mode, "cwd": os.getcwd()}, "hypothesisId": "H2"}) + "\n")
    except Exception:
        pass
    # #endregion

    print(f"{'=' * 50}")
    print(f"3Dps Server")
    print(f"{'=' * 50}")
    print(f"Time:         {datetime.now()}")
    print(f"Python:       {sys.version}")
    print(f"Working dir:  {os.getcwd()}")
    print(f"PID:          {os.getpid()}")
    print(f"Service mode: {service_mode}")
    print(f"{'=' * 50}")

    app_import = "backend.main:app" if __package__ else "main:app"
    print(f"\n  Open in browser: http://127.0.0.1:8000\n")
    uvicorn.run(app_import, host="127.0.0.1", port=8000, reload=True)
