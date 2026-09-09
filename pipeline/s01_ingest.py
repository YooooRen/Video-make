"""Stage 01：檢查素材、決定時間軸格式、抽出給語音辨識用的音軌。"""
from __future__ import annotations

from pathlib import Path

from .timeline import Rate
from .util import (IMAGE_EXT, VIDEO_EXT, StageError, iter_media, log,
                   media_info, require, run, write_json)


def run_stage(cfg, claude=None) -> dict:
    require("ffmpeg", "請先 `brew install ffmpeg`。")
    src = cfg.source_video
    log("01", f"讀取訪談影片 {src.name}")
    info = media_info(src)
    if not info["has_audio"]:
        raise StageError(f"{src.name} 沒有音軌，無法轉錄。")
    if info["duration"] <= 0:
        raise StageError(f"無法判讀 {src.name} 的長度，檔案可能損毀。")

    # ---- 決定成品時間軸格式 -------------------------------------------------
    width = int(cfg.get("sequence.width") or 0) or info.get("width", 1920)
    height = int(cfg.get("sequence.height") or 0) or info.get("height", 1080)
    src_rate = Rate(info.get("fps_num", 30), info.get("fps_den", 1))
    rate = Rate.parse(cfg.get("sequence.fps") or None, fallback=src_rate)
    log("01", f"時間軸 {width}x{height} @ {rate.fps:.3f}fps "
              f"（原始 {info.get('width')}x{info.get('height')} @ {src_rate.fps:.3f}）")

    # ---- 抽音軌（16k mono，Whisper 的原生取樣率）----------------------------
    audio = cfg.build_file("audio.wav")
    if audio.exists() and audio.stat().st_mtime > src.stat().st_mtime:
        log("01", "音軌已存在，沿用")
    else:
        log("01", "抽出音軌 → audio.wav")
        run(["ffmpeg", "-y", "-v", "error", "-i", str(src),
             "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(audio)])

    # ---- 清點 B-roll 素材 ---------------------------------------------------
    media_dir = cfg.media_dir
    videos: list[Path] = []
    images: list[Path] = []
    if media_dir:
        videos = [p for p in iter_media(media_dir, VIDEO_EXT) if p.resolve() != src.resolve()]
        images = iter_media(media_dir, IMAGE_EXT)
        log("01", f"素材資料夾：{len(videos)} 支影片、{len(images)} 張照片")
    elif cfg.get("broll.enabled"):
        log("01", "沒有設定 project.media_dir，B-roll 階段會自動跳過")

    out = {
        "source": info,
        "audio": str(audio),
        "sequence": {
            "width": width, "height": height,
            "fps_num": rate.num, "fps_den": rate.den,
            "frame_duration": rate.frame_duration,
            "audio_rate": int(cfg.get("sequence.audio_rate", 48000)),
        },
        "broll_candidates": {
            "videos": [str(p) for p in videos],
            "images": [str(p) for p in images],
        },
    }
    write_json(cfg.build_file("01_ingest.json"), out)
    log("01", f"完成（片長 {info['duration']/60:.1f} 分鐘）")
    return out
