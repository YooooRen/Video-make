"""Stage 04：在「剪完的時間軸」上生成中英雙語字幕。"""
from __future__ import annotations

import re
from pathlib import Path

from .claude_client import load_prompt
from .timeline import EditMap, Rate, write_srt, write_vtt
from .util import (apply_corrections, chunked, fmt_hhmmss, log, read_json,
                   warn, write_json, write_text)

_SENT_END = re.compile(r"[.!?…]$")

# 純粹的應答語／發語詞。整張字卡只有這些字時不給字幕 ——
# 觀眾看到「嗯嗯」沒有任何幫助，只會佔住畫面。
# 刻意不收 yeah / yes / right / okay：那些常常是有意義的回答。
_INTERJECTIONS = {
    "um", "umm", "ummm", "uh", "uhh", "uhhh", "erm", "er", "err",
    "ah", "ahh", "mm", "mmm", "hmm", "hm", "hmmm",
    "mhm", "mhmm", "mmhmm", "uhhuh", "huh",
}
_WORD_RE = re.compile(r"[A-Za-z']+(?:-[A-Za-z']+)*")
# 中文側的應答語，用來擋住翻譯把它們譯成「嗯嗯」的情況
_ZH_INTERJECTIONS = re.compile(r"^[嗯呃啊喔哦唔欸誒嘿哈，。、！？…\s]+$")


def _is_interjection_only(text: str) -> bool:
    """整段文字是不是只有應答語（含只剩標點的情況）。"""
    toks = _WORD_RE.findall((text or "").lower())
    if not toks:
        return True
    return all(t.replace("-", "").replace("'", "") in _INTERJECTIONS for t in toks)
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

    if cfg.get("subtitles.drop_interjections", True):
        kept = [c for c in cues if not _is_interjection_only(c["en"])]
        if len(kept) < len(cues):
            log("04", f"移除 {len(cues) - len(kept)} 則只有應答語的字幕"
                      f"（Mm-hmm、uh-huh 這類）")
            for i, c in enumerate(kept):
                c["id"] = i          # 重新編號，翻譯才對得上
            cues = kept

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
    corrections = cfg.get("corrections", {}) or {}

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
            "en": _wrap(apply_corrections(_join(toks), corrections),
                        max_chars, max_lines),
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

_GLOSSARY_HEADER = """\
# ── 翻譯詞彙表 ──────────────────────────────────────────────
# 這個檔案可以直接編輯，改完重跑 stage 04 就會生效。
#
# 每行三欄，用 Tab 分隔：  英文原詞 <TAB> 中文譯法 <TAB> 說明（選填）
#
# 你的編輯優先：這裡已經有的詞，AI 不會覆蓋，
# 它只會在檔尾補上還沒收錄到的新詞。
# 不要某個詞就刪掉那一行；想完全重新產生就刪掉整個檔案。
#
# 人名與船名不收錄在這裡（它們保持英文原樣）。
# 要訂正聽錯的人名，用 project.yaml 的 corrections。
# ────────────────────────────────────────────────────────────
"""


def _glossary_path(cfg) -> Path:
    custom = cfg.get("subtitles.glossary_file", "")
    return cfg.path(custom) if custom else cfg.build_file("04_glossary.txt")


def _read_glossary(path: Path) -> list[dict]:
    """讀取使用者編輯過的詞彙表。容許用 Tab 或連續空白分欄。"""
    if not path.exists():
        return []
    out: list[dict] = []
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t") if "\t" in line else re.split(r"\s{2,}", line)
        parts = [p.strip() for p in parts if p.strip()]
        if len(parts) < 2:
            warn("04", f"詞彙表第 {lineno} 行少了中文譯法，略過：{line[:40]}")
            continue
        out.append({"en": parts[0], "zh": parts[1],
                    "note": parts[2] if len(parts) > 2 else ""})
    return out


def _write_glossary(path: Path, items: list[dict]) -> None:
    body = "\n".join(f'{d["en"]}\t{d["zh"]}\t{d.get("note", "")}'.rstrip("\t")
                     for d in items)
    write_text(path, _GLOSSARY_HEADER + body + "\n")


def _glossary(cfg, claude, tr) -> list[dict]:
    """
    建立翻譯用的術語對照表。

    使用者編輯過的內容一律優先：既有的詞不會被覆蓋，AI 只負責補上
    還沒收錄到的新詞（例如換了更長的影片之後新出現的術語）。
    """
    path = _glossary_path(cfg)
    user_items = _read_glossary(path)
    have = {d["en"].strip().lower() for d in user_items}
    if user_items:
        log("04", f"沿用你編輯的詞彙表 {len(user_items)} 個詞（{path.name}）")

    text = " ".join(s["text"] for s in tr["segments"])[:60000]
    existing = "\n".join(f'- {d["en"]}' for d in user_items) or "（目前沒有）"
    try:
        data = claude.ask_json(
            load_prompt("glossary.md")
            .replace("{{EXISTING}}", existing)
            .replace("{{TRANSCRIPT}}", text), label="04")
    except Exception as exc:  # noqa: BLE001
        warn("04", f"詞彙表補充失敗（{exc}），只用現有的繼續翻譯")
        return user_items

    added = [d for d in (data if isinstance(data, list) else [])
             if isinstance(d, dict) and d.get("en") and d.get("zh")
             and d["en"].strip().lower() not in have]
    # 同一批回覆裡也可能重複，再去一次重
    seen = set(have)
    deduped = []
    for d in added:
        key = d["en"].strip().lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append({"en": d["en"], "zh": d["zh"], "note": d.get("note", "")})

    items = user_items + deduped
    if deduped:
        log("04", f"AI 補上 {len(deduped)} 個新詞，共 {len(items)} 個")
    elif not user_items:
        log("04", f"建立詞彙表 {len(items)} 個專有名詞")
    else:
        log("04", "沒有需要補充的新詞")
    _write_glossary(path, items)
    return items


def _translate(cfg, claude, cues, glossary) -> None:
    corrections = cfg.get("corrections", {}) or {}
    gl = "\n".join(f'- {d["en"]} → {d["zh"]}' for d in glossary) or "（無）"
    if corrections:
        gl += "\n\n【人名與專有名詞的正確寫法，請務必照這個拼】\n"
        gl += "\n".join(f"- {k} → {v}" for k, v in corrections.items())
    batch = int(cfg.get("subtitles.translate_batch", 25))

    _translate_batches(cfg, claude, cues, cues, gl, corrections, batch, "翻譯")

    # 補漏：整批失敗時（多半是 Claude 用量到上限或回覆被截斷）用更小的批次重試。
    # 已成功的批次會命中快取，不會重複消耗額度。
    for attempt, size in ((1, max(6, batch // 3)), (2, 4)):
        missing = [c for c in cues if not c.get("zh")]
        if not missing:
            break
        warn("04", f"有 {len(missing)} 則沒翻到，改用 {size} 則一批重試（第 {attempt} 次）")
        _translate_batches(cfg, claude, cues, missing, gl, corrections, size,
                           f"補譯{attempt}")

    missing = [c for c in cues if not c.get("zh")]
    if missing:
        warn("04", "─" * 52)
        warn("04", f"仍有 {len(missing)}/{len(cues)} 則字幕沒有中文，"
                   f"缺口從 {fmt_hhmmss(missing[0]['s'])} 到 "
                   f"{fmt_hhmmss(missing[-1]['e'])}")
        warn("04", "中文 SRT 會在缺口處直接沒有字幕，英文則完整。")
        warn("04", "最常見的原因是 Claude 用量到上限。等額度恢復後重跑：")
        warn("04", "    python run.py --only 04")
        warn("04", "已翻好的部分會命中快取，只會重問缺的那些。")
        warn("04", "─" * 52)
    else:
        log("04", f"{len(cues)} 則字幕全部翻譯完成")


def _translate_batches(cfg, claude, all_cues, targets, gl, corrections,
                       batch: int, label: str) -> None:
    """把 targets 分批送去翻譯；前後文一律從 all_cues 取，保持語境完整。"""
    tmpl = load_prompt("translate_zhtw.md")
    max_chars = int(cfg.get("subtitles.max_chars_zh", 18))
    max_lines = int(cfg.get("subtitles.max_lines", 2))
    pos = {c["id"]: i for i, c in enumerate(all_cues)}
    idx = {c["id"]: c for c in all_cues}
    total = (len(targets) + batch - 1) // batch

    for bi, group in enumerate(chunked(targets, batch)):
        lo, hi = pos[group[0]["id"]], pos[group[-1]["id"]]
        before, after = all_cues[max(0, lo - 3):lo], all_cues[hi + 1:hi + 4]
        body = []
        if before:
            body.append("（前文，僅供參考，不要翻譯）")
            body += [f'  {c["speaker"]}: {c["en"]}' for c in before]
        body.append("（以下才是要翻譯的）")
        body += [f'{c["id"]} | {c["speaker"]} | {c["en"]}'.replace("\n", " ")
                 for c in group]
        if after:
            body.append("（後文，僅供參考，不要翻譯）")
            body += [f'  {c["speaker"]}: {c["en"]}' for c in after]

        prompt = (tmpl.replace("{{MAX_CHARS}}", str(max_chars))
                      .replace("{{GLOSSARY}}", gl)
                      .replace("{{CUES}}", "\n".join(body)))
        log("04", f"{label} {bi+1}/{total}（字幕 {group[0]['id']}–{group[-1]['id']}）")
        try:
            data = claude.ask_json(prompt, label="04")
        except Exception as exc:  # noqa: BLE001
            warn("04", f"{label} 第 {bi+1} 批失敗（{exc}）")
            continue
        got = 0
        for it in (data if isinstance(data, list) else []):
            if not isinstance(it, dict):
                continue
            cue = idx.get(it.get("id"))
            zh = str(it.get("zh", "")).strip()
            if cue is not None and zh:
                if (cfg.get("subtitles.drop_interjections", True)
                        and _ZH_INTERJECTIONS.match(zh)):
                    zh = ""          # 譯成「嗯嗯」之類的就不要這則中文
                if zh:
                    cue["zh"] = _wrap_zh(apply_corrections(zh, corrections),
                                         max_chars, max_lines)
                got += 1
        if got < len(group):
            warn("04", f"{label} 第 {bi+1} 批有 {len(group)-got} 則沒翻到")


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
