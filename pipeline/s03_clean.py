"""Stage 03：去掉冗言贅詞、重複、改口與過久停頓，產出剪輯決策。"""
from __future__ import annotations

from .claude_client import load_prompt
from .timeline import Rate, build_keeps, find_pauses
from .util import (StageError, fmt_hhmmss, log, read_json, warn, write_json,
                   write_text)

_KIND_LABEL = {
    "filler": "發語詞",
    "repetition": "重複",
    "false_start": "改口",
    "tangent": "離題",
    "pause": "停頓",
}


def run_stage(cfg, claude=None) -> dict:
    tr = read_json(cfg.build_file("02_transcript.json"))
    ingest = read_json(cfg.build_file("01_ingest.json"))
    rate = Rate(ingest["sequence"]["fps_num"], ingest["sequence"]["fps_den"])
    words = tr["words"]
    duration = float(tr["duration"])

    removals: list[dict] = []

    # ---- 1. 過久停頓（純程式，不花 AI 額度）--------------------------------
    pauses = find_pauses(
        words,
        max_pause=float(cfg.get("cleanup.max_pause", 0.6)),
        keep_pause=float(cfg.get("cleanup.keep_pause", 0.22)),
        duration=duration,
    )
    removals.extend(pauses)
    log("03", f"偵測到 {len(pauses)} 處過久停頓")

    # ---- 2. 贅詞／重複／改口（交給 Claude）---------------------------------
    if cfg.get("cleanup.enabled", True) and claude is not None:
        removals.extend(_ai_removals(cfg, claude, tr))
    elif not cfg.get("cleanup.enabled", True):
        log("03", "cleanup.enabled=false，只處理停頓")

    # ---- 3. 轉成保留片段 ----------------------------------------------------
    keeps, effective = build_keeps(
        duration,
        [(r["s"], r["e"]) for r in removals],
        rate=rate,
        min_removal=float(cfg.get("cleanup.min_removal", 0.10)),
        min_keep=float(cfg.get("cleanup.min_keep", 0.30)),
        pad=float(cfg.get("cleanup.cut_pad", 0.06)),
    )
    kept = sum(e - s for s, e in keeps)
    log("03", f"原始 {fmt_hhmmss(duration)} → 剪後 {fmt_hhmmss(kept)}"
              f"（省下 {fmt_hhmmss(duration - kept)}，{(1-kept/duration)*100:.1f}%）")
    if kept < duration * 0.5:
        warn("03", "剪掉超過一半，建議先看 03_cuts.txt 確認沒有誤刪。")

    out = {
        "duration_src": duration,
        "duration_edit": kept,
        "keeps": [[round(s, 3), round(e, 3)] for s, e in keeps],
        "removals": sorted(removals, key=lambda r: r["s"]),
        "effective_removals": [[round(s, 3), round(e, 3)] for s, e in effective],
        "stats": _stats(removals),
    }
    write_json(cfg.build_file("03_cuts.json"), out)
    _write_report(cfg, out)
    return out


# --------------------------------------------------------------- AI 判斷 ---

def _ai_removals(cfg, claude, tr) -> list[dict]:
    words = tr["words"]
    segments = tr["segments"]
    roles = tr.get("speakers", {})
    tmpl = load_prompt("clean_transcript.md")
    rules = _rules(cfg)
    chunk_n = int(cfg.get("cleanup.chunk_segments", 40))
    allow_tangent = bool(cfg.get("cleanup.remove_tangents", False))

    out: list[dict] = []
    total = (len(segments) + chunk_n - 1) // chunk_n
    for ci in range(total):
        chunk = segments[ci * chunk_n:(ci + 1) * chunk_n]
        if not chunk:
            continue
        lo, hi = chunk[0]["w0"], chunk[-1]["w1"]
        body = _render(chunk, words, roles)
        prompt = tmpl.replace("{{RULES}}", rules).replace("{{TRANSCRIPT}}", body)
        log("03", f"AI 清理 {ci+1}/{total}（{fmt_hhmmss(chunk[0]['s'])}–{fmt_hhmmss(chunk[-1]['e'])}）")
        try:
            data = claude.ask_json(prompt, label="03")
        except Exception as exc:  # noqa: BLE001
            warn("03", f"第 {ci+1} 段清理失敗（{exc}），該段保留原樣")
            continue
        out.extend(_validate(data, words, lo, hi, allow_tangent))
    log("03", f"AI 標記出 {len(out)} 處贅詞／重複／改口")
    return out


def _rules(cfg) -> str:
    rules = []
    if cfg.get("cleanup.remove_fillers", True):
        rules.append("- **發語詞**：um、uh、er、ah、like（當語助詞時）、you know、I mean、"
                     "sort of、kind of、basically、actually（無實義時）、so（句首無意義時）")
    if cfg.get("cleanup.remove_repetitions", True):
        rules.append("- **口吃與重複**：連續講兩次同一個字或詞組（the the、I I），"
                     "只留下最後一次完整的那次")
    if cfg.get("cleanup.remove_false_starts", True):
        rules.append("- **講到一半改口**：說了半句話又重新開始講同一件事，剪掉前面失敗的那次")
    if cfg.get("cleanup.remove_tangents", False):
        rules.append("- **離題**：與帆船／航海主題完全無關、且刪掉不影響上下文的整段閒聊")
    else:
        rules.append("- （本次**不要**刪除離題段落，即使內容偏離主題也請保留）")
    return "\n".join(rules)


def _render(chunk, words, roles) -> str:
    lines = []
    for seg in chunk:
        who = roles.get(seg.get("spk", ""), {})
        name = who.get("name") or who.get("role") or seg.get("spk", "")
        toks = " ".join(f'{w["i"]}:{w["w"]}' for w in words[seg["w0"]:seg["w1"] + 1])
        lines.append(f'[{fmt_hhmmss(seg["s"])}] {name}\n{toks}')
    return "\n\n".join(lines)


def _validate(data, words, lo: int, hi: int, allow_tangent: bool) -> list[dict]:
    """把模型回傳的字詞索引範圍轉成時間範圍，並擋掉不合理的刪除。"""
    items = data.get("removals", []) if isinstance(data, dict) else data
    if not isinstance(items, list):
        return []
    out: list[dict] = []
    dropped = 0
    for it in items:
        try:
            a = int(it["from"])
            b = int(it["to"])
        except (KeyError, TypeError, ValueError):
            dropped += 1
            continue
        if a > b:
            a, b = b, a
        if a < lo or b > hi:            # 模型幻想出範圍外的索引
            dropped += 1
            continue
        kind = str(it.get("kind", "filler"))
        span = b - a + 1
        if kind == "tangent" and not allow_tangent:
            dropped += 1
            continue
        if span > 40 and kind != "tangent":   # 非離題卻要砍一大段 → 不信任
            dropped += 1
            continue
        out.append({
            "s": words[a]["s"],
            "e": words[b]["e"],
            "kind": kind,
            "text": " ".join(w["w"] for w in words[a:b + 1])[:120],
            "reason": str(it.get("reason", ""))[:120],
        })
    if dropped:
        warn("03", f"忽略 {dropped} 筆不合理的刪除建議")
    return out


def _stats(removals) -> dict:
    st: dict[str, dict] = {}
    for r in removals:
        k = r.get("kind", "other")
        e = st.setdefault(k, {"count": 0, "seconds": 0.0})
        e["count"] += 1
        e["seconds"] = round(e["seconds"] + (r["e"] - r["s"]), 2)
    return st


def _write_report(cfg, out) -> None:
    """人看的剪輯清單 —— 進 FCP 前務必掃一眼。"""
    lines = ["# 剪輯決策報表", ""]
    for k, v in out["stats"].items():
        lines.append(f"- {_KIND_LABEL.get(k, k)}：{v['count']} 處，共 {v['seconds']:.1f} 秒")
    lines += ["", f"原始長度 {fmt_hhmmss(out['duration_src'])} → "
                  f"剪後 {fmt_hhmmss(out['duration_edit'])}", "",
              "## 被刪掉的內容", ""]
    for r in out["removals"]:
        if r["kind"] == "pause":
            continue
        lines.append(f'{fmt_hhmmss(r["s"])}  [{_KIND_LABEL.get(r["kind"], r["kind"])}] '
                     f'「{r["text"]}」 — {r["reason"]}')
    write_text(cfg.build_file("03_cuts.txt"), "\n".join(lines))
