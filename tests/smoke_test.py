#!/usr/bin/env python3
"""
端到端煙霧測試：用合成素材與假的 Claude 回覆跑完 stage 03–10，
驗證時間軸數學、FCPXML 結構與各階段的資料交接都正確。

    python tests/smoke_test.py            # 跑完自動清理
    python tests/smoke_test.py --keep     # 保留產出以便檢查

不需要 Claude、不需要 GPU，只需要 ffmpeg。
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline import (s01_ingest, s03_clean, s04_subtitles, s05_broll,     # noqa: E402
                      s06_explainers, s07_framing, s08_fcpxml, s09_package,
                      s10_thumbnail)
from pipeline.config import load_config                                     # noqa: E402
from pipeline.timeline import EditMap, Rate, build_keeps                    # noqa: E402
from pipeline.util import write_json                                        # noqa: E402

FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
FPS_N, FPS_D = 30000, 1001
DUR = 60.0

_failures: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("  ✅ " if cond else "  ❌ ") + msg)
    if not cond:
        _failures.append(msg)


# ------------------------------------------------------------ 假的 Claude --

class FakeClaude:
    """依 prompt 內容回傳合理的假資料，讓所有階段都能跑完。"""

    backend = "fake"
    calls = 0
    cache_hits = 0

    def summary(self) -> str:
        return f"FakeClaude 呼叫 {self.calls} 次"

    def ask(self, prompt, *, system="", images=(), label="") -> str:
        return json.dumps(self.ask_json(prompt, label=label), ensure_ascii=False)

    def ask_json(self, prompt, *, system="", images=(), label="", **kw):
        self.calls += 1
        if "標出**應該從影片中剪掉**" in prompt:
            # 刪掉每個 chunk 裡的第一個字，模擬去贅詞
            idx = [int(t.split(":")[0]) for t in prompt.split()
                   if ":" in t and t.split(":")[0].isdigit()]
            if not idx:
                return {"removals": []}
            return {"removals": [{"from": idx[0], "to": idx[0],
                                  "kind": "filler", "reason": "um"}]}
        if "航海／帆船專有名詞" in prompt:
            return [{"en": "beam reach", "zh": "橫風航行", "note": "風從側面來"}]
        if "字幕譯者" in prompt:
            ids = []
            for line in prompt.splitlines():
                head = line.split("|")[0].strip()
                if head.isdigit():
                    ids.append(int(head))
            return [{"id": i, "zh": f"這是第{i}句中文字幕"} for i in ids]
        if "縮圖拼貼" in prompt:
            return {"summary": "帆船在海上航行", "tags": ["帆船", "海"],
                    "subjects": ["帆船"], "shot_type": "wide", "motion": "slow",
                    "time_of_day": "day", "usable": True, "quality_note": ""}
        if "在哪些時間點插入 B-roll" in prompt:
            return [{"at": 12.0, "duration": 4.0, "asset": "A000", "reason": "講到出海"},
                    {"at": 30.0, "duration": 3.5, "asset": "P000", "reason": "講到港口"},
                    {"at": 31.0, "duration": 3.0, "asset": "A001", "reason": "應被間隔規則濾掉"}]
        if "說明小卡／航線圖" in prompt:
            return [
                {"kind": "route", "at": 20.0, "title_zh": "馬公 → 綠島",
                 "note_zh": "全程約 180 海里",
                 "waypoints": [{"name_zh": "Magong", "lat": 23.566, "lon": 119.566},
                               {"name_zh": "Ludao", "lat": 22.660, "lon": 121.487}]},
                {"kind": "term", "at": 40.0, "term_en": "beam reach",
                 "term_zh": "Beam Reach", "explain_zh": "wind from the side"},
            ]
        if "坐在畫面的哪個位置" in prompt:
            return {"SPEAKER_00": {"x": 0.28, "y": 0.40},
                    "SPEAKER_01": {"x": 0.72, "y": 0.42}}
        if "YouTube 頻道編輯" in prompt:
            return {"titles": ["Sailing Talk"], "hook": "hook",
                    "summary_zh": "摘要", "summary_en": "Summary",
                    "people": [{"name": "Bernard Moitessier", "role": "navigator",
                                "context": "mentioned"}],
                    "places": [{"name_en": "Magong", "name_zh": "Magong"}],
                    "boats": [{"name": "Aeolus", "note": "40ft"}],
                    "chapters": [{"t": 0, "title": "Opening"},
                                 {"t": 5, "title": "太近應被濾掉"},
                                 {"t": 20, "title": "Middle"},
                                 {"t": 40, "title": "End"}],
                    "terms": [{"en": "beam reach", "zh": "橫風航行"}],
                    "tags": ["sailing"], "hashtags": ["#sailing"]}
        if "最適合當 YouTube 封面" in prompt:
            return {"index": 3, "face": {"x": 0.62, "y": 0.38}, "why": "測試"}
        if "主持人還是受訪的帆船船長" in prompt:
            return {"SPEAKER_00": {"role": "guest", "name": "Captain Test"},
                    "SPEAKER_01": {"role": "host", "name": "Host Test"}}
        raise AssertionError(f"FakeClaude 沒有對應的分支，label={label}\n{prompt[:300]}")


# --------------------------------------------------------------- 造素材 ---

def make_media(d: Path) -> tuple[Path, Path]:
    src = d / "interview.mov"
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error",
         "-f", "lavfi", "-i", f"testsrc2=size=1280x720:rate={FPS_N}/{FPS_D}:duration={DUR}",
         "-f", "lavfi", "-i", f"sine=frequency=440:duration={DUR}",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(src)],
        check=True)

    media = d / "footage"
    media.mkdir()
    for i in range(2):
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error",
             "-f", "lavfi", "-i", f"testsrc=size=1920x1080:rate=25:duration=10",
             "-c:v", "libx264", "-pix_fmt", "yuv420p", str(media / f"clip{i}.mp4")],
            check=True)
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
         "-i", "testsrc=size=1600x900:rate=1:duration=1",
         "-frames:v", "1", str(media / "photo0.jpg")], check=True)
    return src, media


def make_transcript(d: Path, build: Path) -> dict:
    """造一份有兩個說話者、含長停頓的逐字稿。"""
    words, segments = [], []
    t = 0.5
    for si in range(12):
        spk = "SPEAKER_00" if si % 2 == 0 else "SPEAKER_01"
        w0 = len(words)
        n = 20                      # 每段約 5.4 秒，接近真實訪談的一輪發言
        for wi in range(n):
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
    # 任何輸出的分數時間都必須是幀的整數倍
    ok = True
    for v in (1.0, 3.337, 12.5, 59.999):
        num = int(rate.time(v).rstrip("s").split("/")[0])
        ok &= (num % FPS_D == 0)
    check(ok, "分數時間都是 frameDuration 的整數倍")

    keeps, eff = build_keeps(100.0, [(10, 12), (30, 30.05), (50, 55)],
                             rate=rate, min_removal=0.1, min_keep=0.3, pad=0.0)
    check(len(keeps) == 3, f"太短的刪除被忽略（保留 {len(keeps)} 段）")
    emap = EditMap(keeps, rate)
    check(abs(emap.duration - 93.0) < 0.05, f"剪後長度 {emap.duration:.2f} ≈ 93 秒")
    check(emap.src_to_edit(5) is not None and abs(emap.src_to_edit(5) - 5) < 0.05,
          "剪輯點之前的時間不變")
    check(emap.src_to_edit(11) is None, "被剪掉的時間回傳 None")
    check(abs(emap.src_to_edit(20) - 18.0) < 0.05, "剪輯點之後的時間正確位移")
    check(abs(emap.edit_to_src(emap.src_to_edit(20)) - 20) < 0.05, "來回換算可逆")


def test_fcpxml(path: Path, rate: Rate) -> None:
    print("\n▶ FCPXML 結構")
    raw = path.read_text()
    check(raw.startswith('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE fcpxml>'),
          "檔頭與 DOCTYPE 正確")
    root = ET.fromstring(raw[raw.index("<fcpxml"):])
    check(root.get("version") == "1.11", "FCPXML 版本 1.11")

    res = root.find("resources")
    check(res is not None and len(res.findall("asset")) >= 3,
          f"resources 有 {len(res.findall('asset'))} 個素材")
    for a in res.findall("asset"):
        check(a.find("media-rep") is not None and
              a.find("media-rep").get("src", "").startswith("file://"),
              f'素材 {a.get("name")} 有合法的 media-rep')

    spine = root.find(".//spine")
    clips = spine.findall("asset-clip")
    check(len(clips) > 1, f"主軸被切成 {len(clips)} 段（贅詞與停頓已剪掉）")

    # offset 必須嚴格遞增，且等於前一段的 offset + duration（中間不能有洞）
    def secs(v: str) -> float:
        v = v.rstrip("s")
        if "/" in v:
            n, d = v.split("/")
            return int(n) / int(d)
        return float(v)

    cursor, contiguous, monotonic = 0.0, True, True
    prev = -1.0
    for c in clips:
        off = secs(c.get("offset"))
        dur = secs(c.get("duration"))
        contiguous &= abs(off - cursor) < 1e-6
        monotonic &= off > prev
        prev, cursor = off, off + dur
    check(monotonic, "spine clip 的 offset 嚴格遞增")
    check(contiguous, "spine clip 首尾相接，沒有空隙")

    seq = root.find(".//sequence")
    check(abs(secs(seq.get("duration")) - cursor) < 0.05,
          f"sequence 長度 {secs(seq.get('duration')):.2f}s 與所有片段總和一致")

    # 連接的素材：offset 必須落在所屬 clip 的內部時間範圍內
    lanes, cap_langs, in_range = set(), set(), True
    n_conn = 0
    for c in clips:
        c_start, c_dur = secs(c.get("start")), secs(c.get("duration"))
        for child in c:
            lane = child.get("lane")
            if lane is None:
                continue
            lanes.add(lane)
            off = secs(child.get("offset"))
            if not (c_start - 1e-6 <= off <= c_start + c_dur + 1e-6):
                in_range = False
            n_conn += 1
            if child.tag == "caption":
                cap_langs.add(child.get("role"))
    check(in_range, f"{n_conn} 個連接素材的 offset 都在所屬 clip 的時間範圍內")
    check("1" in lanes, "B-roll 在 lane 1")
    check("2" in lanes, "說明短片在 lane 2")
    check(len(cap_langs) == 2, f"兩條字幕軌：{sorted(cap_langs)}")
    check(any("zh-Hant" in r for r in cap_langs) and any(".en" in r for r in cap_langs),
          "中英文字幕 role 都存在")

    # adjust-transform 必須是 clip 的第一個子元素
    order_ok = True
    n_tf = 0
    for c in clips:
        kids = list(c)
        for i, k in enumerate(kids):
            if k.tag == "adjust-transform":
                n_tf += 1
                order_ok &= (i == 0)
    check(order_ok and n_tf > 0, f"{n_tf} 個 adjust-transform 都排在 clip 的最前面")

    # B-roll 不能帶進自己的聲音
    bro = [e for c in clips for e in c.findall("asset-clip") if e.get("lane") == "1"]
    check(all(e.get("srcEnable") == "video" for e in bro),
          f"{len(bro)} 段 B-roll 影片都只用畫面不用聲音")


# ------------------------------------------------------------------ main --

def main() -> int:
    test_timeline_math()

    tmp = Path(tempfile.mkdtemp(prefix="pipeline-smoke-"))
    try:
        print(f"\n▶ 建立合成素材於 {tmp}")
        src, media = make_media(tmp)
        cfg_path = tmp / "project.yaml"
        cfg_path.write_text(f"""
project:
  name: "Smoke Test"
  source_video: "{src}"
  media_dir: "{media}"
  build_dir: "{tmp}/build"
broll:
  min_gap: 12.0
  protect_head: 5.0
explainers:
  font_path: "{FONT}"
  duration: 3.0
  width: 640
  height: 360
  fps: 12
thumbnail:
  candidates: 6
""", encoding="utf-8")
        cfg = load_config(cfg_path)
        claude = FakeClaude()

        print("\n▶ 逐階段執行")
        s01_ingest.run_stage(cfg, claude)
        make_transcript(tmp, cfg.build)
        for name, mod in (("03 去贅詞", s03_clean), ("04 字幕", s04_subtitles),
                          ("05 B-roll", s05_broll), ("06 說明短片", s06_explainers),
                          ("07 鏡位", s07_framing), ("08 FCPXML", s08_fcpxml),
                          ("09 說明欄", s09_package), ("10 封面圖", s10_thumbnail)):
            mod.run_stage(cfg, claude)
            check(True, f"stage {name} 執行完成")

        print("\n▶ 產出檔案")
        b = cfg.build
        for f in ("03_cuts.json", "04_subtitles.json", "subtitles_en.srt",
                  "subtitles_zh-Hant.srt", "subtitles_bilingual.srt",
                  "05_broll.json", "06_explainers.json", "07_framing.json",
                  "08_timeline.fcpxml", "09_description.md", "10_thumbnail.png"):
            check((b / f).exists() and (b / f).stat().st_size > 0, f"{f} 已產生且非空")

        cuts = json.loads((b / "03_cuts.json").read_text())
        check(cuts["duration_edit"] < cuts["duration_src"], "剪輯後確實變短了")
        check(cuts["stats"].get("pause", {}).get("count", 0) > 0, "有偵測到過久停頓")
        check(cuts["stats"].get("filler", {}).get("count", 0) > 0, "有偵測到發語詞")

        subs = json.loads((b / "04_subtitles.json").read_text())
        cues = subs["cues"]
        check(all(c["e"] > c["s"] for c in cues), "每則字幕結束時間都晚於開始時間")
        check(all(cues[i]["e"] <= cues[i + 1]["s"] + 1e-6 for i in range(len(cues) - 1)),
              "字幕之間沒有重疊")
        check(all(c["e"] <= subs["duration"] + 0.01 for c in cues), "字幕沒有超出片長")
        check(all(c["zh"] for c in cues), "每則字幕都有中文翻譯")

        bro = json.loads((b / "05_broll.json").read_text())
        check(len(bro["placements"]) == 2,
              f"B-roll 間隔規則生效（3 個建議 → 排入 {len(bro['placements'])} 個）")

        expl = json.loads((b / "06_explainers.json").read_text())
        check(len(expl["items"]) == 2, "航線圖與名詞卡都算圖成功")
        for it in expl["items"]:
            check(Path(it["path"]).exists() and Path(it["path"]).stat().st_size > 1000,
                  f'{Path(it["path"]).name} 是有內容的影片檔')

        fr = json.loads((b / "07_framing.json").read_text())
        check(len(fr["shots"]) > 0 and any(s["mode"] == "punch" for s in fr["shots"]),
              f'產生 {len(fr["shots"])} 個鏡位且含推近特寫')
        check(any(s["mode"] == "wide" for s in fr["shots"]), "有回到全景的鏡位")

        pkg = json.loads((b / "09_package.json").read_text())
        check(pkg["chapters"][0]["t"] == 0, "第一個章節從 0:00 開始")
        check(len(pkg["chapters"]) == 3, "間隔太近的章節已被濾掉")
        desc = (b / "09_description.md").read_text()
        check("Bernard Moitessier" in desc, "提到的人名有進說明欄")
        check("subtitles_zh-Hant.srt" in desc, "說明欄有字幕上傳指示")

        srt = (b / "subtitles_zh-Hant.srt").read_text()
        check(srt.startswith("1\n") and "-->" in srt, "SRT 格式正確")

        test_fcpxml(b / "08_timeline.fcpxml", Rate(FPS_N, FPS_D))
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
