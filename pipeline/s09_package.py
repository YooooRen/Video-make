"""Stage 09：整理上架資訊 —— 標題、說明欄、章節、提到的人名、標籤。"""
from __future__ import annotations

from .claude_client import load_prompt
from .util import fmt_hhmmss, log, read_json, warn, write_json, write_text


def run_stage(cfg, claude=None) -> dict:
    subs = read_json(cfg.build_file("04_subtitles.json"))
    tr = read_json(cfg.build_file("02_transcript.json"))
    expl = read_json(cfg.build_file("06_explainers.json"), default={"items": []})
    total = float(subs["duration"])

    if claude is None:
        warn("09", "沒有 Claude client，跳過說明欄生成")
        return _save(cfg, {})

    lines = [f'{c["s"]:.0f} {c["speaker"]}: {c["en"]}'.replace("\n", " ")
             for c in subs["cues"]]
    speakers = "、".join(
        f'{v.get("name") or k}（{v.get("role","")}）' for k, v in tr.get("speakers", {}).items())

    prompt = (load_prompt("package.md")
              .replace("{{TRANSCRIPT}}", "\n".join(lines)[:150000])
              .replace("{{SPEAKERS}}", speakers or "未知")
              .replace("{{DURATION}}", fmt_hhmmss(total)))
    log("09", "生成標題／說明欄／章節")
    try:
        data = claude.ask_json(prompt, label="09")
    except Exception as exc:  # noqa: BLE001
        warn("09", f"生成失敗（{exc}）")
        return _save(cfg, {})
    if not isinstance(data, dict):
        return _save(cfg, {})

    data["chapters"] = _fix_chapters(data.get("chapters", []), total)
    # 說明短片裡整理過的名詞併進來，避免遺漏
    for it in expl.get("items", []):
        if it.get("kind") == "term" and it.get("term_en"):
            terms = data.setdefault("terms", [])
            if not any(t.get("en") == it["term_en"] for t in terms if isinstance(t, dict)):
                terms.append({"en": it["term_en"], "zh": it.get("term_zh", "")})

    _save(cfg, data)
    _write_description(cfg, data, total)
    people = data.get("people", []) or []
    log("09", f"整理出 {len(people)} 位提到的人名、{len(data.get('chapters', []))} 個章節")
    return data


def _save(cfg, data) -> dict:
    write_json(cfg.build_file("09_package.json"), data)
    return data


def _fix_chapters(chapters, total: float) -> list[dict]:
    """YouTube 章節規則：第一章必須 0:00、遞增、至少 3 章、每章至少 10 秒。"""
    out = []
    for ch in chapters or []:
        if not isinstance(ch, dict):
            continue
        try:
            t = int(float(ch.get("t", -1)))
        except (TypeError, ValueError):
            continue
        title = str(ch.get("title", "")).strip()
        if t < 0 or t >= total or not title:
            continue
        out.append({"t": t, "title": title[:60]})
    out.sort(key=lambda c: c["t"])
    dedup = []
    for ch in out:
        if dedup and ch["t"] - dedup[-1]["t"] < 10:
            continue
        dedup.append(ch)
    if not dedup:
        return []
    dedup[0]["t"] = 0
    return dedup if len(dedup) >= 3 else []


def _write_description(cfg, data, total: float) -> None:
    """輸出可以直接貼進 YouTube 的說明欄。"""
    L: list[str] = []
    titles = data.get("titles") or []
    if titles:
        L += ["# 候選標題（挑一個貼到 YouTube）", ""]
        L += [f"{i+1}. {t}" for i, t in enumerate(titles)]
        L += ["", "---", ""]

    L += ["# 說明欄（以下整段可直接複製）", "", "```"]
    if data.get("hook"):
        L += [data["hook"], ""]
    if data.get("summary_zh"):
        L += [data["summary_zh"], ""]

    if data.get("chapters"):
        L += ["⏱ 章節", ""]
        L += [f'{fmt_hhmmss(c["t"])} {c["title"]}' for c in data["chapters"]]
        L.append("")

    people = [p for p in (data.get("people") or []) if isinstance(p, dict) and p.get("name")]
    if people:
        L += ["👤 影片中提到的人物", ""]
        for p in people:
            role = p.get("role", "")
            ctx = p.get("context", "")
            L.append(f'・{p["name"]}' + (f"— {role}" if role else "")
                     + (f"（{ctx}）" if ctx else ""))
        L.append("")

    boats = [b for b in (data.get("boats") or []) if isinstance(b, dict) and b.get("name")]
    if boats:
        L += ["⛵ 提到的船", ""]
        L += [f'・{b["name"]}' + (f'— {b.get("note","")}' if b.get("note") else "")
              for b in boats]
        L.append("")

    places = [p for p in (data.get("places") or []) if isinstance(p, dict)]
    if places:
        L += ["📍 提到的地點", ""]
        L.append("、".join(
            f'{p.get("name_zh") or p.get("name_en")}'
            + (f'（{p.get("name_en")}）' if p.get("name_zh") and p.get("name_en") else "")
            for p in places))
        L.append("")

    terms = [t for t in (data.get("terms") or []) if isinstance(t, dict) and t.get("en")]
    if terms:
        L += ["📖 航海名詞小抄", ""]
        L += [f'・{t["en"]}｜{t.get("zh","")}' for t in terms]
        L.append("")

    if data.get("summary_en"):
        L += ["— English —", "", data["summary_en"], ""]

    tags = data.get("hashtags") or []
    if tags:
        L.append(" ".join(str(t) for t in tags))
    L += ["```", ""]

    if data.get("tags"):
        L += ["# 標籤（貼到 YouTube 的 Tags 欄位）", "",
              ", ".join(str(t) for t in data["tags"]), ""]

    L += ["# 上傳字幕", "",
          "在 YouTube 後台 → 字幕 → 上傳檔案（含時間碼）：", "",
          "- 中文（繁體）：`build/subtitles_zh-Hant.srt`",
          "- English：`build/subtitles_en.srt`", "",
          "兩個都上傳，觀眾就能在播放器裡自由切換中／英隱藏式字幕。", ""]

    write_text(cfg.build_file("09_description.md"), "\n".join(L))
