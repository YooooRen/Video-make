"""Stage 01：檢查素材、決定時間軸格式、抽出給語音辨識用的音軌。"""
from __future__ import annotations

from pathlib import Path

from .timeline import Rate
from .util import (StageError, file_sig, log, media_info, read_json,
                   require, run, write_json)


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
    if info.get("timecode"):
        log("01", f"嵌入時間碼 {info['timecode']}"
                  f"（{'DF' if info.get('drop_frame') else 'NDF'}）"
                  f" → 素材起點 {info.get('start', 0.0):.3f}s")

    # ---- 抽音軌（16k mono，Whisper 的原生取樣率）----------------------------
    audio = cfg.build_file("audio.wav")
    stamp = cfg.build_file("audio.source.json")
    sig = {"sig": file_sig(src), "path": str(src)}
    # 用來源檔的指紋判斷，不能用「音軌比來源新」——換成另一支較舊的影片時，
    # 舊音軌反而比較新，會被誤判成可以沿用，於是整條 pipeline 都在處理上一支片。
    cached = read_json(stamp, default={})
    if audio.exists() and cached.get("sig") == sig["sig"]:
        log("01", "音軌已存在且來源相同，沿用")
    else:
        if audio.exists():
            prev = Path(cached.get("path", "")).name or "先前的來源"
            log("01", f"來源已變更（{prev} → {src.name}），重新抽音軌")
        else:
            log("01", "抽出音軌 → audio.wav")
        run(["ffmpeg", "-y", "-v", "error", "-i", str(src),
             "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(audio)])
        write_json(stamp, sig)

    out = {
        "source": info,
        "audio": str(audio),
        "sequence": {
            "width": width, "height": height,
            "fps_num": rate.num, "fps_den": rate.den,
            "frame_duration": rate.frame_duration,
            "audio_rate": int(cfg.get("sequence.audio_rate", 48000)),
        },
    }
    write_json(cfg.build_file("01_ingest.json"), out)
    log("01", f"完成（片長 {info['duration']/60:.1f} 分鐘）")
    return out
