"""Stage 04：在「剪完的時間軸」上生成中英雙語字幕。"""
from __future__ import annotations

import re

from .claude_client import load_prompt
from .timeline import EditMap, Rate, write_srt, write_vtt
from .util import chunked, log, read_json, warn, write_json, write_text

_SENT_END = re.compile(r"[.!?…]$")
_NO_SPACE_BEFORE = set(".,!?;:%)]}'’")


def run_stage(cfg, claude=None) -> dict:
    tr = read_json(cfg.build_file("02_transcript.json"))
    cuts = read_json(cfg.build_file("03_cuts.json"))
    ingest = read_json(cfg.build_file("01_ingest.json"))
    rate = Rate(ingest["sequence"]["fps_num"], ingest["sequence"]["fps_den"])
    emap = EditMap([tuple(k) for k in cuts["keeps"]], rate)

    words = _surviving_words(tr["words"], emap)
    log("04", f"剪輯後保留 {len(words)}/{len(tr['words'])} 個字")

    cues = _build_cues(cfg, words, tr.get("speakers", {}))
    log("04", f"切成 {len(cues)} 則英文字幕")

    glossary: list[dict] = []
    if claude is not None:
        glossary = _glossary(cfg, claude, tr)
        _translate(cfg, claude, cues, glossary)
    else:
        warn("04", "沒有 Claude client，跳過中文翻譯")

    _polish_timing(cfg, cues, emap.duration)

    out = {"duration": emap.duration, "cues": cues, "glossary": glossary}
    write_json(cfg.build_file("04_subtitles.json"), out)

    b = cfg.build
    write_srt(cues, b / "subtitles_en.srt", "en")
    write_vtt(cues, b / "subtitles_en.vtt", "en")
    if any(c.get("zh") for c in cues):
        write_srt(cues, b / "subtitles_zh-Hant.srt", "zh")
        write_vtt(cues, b / "subtitles_zh-Hant.vtt", "zh")
        for c in cues:
            c["both"] = (c.get("zh", "") + "\n" + c.get("en", "")).strip()
        write_srt(cues, b / "subtitles_bilingual.srt", "both")
    log("04", "字幕檔已輸出到 build/（上傳 YouTube 時用這些 .srt 當隱藏式字幕）")
    return out


# ------------------------------------------------------------ 字幕切分 -----

def _surviving_words(words, emap: EditMap) -> list[dict]:
    out = []
    for w in words:
        s = emap.src_to_edit(w["s"])
        e = emap.src_to_edit(w["e"])
        if s is None and e is None:
            continue                       # 整個字被剪掉
        if s is None:
            s = emap.src_to_edit_clamped(w["s"])
        if e is None:
            e = emap.src_to_edit_clamped(w["e"])
        if e <= s:
            continue
        out.append({**w, "s": round(s, 3), "e": round(e, 3)})
    return out


def _join(tokens: list[str]) -> str:
    text = ""
    for t in tokens:
        if text and t[:1] not in _NO_SPACE_BEFORE:
            text += " "
        text += t
    return text.strip()


def _wrap(text: str, max_chars: int, max_lines: int) -> str:
    """把一行拆成最多 max_lines 行，盡量讓每行長度平均。"""
    if len(text) <= max_chars:
        return text
    words = text.split(" ")
    if len(words) == 1:
        return text
    best, best_score = None, None
    for i in range(1, len(words)):
        a, b = " ".join(words[:i]), " ".join(words[i:])
        score = abs(len(a) - len(b)) + max(0, len(a) - max_chars) * 10 \
            + max(0, len(b) - max_chars) * 10
        if best_score is None or score < best_score:
            best, best_score = (a, b), score
    lines = list(best) if best else [text]
    return "\n".join(lines[:max_lines])


def _build_cues(cfg, words, roles) -> list[dict]:
    max_chars = int(cfg.get("subtitles.max_chars_en", 42))
    max_lines = int(cfg.get("subtitles.max_lines", 2))
    max_dur = float(cfg.get("subtitles.max_duration", 6.0))
    gap_break = float(cfg.get("subtitles.gap_break", 0.55))
    budget = max_chars * max_lines

    cues: list[dict] = []
    cur: list[dict] = []

    def flush():
        if not cur:
            return
        toks = [w["w"] for w in cur]
        spk = cur[0].get("spk", "")
        who = roles.get(spk, {})
        cues.append({
            "id": len(cues),
            "s": cur[0]["s"],
            "e": cur[-1]["e"],
            "spk": spk,
            "speaker": who.get("name") or who.get("role") or spk,
            "en": _wrap(_join(toks), max_chars, max_lines),
            "zh": "",
        })
        cur.clear()

    for w in words:
        if cur:
            prev = cur[-1]
            text_len = len(_join([x["w"] for x in cur] + [w["w"]]))
            if (w.get("spk") != prev.get("spk")
                    or w["s"] - prev["e"] > gap_break
                    or w["e"] - cur[0]["s"] > max_dur
                    or text_len > budget
                    or (_SENT_END.search(prev["w"]) and text_len > budget * 0.55)):
                flush()
        cur.append(w)
    flush()
    return cues


def _polish_timing(cfg, cues, total: float) -> None:
    """加上進出點緩衝，並確保不重疊、不短於最短秒數。"""
    lead = float(cfg.get("subtitles.lead_in", 0.06))
    tail = float(cfg.get("subtitles.tail_out", 0.20))
    min_dur = float(cfg.get("subtitles.min_duration", 1.0))

    # 先統一套用提前量，才能用「下一則的起點」當作硬上限
    for c in cues:
        c["s"] = max(0.0, c["s"] - lead)

    for i, c in enumerate(cues):
        limit = min(cues[i + 1]["s"] if i + 1 < len(cues) else total, total)
        end = min(c["e"] + tail, limit)          # 延後消失，但絕不吃到下一則
        end = max(end, min(c["s"] + min_dur, limit))
        c["s"], c["e"] = round(c["s"], 3), round(max(end, c["s"] + 0.04), 3)
        if c["e"] > limit:                        # 極端情況：字與字幾乎無縫
            c["e"] = round(limit, 3)


# ------------------------------------------------------------- 翻譯 --------

def _glossary(cfg, claude, tr) -> list[dict]:
    text = " ".join(s["text"] for s in tr["segments"])[:60000]
    try:
        data = claude.ask_json(
            load_prompt("glossary.md").replace("{{TRANSCRIPT}}", text), label="04")
    except Exception as exc:  # noqa: BLE001
        warn("04", f"詞彙表建立失敗（{exc}），繼續翻譯")
        return []
    items = [d for d in (data if isinstance(data, list) else [])
             if isinstance(d, dict) and d.get("en") and d.get("zh")]
    log("04", f"建立詞彙表 {len(items)} 個專有名詞")
    write_text(cfg.build_file("04_glossary.txt"),
               "\n".join(f'{d["en"]}\t{d["zh"]}\t{d.get("note","")}' for d in items))
    return items


def _translate(cfg, claude, cues, glossary) -> None:
    tmpl = load_prompt("translate_zhtw.md")
    gl = "\n".join(f'- {d["en"]} → {d["zh"]}' for d in glossary) or "（無）"
    max_chars = int(cfg.get("subtitles.max_chars_zh", 18))
    batch = int(cfg.get("subtitles.translate_batch", 25))
    idx = {c["id"]: c for c in cues}
    total = (len(cues) + batch - 1) // batch

    for bi, group in enumerate(chunked(cues, batch)):
        lo, hi = group[0]["id"], group[-1]["id"]
        before = cues[max(0, lo - 3):lo]
        after = cues[hi + 1:hi + 4]
        body = []
        if before:
            body.append("（前文，僅供參考，不要翻譯）")
            body += [f'  {c["speaker"]}: {c["en"]}' for c in before]
        body.append("（以下才是要翻譯的）")
        body += [f'{c["id"]} | {c["speaker"]} | {c["en"]}'.replace("\n", " ") for c in group]
        if after:
            body.append("（後文，僅供參考，不要翻譯）")
            body += [f'  {c["speaker"]}: {c["en"]}' for c in after]

        prompt = (tmpl.replace("{{MAX_CHARS}}", str(max_chars))
                      .replace("{{GLOSSARY}}", gl)
                      .replace("{{CUES}}", "\n".join(body)))
        log("04", f"翻譯 {bi+1}/{total}（字幕 {lo}–{hi}）")
        try:
            data = claude.ask_json(prompt, label="04")
        except Exception as exc:  # noqa: BLE001
            warn("04", f"第 {bi+1} 批翻譯失敗（{exc}），這批留空")
            continue
        got = 0
        for it in (data if isinstance(data, list) else []):
            if not isinstance(it, dict):
                continue
            cue = idx.get(it.get("id"))
            zh = str(it.get("zh", "")).strip()
            if cue is not None and zh:
                cue["zh"] = _wrap_zh(zh, max_chars, int(cfg.get("subtitles.max_lines", 2)))
                got += 1
        if got < len(group):
            warn("04", f"第 {bi+1} 批有 {len(group)-got} 則沒翻到")

    missing = [c["id"] for c in cues if not c.get("zh")]
    if missing:
        warn("04", f"{len(missing)} 則字幕沒有中文，可單獨重跑 stage 04")


def _wrap_zh(text: str, max_chars: int, max_lines: int) -> str:
    text = text.replace("\n", "")
    if len(text) <= max_chars:
        return text
    # 優先在標點處斷行
    for i in range(min(len(text) - 1, max_chars), max(0, max_chars - 8), -1):
        if text[i] in "，。、；：？！ ":
            head, rest = text[:i + 1].strip("， 　"), text[i + 1:].strip()
            if rest:
                return head + "\n" + rest[:max_chars * (max_lines - 1)]
    mid = len(text) // 2
    return text[:mid] + "\n" + text[mid:]
