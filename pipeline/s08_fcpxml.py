"""
Stage 08：把所有決策組成一份 FCPXML，匯入 Final Cut Pro 就是一條剪好的時間軸。

時間軸結構：
  spine        每個保留片段一個 asset-clip（贅詞／停頓已經被切掉）
    lane 1     B-roll（只帶畫面，聲音關掉）
    lane 2     航線／名詞說明短片（含 alpha）
    lane -1    中英雙語隱藏式字幕
  每個 spine clip 上帶 adjust-transform 關鍵影格 → 依說話者推近／拉遠
"""
from __future__ import annotations

import html
from pathlib import Path
from xml.etree import ElementTree as ET

from .timeline import Rate
from .util import StageError, fmt_hhmmss, log, media_info, read_json, warn

CAPTION_FONT = "Helvetica Neue"


class _Res:
    """resources 區塊的管理器：格式與素材各自去重。"""

    def __init__(self, root: ET.Element):
        self.node = ET.SubElement(root, "resources")
        self._n = 0
        self._formats: dict[tuple, str] = {}
        self._assets: dict[str, tuple[str, dict]] = {}

    def _id(self) -> str:
        self._n += 1
        return f"r{self._n}"

    def format(self, width: int, height: int, rate: Rate | None) -> str:
        """
        建立一個 format resource。

        刻意**不寫 name**：FCP 會把 name 當成內建格式預設去查表，自己拼出來的
        名字（例如 4K 29.97 的 "FFVideoFormat2160p2997"）並不存在，FCP 解析
        不出格式，用到它的素材就會變成「沒有個別媒體，剪輯無效」。
        name 是選填的，width / height / frameDuration 已經完整定義了格式，
        FCP 會自動建立對應的自訂格式。
        """
        key = (width, height, rate.num if rate else 0, rate.den if rate else 0)
        if key in self._formats:
            return self._formats[key]
        fid = self._id()
        attrs = {"id": fid, "width": str(width), "height": str(height),
                 "colorSpace": "1-1-1 (Rec. 709)"}
        if rate is not None:
            attrs["frameDuration"] = rate.frame_duration
        ET.SubElement(self.node, "format", attrs)
        self._formats[key] = fid
        return fid

    def asset(self, path: str) -> tuple[str, dict]:
        """回傳 (asset_id, media_info)，同一個檔案只會建立一次。"""
        key = str(Path(path).resolve())
        if key in self._assets:
            return self._assets[key]
        info = media_info(key)
        is_still = not info.get("fps_num") or info["duration"] <= 0
        if info.get("has_video") and not is_still:
            arate = Rate(info["fps_num"], info["fps_den"])
            fid = self.format(info.get("width", 1920), info.get("height", 1080), arate)
            dur = arate.time(info["duration"])
        elif info.get("has_video"):
            arate = None
            fid = self.format(info.get("width", 1920), info.get("height", 1080), None)
            dur = "0s"
        else:
            raise StageError(f"{Path(key).name} 沒有影像軌，無法放進時間軸")

        aid = self._id()
        attrs = {"id": aid, "name": Path(key).stem, "start": "0s", "duration": dur,
                 "hasVideo": "1", "videoSources": "1", "format": fid}
        if info.get("has_audio"):
            attrs.update({"hasAudio": "1", "audioSources": "1",
                          "audioChannels": str(info.get("audio_channels", 2)),
                          "audioRate": str(info.get("audio_rate", 48000))})
        node = ET.SubElement(self.node, "asset", attrs)
        ET.SubElement(node, "media-rep", {"kind": "original-media",
                                          "src": Path(key).as_uri()})
        info["_rate"] = arate
        info["_still"] = arate is None
        self._assets[key] = (aid, info)
        return aid, info


def run_stage(cfg, claude=None) -> dict:
    ingest = read_json(cfg.build_file("01_ingest.json"))
    cuts = read_json(cfg.build_file("03_cuts.json"))
    subs = read_json(cfg.build_file("04_subtitles.json"))
    broll = read_json(cfg.build_file("05_broll.json"), default={"placements": []})
    expl = read_json(cfg.build_file("06_explainers.json"), default={"items": []})
    framing = read_json(cfg.build_file("07_framing.json"), default={"shots": []})

    seq = ingest["sequence"]
    rate = Rate(seq["fps_num"], seq["fps_den"])
    W, H = int(seq["width"]), int(seq["height"])

    root = ET.Element("fcpxml", {"version": "1.11"})
    res = _Res(root)
    seq_format = res.format(W, H, rate)
    main_id, main_info = res.asset(ingest["source"]["path"])

    lib = ET.SubElement(root, "library")
    event = ET.SubElement(lib, "event", {"name": cfg.get("project.event", "Interview")})
    project = ET.SubElement(event, "project", {"name": cfg.get("project.name", "Interview")})

    total = float(cuts["duration_edit"])
    sequence = ET.SubElement(project, "sequence", {
        "format": seq_format,
        "duration": rate.time(total),
        "tcStart": "0s", "tcFormat": "NDF",
        "audioLayout": "stereo",
        "audioRate": f'{int(seq["audio_rate"]) // 1000}k',
    })
    spine = ET.SubElement(sequence, "spine")

    # ---- 主軸：每個保留片段一刀 -------------------------------------------
    clips: list[dict] = []
    edit_cursor = 0.0
    for i, (src_s, src_e) in enumerate(cuts["keeps"]):
        dur = src_e - src_s
        node = ET.SubElement(spine, "asset-clip", {
            "ref": main_id,
            "offset": rate.time(edit_cursor),
            "name": f'{cfg.get("project.name", "Interview")} {i+1:03d}',
            "start": rate.time(src_s),
            "duration": rate.time(dur),
            "format": seq_format,
            "tcFormat": "NDF",
        })
        clips.append({"node": node, "edit_s": edit_cursor, "edit_e": edit_cursor + dur,
                      "src_s": src_s})
        edit_cursor += dur

    def owner(t: float) -> dict:
        """找出負責掛載這個時間點的 spine clip。"""
        for c in clips:
            if c["edit_s"] <= t < c["edit_e"]:
                return c
        return clips[-1] if t >= clips[-1]["edit_e"] else clips[0]

    def local(c: dict, t: float) -> float:
        """成品時間 → 該 clip 的內部時間（與 clip 的 start 同一個座標系）。"""
        return c["src_s"] + (t - c["edit_s"])

    # ---- 鏡位（關鍵影格）---------------------------------------------------
    n_shots = _apply_framing(cfg, clips, framing.get("shots", []), rate)

    # ---- B-roll（lane 1）---------------------------------------------------
    n_broll = 0
    for p in broll.get("placements", []):
        try:
            aid, info = res.asset(p["path"])
        except Exception as exc:  # noqa: BLE001
            warn("08", f'B-roll {p["path"]} 無法加入：{exc}')
            continue
        c = owner(float(p["at"]))
        arate: Rate | None = info["_rate"]
        dur = float(p["duration"])
        attrs = {
            "ref": aid, "lane": "1",
            "offset": rate.time(local(c, float(p["at"]))),
            "name": Path(p["path"]).stem,
            "duration": (arate or rate).time(dur),
        }
        if info["_still"]:
            # <video> 用 role；照片沒有聲音，直接放
            attrs["start"] = "0s"
            attrs["role"] = "B-Roll"
            ET.SubElement(c["node"], "video", attrs)
        else:
            # <asset-clip> 沒有 role 屬性，只有 audioRole / videoRole。
            # 寫成 role 會讓 Final Cut Pro 匯入時 DTD 驗證失敗。
            attrs["start"] = arate.time(float(p.get("src_in", 0.0)))
            attrs["srcEnable"] = "video"     # 只用畫面，不要素材的原聲
            attrs["videoRole"] = "B-Roll"
            ET.SubElement(c["node"], "asset-clip", attrs)
        n_broll += 1

    # ---- 說明短片（lane 2）-------------------------------------------------
    n_expl = 0
    for it in expl.get("items", []):
        try:
            aid, info = res.asset(it["path"])
        except Exception as exc:  # noqa: BLE001
            warn("08", f'說明短片 {it["path"]} 無法加入：{exc}')
            continue
        c = owner(float(it["at"]))
        arate = info["_rate"] or rate
        ET.SubElement(c["node"], "video", {
            "ref": aid, "lane": "2",
            "offset": rate.time(local(c, float(it["at"]))),
            "name": it.get("title_zh") or it.get("term_zh") or "Explainer",
            "start": "0s",
            "duration": arate.time(float(it["duration"])),
            "role": "Graphics",
        })
        n_expl += 1

    # ---- 字幕（lane -1 / -2）----------------------------------------------
    n_cap = _apply_captions(cfg, clips, subs["cues"], rate, owner, local)

    out = cfg.build_file("08_timeline.fcpxml")
    _write(root, out)
    log("08", f"FCPXML 完成：{len(clips)} 段主畫面、{n_broll} 段 B-roll、"
              f"{n_expl} 段說明、{n_shots} 個鏡位關鍵影格、{n_cap} 則字幕")
    log("08", f"成品長度 {fmt_hhmmss(total)} → {out}")
    return {"path": str(out), "duration": total, "clips": len(clips),
            "broll": n_broll, "explainers": n_expl, "captions": n_cap}


# -------------------------------------------------------------- 最小探針 ---

def build_probe(cfg) -> Path:
    """
    產生一份「能成立的最小 FCPXML」：只有一段主畫面，
    沒有字幕、沒有 B-roll、沒有說明短片、沒有鏡位關鍵影格。

    用途是排錯。Final Cut Pro 匯入失敗時，先試這一份：
      匯入成功 → 媒體本身沒問題，是時間軸上加的東西有問題
      匯入失敗 → 問題在媒體檔（路徑、編碼、VFR）或素材宣告本身
    """
    ingest = read_json(cfg.build_file("01_ingest.json"))
    seq = ingest["sequence"]
    rate = Rate(seq["fps_num"], seq["fps_den"])

    # 取第一段保留片段；還沒跑過 stage 03 就直接取開頭 5 秒
    cuts = read_json(cfg.build_file("03_cuts.json"), default=None)
    if cuts and cuts.get("keeps"):
        src_s, src_e = cuts["keeps"][0]
    else:
        src_s, src_e = 0.0, min(5.0, float(ingest["source"]["duration"]))
    dur = max(rate.seconds(2), src_e - src_s)

    root = ET.Element("fcpxml", {"version": "1.11"})
    res = _Res(root)
    fmt = res.format(int(seq["width"]), int(seq["height"]), rate)
    aid, _ = res.asset(ingest["source"]["path"])

    lib = ET.SubElement(root, "library")
    event = ET.SubElement(lib, "event", {"name": cfg.get("project.event", "Probe")})
    project = ET.SubElement(event, "project", {"name": "PROBE minimal"})
    sequence = ET.SubElement(project, "sequence", {
        "format": fmt, "duration": rate.time(dur), "tcStart": "0s",
        "tcFormat": "NDF", "audioLayout": "stereo",
        "audioRate": f'{int(seq["audio_rate"]) // 1000}k',
    })
    spine = ET.SubElement(sequence, "spine")
    ET.SubElement(spine, "asset-clip", {
        "ref": aid, "offset": "0s", "name": "probe",
        "start": rate.time(src_s), "duration": rate.time(dur),
        "format": fmt, "tcFormat": "NDF",
    })

    out = cfg.build_file("probe_minimal.fcpxml")
    _write(root, out)
    log("probe", f"最小探針已產生：{out}")
    log("probe", f"內容：1 段主畫面，{fmt_hhmmss(src_s)} 起算 {dur:.1f} 秒，"
                 f"沒有字幕／B-roll／鏡位")
    log("probe", "匯入成功 → 媒體沒問題，問題在時間軸上加的東西")
    log("probe", "匯入失敗 → 問題在媒體檔本身（路徑、編碼、VFR）")
    return out


# ------------------------------------------------------------ 鏡位處理 -----

def _apply_framing(cfg, clips, shots, rate: Rate) -> int:
    if not shots:
        return 0
    trans_frames = max(1, int(cfg.get("framing.transition_frames", 1)))
    trans = rate.seconds(trans_frames)
    count = 0

    for c in clips:
        inside = [s for s in shots if s["e"] > c["edit_s"] and s["s"] < c["edit_e"]]
        if not inside:
            continue
        keys: list[tuple[float, float, list]] = []   # (clip 內部時間, scale, position)
        for k, sh in enumerate(inside):
            start = max(sh["s"], c["edit_s"])
            t = c["src_s"] + (start - c["edit_s"])
            if k > 0 and trans > 0:
                prev = inside[k - 1]
                # 前一格保持舊值，下一格才切換 → 視覺上是硬切
                keys.append((max(c["src_s"], t - trans),
                             prev["scale"], prev["position"]))
            keys.append((t, sh["scale"], sh["position"]))
        if not keys:
            continue
        # 第一個關鍵影格必須落在 clip 起點，否則 FCP 會從第一格開始就套用
        if keys[0][0] > c["src_s"]:
            keys.insert(0, (c["src_s"], keys[0][1], keys[0][2]))

        adj = ET.SubElement(c["node"], "adjust-transform")
        for name, getter in (("position", lambda k: f"{k[2][0]} {k[2][1]}"),
                             ("scale", lambda k: f"{k[1]} {k[1]}")):
            param = ET.SubElement(adj, "param", {"name": name})
            anim = ET.SubElement(param, "keyframeAnimation")
            for k in keys:
                ET.SubElement(anim, "keyframe", {
                    "time": rate.time(k[0]),
                    "value": getter(k),
                    "interp": "linear", "curve": "linear",
                })
        # adjust-transform 必須排在連接的 clip 之前，否則 FCP 會拒絕
        c["node"].remove(adj)
        c["node"].insert(0, adj)
        count += len(keys)
    return count


# ------------------------------------------------------------ 字幕處理 -----

def _apply_captions(cfg, clips, cues, rate: Rate, owner, local) -> int:
    tracks = [("zh", "zh-Hant", "-1"), ("en", "en", "-2")]
    count = 0
    for cue in cues:
        c = owner(float(cue["s"]))
        off = local(c, float(cue["s"]))
        dur = max(rate.seconds(2), float(cue["e"]) - float(cue["s"]))
        for field, lang, lane in tracks:
            text = (cue.get(field) or "").strip()
            if not text:
                continue
            style_id = f"cs{count}"
            cap = ET.SubElement(c["node"], "caption", {
                "name": f'{lang} {cue["id"]}',
                "lane": lane,
                "offset": rate.time(off),
                "duration": rate.time(dur),
                "role": f"iTT?captionFormat=ITT.{lang}",
            })
            tnode = ET.SubElement(cap, "text", {"placement": "bottom"})
            style = ET.SubElement(tnode, "text-style", {"ref": style_id})
            style.text = text
            defn = ET.SubElement(cap, "text-style-def", {"id": style_id})
            ET.SubElement(defn, "text-style", {
                "font": CAPTION_FONT, "fontSize": "13", "fontFace": "Regular",
                "fontColor": "1 1 1 1", "backgroundColor": "0 0 0 1",
                "alignment": "center",
            })
            count += 1
    return count


# FCPXML DTD 允許的屬性（只列本程式會產生的元素）。
# 這是踩過坑之後加的防線：asset-clip 沒有 role，只有 audioRole / videoRole，
# 寫錯了 Final Cut Pro 會在匯入時報 "No declaration for attribute ..."。
_ALLOWED_ATTRS: dict[str, set[str]] = {
    "fcpxml": {"version"},
    "resources": set(),
    "format": {"id", "name", "frameDuration", "fieldOrder", "width", "height",
               "paspH", "paspV", "colorSpace", "projection", "stereoscopic"},
    "asset": {"id", "name", "uid", "start", "duration", "hasVideo", "hasAudio",
              "format", "videoSources", "audioSources", "audioChannels",
              "audioRate", "customLUTOverride", "colorSpaceOverride",
              "projectionOverride", "stereoscopicOverride"},
    "media-rep": {"kind", "sig", "src", "suggestedFilename"},
    "library": {"location", "colorProcessing"},
    "event": {"name", "uid"},
    "project": {"name", "uid", "id", "modDate"},
    "sequence": {"format", "duration", "tcStart", "tcFormat", "audioLayout",
                 "audioRate", "renderFormat", "keywords", "note"},
    "spine": {"name", "format", "lane", "offset"},
    "asset-clip": {"ref", "lane", "offset", "name", "start", "duration",
                   "enabled", "format", "tcFormat", "audioRole", "videoRole",
                   "srcEnable", "modDate", "note", "audioStart", "audioDuration"},
    "video": {"ref", "lane", "offset", "name", "start", "duration", "enabled",
              "role", "srcID"},
    "caption": {"lane", "offset", "name", "start", "duration", "enabled",
                "role", "note"},
    "text": {"placement", "alignment", "display-style", "roll-up-height",
             "position"},
    "text-style": {"ref", "font", "fontSize", "fontFace", "fontColor",
                   "backgroundColor", "alignment", "bold", "italic", "underline",
                   "strokeColor", "strokeWidth", "baseline", "shadowColor",
                   "shadowOffset", "shadowBlurRadius", "kerning", "lineSpacing",
                   "tabStops"},
    "text-style-def": {"id", "name"},
    "adjust-transform": {"position", "scale", "rotation", "anchor"},
    "param": {"name", "key", "value", "enabled"},
    "keyframeAnimation": set(),
    "keyframe": {"time", "value", "interp", "curve"},
}


def _validate_attributes(root: ET.Element) -> None:
    """在寫檔前檢查所有屬性名稱，把 DTD 錯誤擋在產出之前。"""
    bad: list[str] = []
    for el in root.iter():
        allowed = _ALLOWED_ATTRS.get(el.tag)
        if allowed is None:
            bad.append(f"未知的元素 <{el.tag}>")
            continue
        for name in el.attrib:
            if name not in allowed:
                bad.append(f"<{el.tag}> 不該有屬性 {name!r}"
                           f"（合法的有：{', '.join(sorted(allowed)) or '無'}）")
    if bad:
        uniq = sorted(set(bad))
        raise StageError(
            "產生的 FCPXML 有 Final Cut Pro 不接受的屬性，已中止寫檔：\n  "
            + "\n  ".join(uniq[:10])
            + (f"\n  …另有 {len(uniq) - 10} 項" if len(uniq) > 10 else ""))


def _write(root: ET.Element, path: Path) -> None:
    _validate_attributes(root)
    ET.indent(root, space="  ")
    body = ET.tostring(root, encoding="unicode")
    path.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE fcpxml>\n' + body + "\n",
        encoding="utf-8")
