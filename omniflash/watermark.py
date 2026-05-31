"""Omni Flash — Video Watermark Remover.

Removes Gemini/Flow watermark from generated videos using reverse alpha blending.
Ported from Gemini-Watermark-Remover-UI (TypeScript → Python).

Algorithm:
  Gemini adds watermark:  watermarked = α × logo + (1 - α) × original
  Reverse solve:          original = (watermarked - α × logo) / (1 - α)
"""

import logging
import os
from pathlib import Path

import cv2
import numpy as np

log = logging.getLogger("omniflash.watermark")

# ── Constants ──────────────────────────────────────────────
ALPHA_THRESHOLD = 0.002   # Ignore very small alpha values (noise)
MAX_ALPHA = 0.99          # Avoid division by near-zero values
LOGO_VALUE = 255          # White watermark color value
VIDEO_ALPHA_SCALE = 0.6   # Gemini videos use 60% opacity watermark

ASSETS_DIR = Path(__file__).parent / "assets"


def _load_alpha_map(size: int) -> np.ndarray:
    """Load background capture image and calculate alpha map.
    
    Alpha map is the max RGB channel of the watermark on black background,
    normalized to [0, 1].
    """
    bg_path = ASSETS_DIR / f"bg_{size}.png"
    if not bg_path.exists():
        raise FileNotFoundError(f"Watermark asset not found: {bg_path}")
    
    bg = cv2.imread(str(bg_path), cv2.IMREAD_COLOR)
    if bg is None:
        raise ValueError(f"Failed to load watermark asset: {bg_path}")
    
    # Resize to exact size if needed
    if bg.shape[0] != size or bg.shape[1] != size:
        bg = cv2.resize(bg, (size, size))
    
    # Max of RGB channels, normalized to [0, 1]
    alpha_map = bg.max(axis=2).astype(np.float32) / 255.0
    return alpha_map


# Cache alpha maps
_alpha_cache: dict[int, np.ndarray] = {}


def get_alpha_map(size: int) -> np.ndarray:
    """Get cached alpha map for given watermark size."""
    if size not in _alpha_cache:
        _alpha_cache[size] = _load_alpha_map(size)
    return _alpha_cache[size]


def detect_watermark_config(width: int, height: int, is_video: bool = True):
    """Detect watermark size and position based on video dimensions."""
    if is_video:
        short_dim = min(width, height)
        if short_dim >= 1080:
            logo_size = 96
            margin_right = 64
            margin_bottom = 64
        else:
            logo_size = 48
            margin_right = 72
            margin_bottom = 72
    else:
        if width > 1024 or height > 1024:
            logo_size = 96
            margin_right = 64
            margin_bottom = 64
        else:
            logo_size = 48
            margin_right = 32
            margin_bottom = 32

    x = width - margin_right - logo_size
    y = height - margin_bottom - logo_size

    return {
        "logo_size": logo_size,
        "x": x,
        "y": y,
        "width": logo_size,
        "height": logo_size,
    }


def remove_watermark_frame(frame: np.ndarray, is_video: bool = True) -> np.ndarray:
    """Remove watermark from a single frame (BGR numpy array).
    
    Uses reverse alpha blending:
      original = (watermarked - α × logo) / (1 - α)
    """
    h, w = frame.shape[:2]
    config = detect_watermark_config(w, h, is_video)
    alpha_map = get_alpha_map(config["logo_size"])
    
    x, y = config["x"], config["y"]
    size = config["logo_size"]
    
    # Extract watermark region
    region = frame[y:y+size, x:x+size].astype(np.float32)
    
    # Scale alpha for video
    alpha = alpha_map.copy()
    if is_video:
        alpha = alpha * VIDEO_ALPHA_SCALE
    
    # Create mask for significant alpha values
    mask = alpha >= ALPHA_THRESHOLD
    alpha_clamped = np.clip(alpha, 0, MAX_ALPHA)
    one_minus_alpha = 1.0 - alpha_clamped
    
    # Apply reverse alpha blending to each channel
    for c in range(3):  # BGR
        channel = region[:, :, c]
        # original = (watermarked - α × LOGO_VALUE) / (1 - α)
        original = (channel - alpha_clamped * LOGO_VALUE) / one_minus_alpha
        # Only modify pixels where alpha is significant
        channel[mask] = original[mask]
        region[:, :, c] = np.clip(channel, 0, 255)
    
    # Write back
    frame[y:y+size, x:x+size] = region.astype(np.uint8)
    return frame


def remove_watermark_video(input_path: str, output_path: str = None, 
                            show_progress: bool = True) -> str:
    """Remove watermark from a video file using ffmpeg for I/O.
    
    Args:
        input_path: Path to input video file
        output_path: Path for output video (default: adds _clean suffix)
        show_progress: Show progress log
    
    Returns:
        Path to cleaned video file
    """
    import subprocess
    import struct
    
    input_path = str(input_path)
    if output_path is None:
        base, ext = os.path.splitext(input_path)
        output_path = f"{base}_clean{ext}"
    
    # Get video info via ffprobe
    probe = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams", "-show_format", input_path],
        capture_output=True, text=True
    )
    import json
    info = json.loads(probe.stdout)
    
    video_stream = next(s for s in info["streams"] if s["codec_type"] == "video")
    width = int(video_stream["width"])
    height = int(video_stream["height"])
    
    # Parse fps (could be "24/1" or "23.976")
    fps_str = video_stream.get("r_frame_rate", "24/1")
    if "/" in fps_str:
        num, den = fps_str.split("/")
        fps = float(num) / float(den)
    else:
        fps = float(fps_str)
    
    duration = float(info.get("format", {}).get("duration", 0))
    total_frames = int(duration * fps) if duration > 0 else 0
    
    log.info("🎬 Processing video: %dx%d @ %.1f fps (%d frames)", width, height, fps, total_frames)
    
    # ffmpeg: decode → raw frames (BGR24)
    read_cmd = [
        "ffmpeg", "-i", input_path,
        "-f", "rawvideo", "-pix_fmt", "bgr24",
        "-v", "quiet", "-"
    ]
    
    # ffmpeg: encode ← raw frames
    write_cmd = [
        "ffmpeg", "-y",
        "-f", "rawvideo", "-pix_fmt", "bgr24",
        "-s", f"{width}x{height}", "-r", str(fps),
        "-i", "-",
        "-i", input_path,   # re-read for audio
        "-map", "0:v", "-map", "1:a?",
        "-c:v", "libx264", "-preset", "fast", "-crf", "18",
        "-c:a", "copy",
        "-movflags", "+faststart",
        "-v", "quiet",
        output_path
    ]
    
    reader = subprocess.Popen(read_cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    writer = subprocess.Popen(write_cmd, stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)
    
    frame_size = width * height * 3  # BGR24
    frame_count = 0
    
    try:
        while True:
            raw = reader.stdout.read(frame_size)
            if len(raw) < frame_size:
                break
            
            # Convert raw bytes to numpy array
            frame = np.frombuffer(raw, dtype=np.uint8).reshape((height, width, 3)).copy()
            
            # Remove watermark
            clean_frame = remove_watermark_frame(frame, is_video=True)
            
            # Write cleaned frame
            writer.stdin.write(clean_frame.tobytes())
            
            frame_count += 1
            if show_progress and frame_count % 30 == 0:
                pct = int(frame_count / total_frames * 100) if total_frames > 0 else 0
                log.info("   Processing: %d%% (%d/%d frames)", pct, frame_count, total_frames)
    finally:
        writer.stdin.close()
        writer.wait()
        reader.wait()
    
    size_mb = os.path.getsize(output_path) / (1024 * 1024)
    log.info("✅ Watermark removed: %s (%.1f MB, %d frames)", output_path, size_mb, frame_count)
    
    return output_path


def remove_watermark_image(input_path: str, output_path: str = None) -> str:
    """Remove watermark from a single image file.
    
    Args:
        input_path: Path to input image
        output_path: Path for output (default: adds _clean suffix)
    
    Returns:
        Path to cleaned image
    """
    input_path = str(input_path)
    if output_path is None:
        base, ext = os.path.splitext(input_path)
        output_path = f"{base}_clean{ext}"
    
    img = cv2.imread(input_path)
    if img is None:
        raise ValueError(f"Cannot read image: {input_path}")
    
    clean = remove_watermark_frame(img, is_video=False)
    cv2.imwrite(output_path, clean)
    
    log.info("✅ Watermark removed: %s", output_path)
    return output_path
