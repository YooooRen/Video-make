"""開跑前的體檢：把所有會讓你等半小時才失敗的問題，在 10 秒內先問出來。"""
from __future__ import annotations

import os
import shutil
import unicodedata
from pathlib import Path

from .util import IMAGE_EXT, VIDEO_EXT, have, iter_media, media_info

OK, WARN, FAIL = "✅", "⚠️ ", "❌"


def _display_width(text: str) -> int:
    """中日韓字元在終端機佔兩格，用來對齊欄位。"""
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)


class _Report:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str]] = []
        self.failed = 0
        self.warned = 0

    def add(self, level: str, item: str, detail: str) -> None:
        self.rows.append((level, item, detail))
        if level == FAIL:
            self.failed += 1
        elif level == WARN:
            self.warned += 1

    def render(self) -> None:
        width = max(_display_width(r[1]) for r in self.rows) + 2
        print("\n" + "═" * 66)
        print("  開跑前體檢")
        print("═" * 66)
        for level, item, detail in self.rows:
            pad = " " * (width - _display_width(item))
            print(f"  {level} {item}{pad} {detail}")
        print("═" * 66)
        if self.failed:
            print(f"  {self.failed} 項必須先解決才能開始（見上方 ❌）")
        elif self.warned:
            print(f"  可以開跑。{self.warned} 項功能會降級（見上方 ⚠️）")
        else:
            print("  全部就緒，可以 python run.py")
        print("═" * 66 + "\n")


def check(cfg) -> int:
    """回傳 0 = 可以開跑，1 = 有必須解決的問題。"""
    r = _Report()
    _tools(cfg, r)
    _packages(r)
    _source(cfg, r)
    _media(cfg, r)
    _font(cfg, r)
    _diarization(cfg, r)
    _disk(cfg, r)
    r.render()
    return 1 if r.failed else 0


def _tools(cfg, r: _Report) -> None:
    for binary, hint in (("ffmpeg", "brew install ffmpeg"), ("ffprobe", "brew install ffmpeg")):
        r.add(OK if have(binary) else FAIL, binary,
              "已安裝" if have(binary) else f"找不到 → {hint}")

    backend = cfg.get("claude.backend", "cli")
    key = os.environ.get(cfg.get("claude.api_key_env", "ANTHROPIC_API_KEY"), "")
    binary = cfg.get("claude.cli_binary", "claude")
    if backend in ("cli", "auto") and not key:
        if have(binary):
            r.add(OK, "claude CLI", "已安裝（記得先執行過一次 claude 完成登入）")
        else:
            r.add(FAIL, "claude CLI",
                  "找不到 → npm install -g @anthropic-ai/claude-code")
    elif key:
        r.add(OK, "Anthropic API key", "已設定，將使用 API backend")
    else:
        r.add(FAIL, "Anthropic API key", "backend=api 但環境變數是空的")


def _packages(r: _Report) -> None:
    required = [("yaml", "pyyaml"), ("faster_whisper", "faster-whisper"),
                ("matplotlib", "matplotlib"), ("PIL", "Pillow")]
    optional = [("pyannote.audio", "pyannote.audio", "不會自動分鏡，其餘正常"),
                ("cartopy", "cartopy", "航線圖沒有海岸線，改用簡化底圖")]
    for mod, pkg in required:
        r.add(*_probe(mod, pkg, None))
    for mod, pkg, note in optional:
        r.add(*_probe(mod, pkg, note))


def _probe(mod: str, pkg: str, degraded: str | None):
    import importlib.util
    try:
        found = importlib.util.find_spec(mod) is not None
    except (ImportError, ValueError):
        found = False
    if found:
        return (OK, pkg, "已安裝")
    if degraded:
        return (WARN, pkg, f"未安裝 → {degraded}")
    return (FAIL, pkg, f"未安裝 → pip install {pkg}")


def _source(cfg, r: _Report) -> None:
    raw = cfg.get("project.source_video", "")
    if not raw:
        r.add(FAIL, "訪談影片", "project.source_video 沒有填")
        return
    p = cfg.path(raw)
    if not p.exists():
        r.add(FAIL, "訪談影片", f"找不到 {p}")
        return
    try:
        info = media_info(p)
    except Exception as exc:  # noqa: BLE001
        r.add(FAIL, "訪談影片", f"無法讀取（{exc}）")
        return
    if not info["has_audio"]:
        r.add(FAIL, "訪談影片", f"{p.name} 沒有音軌，無法轉錄")
        return
    mins = info["duration"] / 60
    r.add(OK, "訪談影片",
          f'{p.name}｜{mins:.1f} 分鐘｜{info.get("width")}x{info.get("height")}'
          f'｜{info.get("fps_num",30)/info.get("fps_den",1):.2f}fps')
    if mins > 120:
        r.add(WARN, "  片長", f"{mins:.0f} 分鐘偏長，轉錄可能要一小時以上")


def _media(cfg, r: _Report) -> None:
    raw = cfg.get("project.media_dir", "")
    if not raw:
        r.add(WARN, "素材資料夾", "沒有設定 → 不會有 B-roll")
        return
    d = cfg.path(raw)
    if not d.exists():
        r.add(FAIL, "素材資料夾", f"找不到 {d}")
        return

    src = cfg.path(cfg.get("project.source_video", "") or ".")
    vids = [p for p in iter_media(d, VIDEO_EXT) if p.resolve() != src.resolve()]
    imgs = iter_media(d, IMAGE_EXT)
    total = len(vids) + len(imgs)
    cap = int(cfg.get("broll.max_index", 80))

    if total == 0:
        r.add(WARN, "素材資料夾", f"{d} 裡沒有影片或照片 → 不會有 B-roll")
        return
    r.add(OK, "素材資料夾", f"{len(vids)} 支影片、{len(imgs)} 張照片")
    if total > cap:
        r.add(WARN, "  素材數量",
              f"共 {total} 份，超過 broll.max_index={cap}，只會辨識前 {cap} 份")
    if src.exists() and d.resolve() == src.parent.resolve():
        r.add(WARN, "  素材位置",
              "與訪談影片同一個資料夾，無關檔案也會被當成 B-roll 候選")


def _font(cfg, r: _Report) -> None:
    from . import render
    try:
        path = render.resolve_font(cfg)
        r.add(OK, "中文字型", Path(path).name)
    except Exception:
        r.add(FAIL, "中文字型",
              "找不到 → 在 project.yaml 設 explainers.font_path")


def _diarization(cfg, r: _Report) -> None:
    if not cfg.get("transcribe.diarize", True):
        r.add(WARN, "說話者分離", "設定為關閉 → 不會自動分鏡")
        return
    env = cfg.get("transcribe.hf_token_env", "HF_TOKEN")
    if os.environ.get(env):
        r.add(OK, "HuggingFace token", f"{env} 已設定")
    elif cfg.get("framing.speaker_positions"):
        r.add(WARN, "HuggingFace token",
              f"{env} 未設定 → 無法分辨誰在講話，分鏡會失效")
    else:
        r.add(WARN, "HuggingFace token",
              f"{env} 未設定 → 不會自動分鏡（見 README 的兩分鐘設定步驟）")


def _disk(cfg, r: _Report) -> None:
    try:
        free = shutil.disk_usage(cfg.build).free / 1e9
    except OSError:
        return
    level = OK if free > 10 else (WARN if free > 3 else FAIL)
    r.add(level, "可用磁碟空間",
          f"{free:.1f} GB" + ("" if free > 10 else "（模型與中間檔約需 5–10 GB）"))
