"""共用工具：外部程式呼叫、ffprobe/ffmpeg、JSON 讀寫、日誌。"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Iterable, Sequence

VIDEO_EXT = {".mov", ".mp4", ".m4v", ".avi", ".mts", ".mxf", ".mkv", ".insv"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".heic", ".tif", ".tiff", ".webp", ".dng"}

_T0 = time.time()


def log(stage: str, msg: str) -> None:
    el = time.time() - _T0
    print(f"[{el:7.1f}s] [{stage}] {msg}", flush=True)


def warn(stage: str, msg: str) -> None:
    log(stage, f"⚠️  {msg}")


class StageError(RuntimeError):
    pass


def have(binary: str) -> bool:
    return shutil.which(binary) is not None


def require(binary: str, hint: str = "") -> None:
    if not have(binary):
        raise StageError(f"找不到必要程式 `{binary}`。{hint}")


def run(cmd: Sequence[str], *, check: bool = True, capture: bool = True,
        stdin: str | None = None, timeout: int | None = None,
        cwd: str | Path | None = None) -> subprocess.CompletedProcess:
    """執行外部指令；capture=False 時直接把輸出接到終端機（給 ffmpeg 進度用）。"""
    proc = subprocess.run(
        list(cmd),
        input=stdin,
        capture_output=capture,
        text=True,
        timeout=timeout,
        cwd=str(cwd) if cwd else None,
    )
    if check and proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "")[-3000:]
        raise StageError(f"指令失敗 ({proc.returncode}): {' '.join(cmd[:6])} ...\n{tail}")
    return proc


# ---------------------------------------------------------------- ffprobe ---

def ffprobe(path: str | Path) -> dict:
    """回傳 ffprobe 的完整 JSON。"""
    require("ffprobe", "請先 `brew install ffmpeg`。")
    proc = run([
        "ffprobe", "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", str(path),
    ])
    return json.loads(proc.stdout)


def _stream(info: dict, kind: str) -> dict | None:
    for s in info.get("streams", []):
        if s.get("codec_type") == kind:
            return s
    return None


def media_info(path: str | Path) -> dict:
    """把 ffprobe 結果整理成 pipeline 用的精簡結構。"""
    info = ffprobe(path)
    v = _stream(info, "video")
    a = _stream(info, "audio")
    dur = float(info.get("format", {}).get("duration") or 0.0)
    out: dict[str, Any] = {
        "path": str(Path(path).resolve()),
        "duration": dur,
        "has_video": v is not None,
        "has_audio": a is not None,
    }
    if v:
        out["width"] = int(v.get("width") or 0)
        out["height"] = int(v.get("height") or 0)
        out["fps_num"], out["fps_den"] = parse_rate(
            v.get("r_frame_rate") or v.get("avg_frame_rate") or "30/1")
        # 旋轉中繼資料（手機直式影片）
        rot = 0
        for sd in v.get("side_data_list", []) or []:
            if "rotation" in sd:
                rot = int(sd["rotation"])
        out["rotation"] = rot
        if abs(rot) % 180 == 90:
            out["width"], out["height"] = out["height"], out["width"]
    if a:
        out["audio_rate"] = int(a.get("sample_rate") or 48000)
        out["audio_channels"] = int(a.get("channels") or 2)
    return out


def parse_rate(text: str) -> tuple[int, int]:
    """'30000/1001' -> (30000, 1001)。"""
    if "/" in text:
        n, d = text.split("/", 1)
        n, d = int(n), int(d)
    else:
        n, d = int(round(float(text) * 1000)), 1000
    if d == 0:
        n, d = 30, 1
    return n, d


# ------------------------------------------------------------------- files --

def iter_media(root: str | Path, exts: set[str]) -> list[Path]:
    root = Path(root)
    if not root.exists():
        return []
    found = [p for p in sorted(root.rglob("*"))
             if p.is_file() and p.suffix.lower() in exts and not p.name.startswith("._")]
    return found


def file_sig(path: str | Path) -> str:
    """檔案指紋：路徑 + 大小 + mtime，用來做快取失效判斷。"""
    p = Path(path)
    st = p.stat()
    raw = f"{p.resolve()}|{st.st_size}|{int(st.st_mtime)}"
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def read_json(path: str | Path, default: Any = None) -> Any:
    p = Path(path)
    if not p.exists():
        if default is not None:
            return default
        raise StageError(f"缺少前一階段的產物：{p}（請先跑對應的 stage）")
    return json.loads(p.read_text(encoding="utf-8"))


def write_json(path: str | Path, data: Any) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def write_text(path: str | Path, text: str) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


# -------------------------------------------------------------- JSON 抽取 ---

_FENCE = re.compile(r"```(?:json|jsonc)?\s*(.+?)```", re.S)


def extract_json(text: str) -> Any:
    """從 LLM 的自由文字回覆裡撈出第一個合法的 JSON 物件或陣列。"""
    if text is None:
        raise StageError("模型沒有回傳任何內容")
    text = text.strip()
    candidates: list[str] = []
    for m in _FENCE.finditer(text):
        candidates.append(m.group(1).strip())
    candidates.append(text)
    for cand in candidates:
        try:
            return json.loads(cand)
        except json.JSONDecodeError:
            pass
        block = _balanced_block(cand)
        if block:
            try:
                return json.loads(block)
            except json.JSONDecodeError:
                continue
    raise StageError(f"無法從模型回覆解析出 JSON。前 500 字：\n{text[:500]}")


def _balanced_block(text: str) -> str | None:
    """掃出第一個成對的 {...} 或 [...]，忽略字串內的括號。"""
    start = None
    for i, ch in enumerate(text):
        if ch in "{[":
            start = i
            break
    if start is None:
        return None
    opener = text[start]
    closer = "}" if opener == "{" else "]"
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def chunked(items: Sequence[Any], size: int) -> Iterable[Sequence[Any]]:
    for i in range(0, len(items), size):
        yield items[i:i + size]


def clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def fmt_hhmmss(sec: float) -> str:
    sec = max(0.0, sec)
    h, rem = divmod(int(sec), 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
