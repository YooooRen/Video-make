#!/usr/bin/env python3
"""
端到端煙霧測試：用合成素材與假的 Claude 回覆跑完 stage 03–06，
驗證時間軸數學、字幕、說明動畫、FCPXML 結構與各階段的資料交接都正確。

    python tests/smoke_test.py            # 跑完自動清理
    python tests/smoke_test.py --keep     # 保留產出以便檢查

不需要 Claude、不需要 GPU，只需要 ffmpeg。
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline import (s01_ingest, s03_clean, s04_subtitles,          # noqa: E402
                      s05_explainers, s06_fcpxml)
from pipeline.config import load_config                              # noqa: E402
from pipeline.timeline import EditMap, Rate, build_keeps             # noqa: E402
from pipeline.util import parse_timecode, write_json                 # noqa: E402

FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
FPS_N, FPS_D = 30000, 1001
TIMECODE = "21:43:27;18"        # drop frame，與使用者實際遇到的檔案一致
DUR = 60.0

_failures: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("  ✅ " if cond else "  ❌ ") + msg)
    if not cond:
        _failures.append(msg)


def secs(v: str) -> float:
    v = (v or "0s").rstrip("s")
    if "/" in v:
        n, d = v.split("/")
        return int(n) / int(d)
    return float(v or 0)


# ------------------------------------------------------------ 假的 Claude --

class FakeClaude:
    """依 prompt 內容回傳合理的假資料，讓所有階段都能跑完。"""

    backend = "fake"
    calls = 0
    cache_hits = 0

    def __init__(self) -> None:
        self.translate_prompts: list[str] = []

    def summary(self) -> str:
        return f"FakeClaude 呼叫 {self.calls} 次"

    def ask(self, prompt, *, system="", images=(), label="") -> str:
        return json.dumps(self.ask_json(prompt, label=label), ensure_ascii=False)

    def ask_json(self, prompt, *, system="", images=(), label="", **kw):
        self.calls += 1
        if "標出**應該從影片中剪掉**" in prompt:
            # 刪掉每個 chunk 裡的第一個字，模擬去結巴
            idx = [int(t.split(":")[0]) for t in prompt.split()
                   if ":" in t and t.split(":")[0].isdigit()]
            return {"removals": [{"from": idx[0], "to": idx[0],
                                  "kind": "filler", "reason": "um"}]} if idx else {"removals": []}
        if "航海／帆船專有名詞" in prompt:
            return [{"en": "beam reach", "zh": "橫風航行", "note": "風從側面來"},
                    {"en": "tacking", "zh": "搶風轉向", "note": "逆風之字前進"}]
        if "字幕譯者" in prompt:
            self.translate_prompts.append(prompt)
            ids = [int(h) for h in
                   (ln.split("|")[0].strip() for ln in prompt.splitlines())
                   if h.isdigit()]
            return [{"id": i, "zh": f"這是第{i}句中文字幕"} for i in ids]
        if "說明小卡／航線圖" in prompt:
            return [
                {"kind": "route", "at": 12.0, "title_zh": "馬公 → 綠島",
                 "note_zh": "全程約 180 海里",
                 "waypoints": [{"name_zh": "Magong", "lat": 23.566, "lon": 119.566},
                               {"name_zh": "Ludao", "lat": 22.660, "lon": 121.487}]},
                {"kind": "term", "at": 14.0, "term_en": "beam reach",
                 "term_zh": "Beam Reach", "explain_zh": "wind from the side"},
            ]
        if "主持人還是受訪的帆船船長" in prompt:
            return {"SPEAKER_00": {"role": "guest", "name": "Captain Test"},
                    "SPEAKER_01": {"role": "host", "name": "Host Test"}}
        raise AssertionError(f"FakeClaude 沒有對應的分支，label={label}\n{prompt[:300]}")


# --------------------------------------------------------------- 造素材 ---

def make_media(d: Path) -> Path:
    src = d / "interview.mov"
    # 刻意寫入 drop-frame 時間碼，重現相機檔會帶「拍攝當下時間」的情況 ——
    # 這正是讓 Final Cut Pro 說「沒有個別媒體，剪輯無效」的原因
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error",
         "-f", "lavfi", "-i", f"testsrc2=size=1280x720:rate={FPS_N}/{FPS_D}:duration={DUR}",
         "-f", "lavfi", "-i", f"sine=frequency=440:duration={DUR}",
         "-timecode", TIMECODE,
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(src)],
        check=True)
    return src


def make_transcript(build: Path) -> dict:
    """造一份有兩個說話者、含長停頓與結巴的逐字稿。"""
    words, segments = [], []
    t = 0.5
    for si in range(12):
        spk = "SPEAKER_00" if si % 2 == 0 else "SPEAKER_01"
        w0 = len(words)
        for wi in range(20):
            dur = 0.22
            words.append({"i": len(words), "w": ("um" if wi == 0 else f"word{si}_{wi}"),
                          "s": round(t, 3), "e": round(t + dur, 3), "p": 0.95, "spk": spk})
            t += dur + 0.05
        if si % 3 == 2:
            t += 1.6            # 故意插入一段過久停頓
        segments.append({"id": si, "s": words[w0]["s"], "e": words[-1]["e"],
                         "w0": w0, "w1": len(words) - 1, "spk": spk,
                         "text": " ".join(w["w"] for w in words[w0:])})
    data = {"language": "en", "duration": DUR,
            "speakers": {"SPEAKER_00": {"id": "SPEAKER_00", "name": "Captain", "role": "guest"},
                         "SPEAKER_01": {"id": "SPEAKER_01", "name": "Host", "role": "host"}},
            "words": words, "segments": segments}
    write_json(build / "02_transcript.json", data)
    return data


# ------------------------------------------------------------ 單元檢查 ----

def test_timeline_math() -> None:
    print("\n▶ 時間軸數學")
    rate = Rate(FPS_N, FPS_D)
    check(rate.frame_duration == "1001/30000s", "29.97fps 的 frameDuration 正確")
    check(rate.time(0) == "0s", "0 秒輸出 0s")
    ok = all(int(rate.time(v).rstrip("s").split("/")[0]) % FPS_D == 0
             for v in (1.0, 3.337, 12.5, 59.999))
    check(ok, "分數時間都是 frameDuration 的整數倍")

    keeps, _ = build_keeps(100.0, [(10, 12), (30, 30.05), (50, 55)],
                           rate=rate, min_removal=0.1, min_keep=0.3, pad=0.0)
    check(len(keeps) == 3, f"太短的刪除被忽略（保留 {len(keeps)} 段）")
    emap = EditMap(keeps, rate)
    check(abs(emap.duration - 93.0) < 0.05, f"剪後長度 {emap.duration:.2f} ≈ 93 秒")
    check(emap.src_to_edit(11) is None, "被剪掉的時間回傳 None")
    check(abs(emap.src_to_edit(20) - 18.0) < 0.05, "剪輯點之後的時間正確位移")
    check(abs(emap.edit_to_src(emap.src_to_edit(20)) - 20) < 0.05, "來回換算可逆")

    want, drop = parse_timecode(TIMECODE, FPS_N, FPS_D)
    check(drop and abs(want - 78207.5294) < 1e-3,
          f"drop-frame 時間碼換算正確（{TIMECODE} → {want:.4f}s）")


def test_fcpxml(path: Path, rate: Rate) -> None:
    print("\n▶ FCPXML 結構")
    raw = path.read_text()
    check(raw.startswith('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE fcpxml>'),
          "檔頭與 DOCTYPE 正確")
    root = ET.fromstring(raw[raw.index("<fcpxml"):])
    check(root.get("version") == "1.11", "FCPXML 版本 1.11")

    res = root.find("resources")
    check(res is not None and len(res.findall("asset")) >= 2,
          f"resources 有 {len(res.findall('asset'))} 個素材")
    named = [f.get("name") for f in res.findall("format") if f.get("name")]
    check(not named, "format 沒有硬拼的 name 屬性" + (f"（發現：{named}）" if named else ""))
    for a in res.findall("asset"):
        mr = a.find("media-rep")
        check(mr is not None and mr.get("src", "").startswith("file://"),
              f'素材 {a.get("name")} 有合法的 media-rep')

    clips = root.find(".//spine").findall("asset-clip")
    check(len(clips) > 1, f"主軸被切成 {len(clips)} 段（停頓與結巴已剪掉）")

    cursor, contiguous, monotonic, prev = 0.0, True, True, -1.0
    for c in clips:
        off, dur = secs(c.get("offset")), secs(c.get("duration"))
        contiguous &= abs(off - cursor) < 1e-6
        monotonic &= off > prev
        prev, cursor = off, off + dur
    check(monotonic, "spine clip 的 offset 嚴格遞增")
    check(contiguous, "spine clip 首尾相接，沒有空隙")
    check(abs(secs(root.find(".//sequence").get("duration")) - cursor) < 0.05,
          "sequence 長度與所有片段總和一致")

    # 時間碼：素材的 start 必須是媒體真正的起點，clip 也要落在那之後
    want, drop = parse_timecode(TIMECODE, FPS_N, FPS_D)
    main = res.findall("asset")[0]
    a_start, a_dur = secs(main.get("start")), secs(main.get("duration"))
    check(abs(a_start - want) < 0.01, f"素材 start = 時間碼換算值 {want:.2f}s")
    check(all(a_start - 1e-6 <= secs(c.get("start")) <= a_start + a_dur + 1e-6
              for c in clips), "每個 clip 的 start 都落在素材真實的媒體範圍內")
    check(all(c.get("tcFormat") == ("DF" if drop else "NDF") for c in clips),
          f"clip 的 tcFormat 標成 {'DF' if drop else 'NDF'}")
    check(all(c.get("audioRole") for c in clips), "主畫面掛在 dialogue 音訊角色上")

    # 連接的說明動畫：offset 要落在所屬 clip 的時間範圍內
    lanes, in_range, n_conn = set(), True, 0
    for c in clips:
        c_start, c_dur = secs(c.get("start")), secs(c.get("duration"))
        for child in c:
            if child.get("lane") is None:
                continue
            lanes.add(child.get("lane"))
            n_conn += 1
            if not (c_start - 1e-6 <= secs(child.get("offset")) <= c_start + c_dur + 1e-6):
                in_range = False
    check(in_range, f"{n_conn} 個連接項目的 offset 都在所屬 clip 的範圍內")
    check("1" in lanes, "說明動畫在 lane 1")
    check(not any(c.get("role") for c in root.iter("asset-clip")),
          "沒有任何 asset-clip 帶 role 屬性（它只有 audioRole / videoRole）")

    from pipeline.s06_fcpxml import (_ALLOWED_ATTRS, _validate_frame_grid,
                                     _validate_ranges)
    offenders = []
    for el in root.iter():
        allowed = _ALLOWED_ATTRS.get(el.tag)
        if allowed is None:
            offenders.append(f"<{el.tag}> 未知元素")
            continue
        offenders += [f"<{el.tag}> {a}" for a in el.attrib if a not in allowed]
    check(not offenders, "所有屬性名都符合 FCPXML DTD" +
          (f"（違規：{sorted(set(offenders))[:4]}）" if offenders else ""))

    ranges = {a.get("id"): (secs(a.get("start")), secs(a.get("duration")), a.get("name"))
              for a in res.findall("asset")}
    probs = _validate_ranges(root, ranges)
    check(not probs, "所有 clip 都落在素材的媒體範圍內" +
          (f"（越界：{probs[:2]}）" if probs else ""))
    grid = _validate_frame_grid(root, rate)
    check(not grid, "所有 offset / duration 都對齊序列的影格網格" +
          (f"（未對齊：{grid[:2]}）" if grid else ""))


# ------------------------------------------------------------------ main --

def main() -> int:
    test_timeline_math()

    tmp = Path(tempfile.mkdtemp(prefix="pipeline-smoke-"))
    try:
        print(f"\n▶ 建立合成素材於 {tmp}")
        src = make_media(tmp)
        cfg_path = tmp / "project.yaml"
        cfg_path.write_text(f"""
project:
  name: "Smoke Test"
  source_video: "{src}"
  build_dir: "{tmp}/build"
explainers:
  font_path: "{FONT}"
  duration: 3.0
  min_gap: 5.0
  width: 640
  height: 360
  fps: 30
subtitles:
  fcp_captions: "both"
corrections:
  word0_1: "CORRECTED"
""", encoding="utf-8")
        cfg = load_config(cfg_path)
        claude = FakeClaude()

        print("\n▶ 逐階段執行")
        s01_ingest.run_stage(cfg, claude)
        make_transcript(cfg.build)
        for name, mod in (("03 剪停頓與結巴", s03_clean), ("04 字幕", s04_subtitles),
                          ("05 說明動畫", s05_explainers), ("06 FCPXML", s06_fcpxml)):
            mod.run_stage(cfg, claude)
            check(True, f"stage {name} 執行完成")

        b = cfg.build
        print("\n▶ 產出檔案")
        for f in ("03_cuts.json", "03_cuts.txt", "04_subtitles.json",
                  "04_glossary.txt", "subtitles_en.srt", "subtitles_zh-Hant.srt",
                  "subtitles_bilingual.srt", "05_explainers.json",
                  "06_timeline.fcpxml"):
            check((b / f).exists() and (b / f).stat().st_size > 0, f"{f} 已產生且非空")

        print("\n▶ 剪輯決策")
        cuts = json.loads((b / "03_cuts.json").read_text())
        check(cuts["duration_edit"] < cuts["duration_src"], "剪輯後確實變短了")
        check(cuts["stats"].get("pause", {}).get("count", 0) > 0, "有偵測到過久停頓")
        check(cuts["stats"].get("filler", {}).get("count", 0) > 0, "有偵測到結巴／贅詞")

        print("\n▶ 字幕")
        subs = json.loads((b / "04_subtitles.json").read_text())
        cues = subs["cues"]
        check(all(c["e"] > c["s"] for c in cues), "每則字幕結束時間都晚於開始時間")
        check(all(cues[i]["e"] <= cues[i + 1]["s"] + 1e-6 for i in range(len(cues) - 1)),
              "字幕之間沒有重疊")
        check(all(c["e"] <= subs["duration"] + 0.01 for c in cues), "字幕沒有超出片長")
        check(all(c["zh"] for c in cues), "每則字幕都有中文翻譯")
        srt = (b / "subtitles_zh-Hant.srt").read_text()
        check(srt.startswith("1\n") and "-->" in srt, "SRT 格式正確")

        en_srt = (b / "subtitles_en.srt").read_text()
        standalone = re.search(r"(?<![A-Za-z0-9])word0_1(?![A-Za-z0-9])", en_srt)
        check("CORRECTED" in en_srt, "corrections 有套用到英文字幕")
        check(standalone is None, "只換完整單字（word0_10 等更長的詞沒被誤傷）")
        check("word0_10" in en_srt, "更長的詞確實保持原樣")

        print("\n▶ 可編輯的詞彙表")
        gl = b / "04_glossary.txt"
        first = gl.read_text()
        check("beam reach" in first and "橫風航行" in first, "首次執行由 AI 建立詞彙表")
        check(first.startswith("#"), "檔案帶有說明用的註解開頭")
        # 模擬使用者編輯：改譯法、加自訂詞、用連續空白分欄、留一行註解
        gl.write_text("# 我自己加的註解\n"
                      "beam reach\t自訂譯法\t使用者改的\n"
                      "jury rig     應急帆裝     使用者新增的詞\n", encoding="utf-8")
        s04_subtitles.run_stage(cfg, claude)
        after = gl.read_text()
        check("自訂譯法" in after, "使用者的譯法沒有被 AI 覆蓋")
        check("橫風航行" not in after, "AI 沒有把同一個詞重複加回去")
        check("應急帆裝" in after, "使用者新增的詞保留下來")
        check("搶風轉向" in after, "AI 補上了使用者沒收錄的新詞")
        check(after.index("應急帆裝") < after.index("搶風轉向"),
              "使用者的詞排在前面，AI 補的接在後面")
        check(any("自訂譯法" in p for p in claude.translate_prompts),
              "編輯後的譯法有送進翻譯提示詞")

        print("\n▶ 說明動畫")
        expl = json.loads((b / "05_explainers.json").read_text())
        check(len(expl["items"]) == 2, "航線圖與名詞卡都算圖成功")
        kinds = {it["kind"] for it in expl["items"]}
        check(kinds == {"route", "term"}, f"兩種型態都有（{sorted(kinds)}）")
        for it in expl["items"]:
            p = Path(it["path"])
            check(p.exists() and p.stat().st_size > 1000,
                  f"{p.name} 是有內容的影片檔")
        ats = [it["at"] for it in expl["items"]]
        check(all(b_ - a >= 5.0 - 1e-6 for a, b_ in zip(ats, ats[1:])),
              "說明動畫之間有維持最小間隔")

        s06_fcpxml.run_stage(cfg, claude)
        test_fcpxml(b / "06_timeline.fcpxml", Rate(FPS_N, FPS_D))

        print("\n▶ 說明動畫的內容指紋")
        from pipeline.s05_explainers import _content_hash, _style_fingerprint
        style = _style_fingerprint(cfg)
        item = {"kind": "term", "term_en": "beam reach", "term_zh": "橫風航行",
                "explain_zh": "風從側面來", "duration": 6.0, "at": 10.0}
        h1 = _content_hash(item, style)
        check(_content_hash({**item, "at": 99.0}, style) == h1,
              "只改插入時間不會重新算圖（at 不影響畫面）")
        check(_content_hash({**item, "term_zh": "別的名詞"}, style) != h1,
              "改了內容就換一組指紋（不會誤用舊的算圖）")
        check(_content_hash(item, style + "x") != h1,
              "改了樣式設定也會重新算圖")
        movs = sorted(p.name for p in (cfg.build / "explainers").glob("*.mov"))
        check(all(len(m.split("_")) >= 3 for m in movs),
              f"算圖檔名都帶內容指紋：{movs}")

        print("\n▶ 最小探針 (--probe)")
        s06_fcpxml.build_probe(cfg)
        pr = b / "probe_minimal.fcpxml"
        check(pr.exists(), "probe_minimal.fcpxml 已產生")
        praw = pr.read_text()
        proot = ET.fromstring(praw[praw.index("<fcpxml"):])
        pclips = proot.findall(".//spine/asset-clip")
        check(len(pclips) == 1, f"探針只有 1 段主畫面（實際 {len(pclips)}）")
        check(not list(proot.iter("caption")), "探針沒有字幕")
        check(len(list(pclips[0])) == 0, "探針的 clip 沒有任何連接項目")
        check(pclips[0].get("start") != "0s", "探針也套用了時間碼起點")

        print("\n▶ 換來源影片時的快取失效")
        # 重現實際踩到的情境：新來源的修改時間「比既有音軌還舊」。
        # 若用 mtime 比大小判斷，就會誤以為音軌還新、繼續沿用上一支片的音訊。
        import os
        from pipeline.util import media_info
        before = media_info(cfg.build / "audio.wav")["duration"]
        src2 = tmp / "interview2.mov"
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
             "-i", "testsrc2=size=640x360:rate=30000/1001:duration=8",
             "-f", "lavfi", "-i", "sine=duration=8",
             "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
             "-shortest", str(src2)], check=True)
        old_time = (cfg.build / "audio.wav").stat().st_mtime - 86400
        os.utime(src2, (old_time, old_time))
        check(src2.stat().st_mtime < (cfg.build / "audio.wav").stat().st_mtime,
              "新來源的修改時間確實比既有音軌舊（重現問題情境）")

        cfg2_path = tmp / "project2.yaml"
        cfg2_path.write_text(
            f'project:\n  source_video: "{src2}"\n  build_dir: "{tmp}/build"\n',
            encoding="utf-8")
        cfg2 = load_config(cfg2_path)
        s01_ingest.run_stage(cfg2, claude)
        after = media_info(cfg.build / "audio.wav")["duration"]
        check(abs(before - DUR) < 1.0, f"換片前音軌是第一支影片（{before:.1f}s）")
        check(abs(after - 8.0) < 1.0,
              f"換片後音軌重新抽取自新影片（{after:.1f}s），沒有沿用舊的")

    finally:
        if "--keep" in sys.argv:
            print(f"\n▶ 產出保留在 {tmp}")
        else:
            shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "═" * 52)
    if _failures:
        print(f"  ❌ {len(_failures)} 項檢查失敗：")
        for f in _failures:
            print(f"     - {f}")
        print("═" * 52)
        return 1
    print("  ✅ 全部通過")
    print("═" * 52)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
