#!/usr/bin/env python3
"""
AI 訪談影片自動剪輯 pipeline —— 總控制台。

用法：
    python run.py --check              # 開跑前體檢（10 秒，強烈建議先跑）
    python run.py --probe              # 產生最小 FCPXML，排查 FCP 匯入問題
    python run.py                      # 從頭跑到尾
    python run.py --from 04            # 從第 4 階段接著跑
    python run.py --only 05 06         # 只重跑 B-roll 與說明短片
    python run.py --list               # 看有哪些階段
"""
from __future__ import annotations

import argparse
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pipeline import (s01_ingest, s02_transcribe, s03_clean, s04_subtitles,   # noqa: E402
                      s05_broll, s06_explainers, s07_framing, s08_fcpxml,
                      s09_package, s10_thumbnail)
from pipeline.claude_client import ClaudeClient                                # noqa: E402
from pipeline.config import load_config                                        # noqa: E402
from pipeline.util import StageError, log                                      # noqa: E402

STAGES = [
    ("01", "ingest",     "檢查素材、抽音軌",             s01_ingest,     False),
    ("02", "transcribe", "語音轉文字 + 說話者分離",       s02_transcribe, True),
    ("03", "clean",      "去贅詞、去停頓、決定剪輯點",     s03_clean,      True),
    ("04", "subtitles",  "生成中英雙語字幕",             s04_subtitles,  True),
    ("05", "broll",      "素材辨識 + B-roll 排片",       s05_broll,      True),
    ("06", "explainers", "航線圖／名詞卡說明短片",        s06_explainers, True),
    ("07", "framing",    "依說話者決定鏡位",             s07_framing,    True),
    ("08", "fcpxml",     "組出 Final Cut Pro 時間軸",    s08_fcpxml,     False),
    ("09", "package",    "標題／說明欄／章節／人名",       s09_package,    True),
    ("10", "thumbnail",  "封面圖",                      s10_thumbnail,  True),
]
_BY_KEY = {}
for _num, _name, _desc, _mod, _needs in STAGES:
    _BY_KEY[_num] = _BY_KEY[_name] = (_num, _name, _desc, _mod, _needs)


def main() -> int:
    ap = argparse.ArgumentParser(description="AI 訪談影片自動剪輯 pipeline")
    ap.add_argument("-c", "--config", default="project.yaml", help="設定檔路徑")
    ap.add_argument("--from", dest="start", default=None, help="從哪個階段開始（例如 04）")
    ap.add_argument("--to", dest="end", default=None, help="跑到哪個階段為止")
    ap.add_argument("--only", nargs="+", default=None, help="只跑指定的階段")
    ap.add_argument("--no-cache", action="store_true", help="不用 Claude 回覆快取，全部重問")
    ap.add_argument("--list", action="store_true", help="列出所有階段")
    ap.add_argument("--check", action="store_true",
                    help="只做開跑前體檢，不執行任何階段")
    ap.add_argument("--probe", action="store_true",
                    help="產生最小 FCPXML（只有一段主畫面）用來排查 FCP 匯入問題")
    args = ap.parse_args()

    if args.list:
        print("階段一覽：\n")
        for num, name, desc, _, needs in STAGES:
            print(f"  {num}  {name:<11} {desc}" + ("   [需要 Claude]" if needs else ""))
        return 0

    try:
        cfg = load_config(args.config)
    except StageError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 2

    if args.check:
        from pipeline.preflight import check
        return check(cfg)

    if args.probe:
        try:
            s08_fcpxml.build_probe(cfg)
        except StageError as exc:
            print(f"❌ {exc}", file=sys.stderr)
            return 1
        return 0

    if args.no_cache:
        cfg.data["claude"]["use_cache"] = False

    selected = _select(args)
    if not selected:
        print("❌ 沒有選到任何階段", file=sys.stderr)
        return 2

    claude = None
    if any(s[4] for s in selected):
        try:
            claude = ClaudeClient(cfg)
            log("init", f"Claude backend = {claude.backend}")
        except StageError as exc:
            print(f"❌ {exc}", file=sys.stderr)
            return 2

    log("init", f"專案：{cfg.get('project.name')}｜工作目錄 {cfg.build}")
    for num, name, desc, mod, _ in selected:
        print()
        log(num, f"=== {desc} ===")
        try:
            mod.run_stage(cfg, claude)
        except StageError as exc:
            print(f"\n❌ 階段 {num} ({name}) 失敗：{exc}", file=sys.stderr)
            return 1
        except KeyboardInterrupt:
            print("\n中斷。已完成的階段都存在 build/，可以用 --from 接著跑。", file=sys.stderr)
            return 130
        except Exception as exc:  # noqa: BLE001
            print(f"\n❌ 階段 {num} ({name}) 發生未預期的錯誤：{exc}\n", file=sys.stderr)
            traceback.print_exc()
            return 1

    print()
    if claude:
        log("done", claude.summary())
    _final_report(cfg)
    return 0


def _select(args) -> list:
    if args.only:
        out = []
        for key in args.only:
            hit = _BY_KEY.get(key)
            if hit is None:
                raise SystemExit(f"❌ 不認識的階段：{key}（用 --list 看可用的）")
            out.append(hit)
        return sorted(set(out), key=lambda s: s[0])
    lo = _BY_KEY[args.start][0] if args.start else "01"
    hi = _BY_KEY[args.end][0] if args.end else "10"
    if args.start and args.start not in _BY_KEY:
        raise SystemExit(f"❌ 不認識的階段：{args.start}")
    return [s for s in STAGES if lo <= s[0] <= hi]


def _final_report(cfg) -> None:
    b = cfg.build
    print("\n" + "═" * 58)
    print("  完成。接下來在 Mac 上這樣做：")
    print("═" * 58)
    items = [
        ("08_timeline.fcpxml", "Final Cut Pro → 檔案 → 匯入 → XML"),
        ("subtitles_zh-Hant.srt", "上傳到 YouTube 當中文隱藏式字幕"),
        ("subtitles_en.srt", "上傳到 YouTube 當英文隱藏式字幕"),
        ("09_description.md", "標題／說明欄／章節／人名，直接複製貼上"),
        ("10_thumbnail.png", "封面圖"),
        ("03_cuts.txt", "剪掉了哪些話 —— 進 FCP 前建議掃一眼"),
    ]
    for name, note in items:
        p = b / name
        mark = "✅" if p.exists() else "—"
        print(f"  {mark}  {name:<24} {note}")
    print("═" * 58)


if __name__ == "__main__":
    raise SystemExit(main())
