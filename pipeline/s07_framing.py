"""Stage 07：依說話者自動決定鏡位（全景／推近特寫），輸出畫面變形決策。"""
from __future__ import annotations

import math
from pathlib import Path

from .timeline import EditMap, Rate
from .util import clamp, log, read_json, run, warn, write_json


def run_stage(cfg, claude=None) -> dict:
    tr = read_json(cfg.build_file("02_transcript.json"))
    cuts = read_json(cfg.build_file("03_cuts.json"))
    ingest = read_json(cfg.build_file("01_ingest.json"))
    seq = ingest["sequence"]
    rate = Rate(seq["fps_num"], seq["fps_den"])
    emap = EditMap([tuple(k) for k in cuts["keeps"]], rate)
    empty = {"positions": {}, "shots": []}

    if not cfg.get("framing.enabled", True):
        log("07", "framing.enabled=false，全片維持原始構圖")
        return _save(cfg, empty)

    speakers = sorted({s.get("spk") for s in tr["segments"] if s.get("spk")})
    if len(speakers) < 2:
        warn("07", "只偵測到一位說話者，不做分鏡（維持原始構圖）")
        return _save(cfg, empty)

    positions = _positions(cfg, claude, tr, ingest, emap)
    if not positions:
        warn("07", "無法判斷兩人在畫面中的位置，全片維持原始構圖")
        return _save(cfg, empty)

    turns = _turns(tr["words"], emap)
    shots = _shots(cfg, turns, positions, seq)
    log("07", f"產生 {len(shots)} 個鏡位（其中 "
              f"{sum(1 for s in shots if s['mode']=='punch')} 個推近特寫）")
    return _save(cfg, {"positions": positions, "shots": shots})


def _save(cfg, data) -> dict:
    write_json(cfg.build_file("07_framing.json"), data)
    return data


# ---------------------------------------------------- 誰坐在畫面的哪一邊 ---

def _positions(cfg, claude, tr, ingest, emap) -> dict:
    """回傳 {SPEAKER_xx: {"x": 0.28, "y": 0.42}}（畫面正規化座標）。"""
    manual = cfg.get("framing.speaker_positions", {}) or {}
    if manual:
        out = {}
        for sid, val in manual.items():
            if isinstance(val, dict):
                out[sid] = {"x": float(val.get("x", 0.5)), "y": float(val.get("y", 0.45))}
            else:
                x = {"left": 0.27, "center": 0.5, "right": 0.73}.get(str(val).lower(), 0.5)
                out[sid] = {"x": x, "y": 0.42}
        log("07", f"使用 project.yaml 指定的人物位置：{manual}")
        return out
    if claude is None:
        return {}

    # 取每位說話者最長的幾段獨白，各抽一張畫面
    src = ingest["source"]["path"]
    frames_dir = cfg.build / "framing_frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    per = max(2, int(cfg.get("framing.probe_frames", 12)) // 2)

    shots_by_spk: dict[str, list[str]] = {}
    for sid in sorted({s.get("spk") for s in tr["segments"] if s.get("spk")}):
        segs = [s for s in tr["segments"] if s.get("spk") == sid]
        segs.sort(key=lambda s: s["e"] - s["s"], reverse=True)
        paths = []
        for k, seg in enumerate(segs[:per]):
            t = (seg["s"] + seg["e"]) / 2
            if emap.src_to_edit(t) is None:       # 這段被剪掉了，換一段
                continue
            out = frames_dir / f"{sid}_{k}.jpg"
            if not out.exists():
                try:
                    run(["ffmpeg", "-y", "-v", "error", "-ss", f"{t:.2f}", "-i", src,
                         "-frames:v", "1", "-vf", "scale=960:-2", str(out)])
                except Exception as exc:  # noqa: BLE001
                    warn("07", f"抽格失敗 {t:.1f}s：{exc}")
                    continue
            paths.append(str(out))
        if paths:
            shots_by_spk[sid] = paths

    if len(shots_by_spk) < 2:
        return {}

    listing = []
    images = []
    for sid, paths in shots_by_spk.items():
        for p in paths:
            images.append(p)
            listing.append(f"- {Path(p).name} → 這一格正在說話的是 {sid}")

    prompt = (
        "這些是同一支雙人訪談影片在不同時間點的畫面截圖。攝影機固定不動，兩個人坐在畫面的不同位置。\n"
        "每張圖對應的『當下正在說話的人』代號如下：\n" + "\n".join(listing) + "\n\n"
        "請判斷每個代號的人**坐在畫面的哪個位置**。\n"
        "提示：正在說話的人通常嘴巴張開、身體較前傾、視線朝向對方。\n"
        "請用正規化座標回答：x=0 是畫面最左邊、x=1 是最右邊；y=0 是最上面、y=1 是最下面。\n"
        "座標請指向那個人的**臉部中心**。\n\n"
        '只輸出 JSON：{"SPEAKER_00": {"x": 0.28, "y": 0.40}, "SPEAKER_01": {"x": 0.72, "y": 0.42}}\n'
        "如果畫面中只有一個人、或你無法可靠判斷，輸出 {}。")

    try:
        data = claude.ask_json(prompt, images=images, label="07")
    except Exception as exc:  # noqa: BLE001
        warn("07", f"人物位置判斷失敗（{exc}）")
        return {}
    if not isinstance(data, dict) or not data:
        return {}

    out = {}
    for sid, val in data.items():
        if not isinstance(val, dict):
            continue
        out[sid] = {"x": clamp(float(val.get("x", 0.5)), 0.05, 0.95),
                    "y": clamp(float(val.get("y", 0.42)), 0.05, 0.95)}
    if len(out) < 2:
        return {}
    # 兩人位置太接近 → 判斷不可信
    xs = sorted(v["x"] for v in out.values())
    if xs[-1] - xs[0] < 0.12:
        warn("07", "AI 判斷的兩人位置太接近，不做分鏡。"
                   "可在 project.yaml 用 framing.speaker_positions 手動指定。")
        return {}
    log("07", "人物位置：" + "、".join(f'{k} x={v["x"]:.2f}' for k, v in out.items()))
    return out


# ------------------------------------------------------------ 鏡位安排 -----

def _turns(words, emap: EditMap) -> list[dict]:
    """把剪輯後的字合併成一段一段的「誰在講話」。"""
    turns: list[dict] = []
    for w in words:
        s = emap.src_to_edit(w["s"])
        e = emap.src_to_edit(w["e"])
        if s is None or e is None or e <= s:
            continue
        spk = w.get("spk", "SPEAKER_00")
        if turns and turns[-1]["spk"] == spk and s - turns[-1]["e"] < 1.2:
            turns[-1]["e"] = e
        else:
            turns.append({"spk": spk, "s": s, "e": e})
    return turns


def _shots(cfg, turns, positions, seq) -> list[dict]:
    min_shot = float(cfg.get("framing.min_shot", 2.5))
    max_shot = float(cfg.get("framing.max_shot", 14.0))
    punch = float(cfg.get("framing.punch_scale", 1.32))
    wide = float(cfg.get("framing.wide_scale", 1.0))
    every = max(1, int(cfg.get("framing.return_to_wide_every", 3)))
    W, H = int(seq["width"]), int(seq["height"])

    shots: list[dict] = []
    punch_count = 0
    for t in turns:
        dur = t["e"] - t["s"]
        pos = positions.get(t["spk"])
        if dur < min_shot or pos is None:
            shots.append(_shot(t["s"], t["e"], "wide", t["spk"], wide, None, W, H))
            continue
        # 太長的一段拆成幾個鏡位，避免畫面呆滯
        n = max(1, math.ceil(dur / max_shot))
        step = dur / n
        for k in range(n):
            s = t["s"] + k * step
            e = t["s"] + (k + 1) * step if k < n - 1 else t["e"]
            punch_count += 1
            if punch_count % (every + 1) == 0:
                shots.append(_shot(s, e, "wide", t["spk"], wide, None, W, H))
            else:
                shots.append(_shot(s, e, "punch", t["spk"], punch, pos, W, H))

    # 開場先給全景，讓觀眾知道現場有誰、坐在哪 —— 不要一開始就懟臉
    if shots and bool(cfg.get("framing.open_wide", True)):
        head_until = float(cfg.get("framing.open_wide_seconds", 6.0))
        for sh in shots:
            if sh["s"] >= head_until:
                break
            sh.update({"mode": "wide", "scale": round(wide, 4),
                       "position": [0.0, 0.0], "focus": None})

    # 合併相鄰且參數相同的鏡位
    merged: list[dict] = []
    for sh in shots:
        if merged and merged[-1]["mode"] == sh["mode"] \
                and merged[-1]["position"] == sh["position"] \
                and abs(merged[-1]["e"] - sh["s"]) < 0.05:
            merged[-1]["e"] = sh["e"]
        else:
            merged.append(sh)
    return [m for m in merged if m["e"] - m["s"] > 0.2]


def _shot(s, e, mode, spk, scale, pos, W, H) -> dict:
    """算出 FCP adjust-transform 的位移量（像素，Y 軸向上為正）。"""
    if pos is None or scale <= 1.0:
        px = py = 0.0
    else:
        px = (0.5 - pos["x"]) * W * scale
        py = (pos["y"] - 0.5) * H * scale
        lim_x = (scale - 1) / 2 * W
        lim_y = (scale - 1) / 2 * H
        px = clamp(px, -lim_x, lim_x)
        py = clamp(py, -lim_y, lim_y)
    return {"s": round(s, 3), "e": round(e, 3), "mode": mode, "spk": spk,
            "scale": round(scale, 4),
            "position": [round(px, 1), round(py, 1)],
            "focus": pos}
