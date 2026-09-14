"""Stage 05：找出航線／專有名詞，生成說明短片（含 alpha 的 ProRes 4444）。"""
from __future__ import annotations

from pathlib import Path

from . import render
from .claude_client import load_prompt
from .util import StageError, log, read_json, warn, write_json


def run_stage(cfg, claude=None) -> dict:
    subs = read_json(cfg.build_file("04_subtitles.json"))
    empty = {"items": []}

    if not cfg.get("explainers.enabled", True):
        log("05", "explainers.enabled=false，跳過")
        return _save(cfg, empty)
    if claude is None:
        warn("05", "沒有 Claude client，跳過說明短片")
        return _save(cfg, empty)

    items = _extract(cfg, claude, subs)
    if not items:
        log("05", "沒有找到值得做說明的內容")
        return _save(cfg, empty)

    items = _schedule(cfg, items, float(subs["duration"]))
    log("05", f"排定 {len(items)} 段說明短片，開始算圖")

    out_dir = cfg.build / "explainers"
    out_dir.mkdir(parents=True, exist_ok=True)
    rendered: list[dict] = []
    for i, it in enumerate(items):
        name = f'{i:02d}_{it["kind"]}'
        out = out_dir / f"{name}.mov"
        if out.exists():
            log("05", f"  {name} 已存在，沿用")
            rendered.append({**it, "path": str(out)})
            continue
        log("05", f"  算圖 {i+1}/{len(items)}：{it.get('title_zh') or it.get('term_zh')}")
        try:
            if it["kind"] == "route":
                made = render.render_route(cfg, it, out)
            else:
                made = render.render_term(cfg, it, out)
        except StageError:
            raise
        except Exception as exc:  # noqa: BLE001
            warn("05", f"  {name} 算圖失敗：{exc}")
            continue
        if made and made.exists():
            rendered.append({**it, "path": str(made)})

    log("05", f"完成 {len(rendered)} 段說明短片 → build/explainers/")
    return _save(cfg, {"items": rendered})


def _save(cfg, data) -> dict:
    write_json(cfg.build_file("05_explainers.json"), data)
    return data


def _extract(cfg, claude, subs) -> list[dict]:
    lines = [f'{c["s"]:.1f} {c["speaker"]}: {c["en"]}'.replace("\n", " ")
             for c in subs["cues"]]
    prompt = (load_prompt("explainer_extract.md")
              .replace("{{TRANSCRIPT}}", "\n".join(lines)[:120000])
              .replace("{{MAX_COUNT}}", str(cfg.get("explainers.max_count", 12))))
    try:
        data = claude.ask_json(prompt, label="05")
    except Exception as exc:  # noqa: BLE001
        warn("05", f"擷取失敗（{exc}），跳過說明短片")
        return []

    out: list[dict] = []
    for it in (data if isinstance(data, list) else []):
        if not isinstance(it, dict):
            continue
        kind = it.get("kind")
        if kind == "route":
            wps = [w for w in (it.get("waypoints") or [])
                   if isinstance(w, dict)
                   and isinstance(w.get("lat"), (int, float))
                   and isinstance(w.get("lon"), (int, float))
                   and -90 <= w["lat"] <= 90 and -180 <= w["lon"] <= 180]
            if len(wps) < 2:
                continue
            out.append({"kind": "route", "at": float(it.get("at", 0)),
                        "title_zh": str(it.get("title_zh", ""))[:40],
                        "note_zh": str(it.get("note_zh", ""))[:60],
                        "waypoints": wps})
        elif kind == "term":
            if not (it.get("term_zh") or it.get("term_en")):
                continue
            out.append({"kind": "term", "at": float(it.get("at", 0)),
                        "term_en": str(it.get("term_en", ""))[:60],
                        "term_zh": str(it.get("term_zh", ""))[:40],
                        "explain_zh": str(it.get("explain_zh", ""))[:120]})
    return out


def _schedule(cfg, items, total: float) -> list[dict]:
    """彼此不重疊、間隔夠遠、不超出片長；必要時往後挪。"""
    dur = float(cfg.get("explainers.duration", 6.0))
    gap = float(cfg.get("explainers.min_gap", 15.0))
    limit = int(cfg.get("explainers.max_count", 12))

    out: list[dict] = []
    last_end = -1e9
    for it in sorted(items, key=lambda x: x["at"]):
        at = max(0.0, float(it["at"]))
        if at - last_end < gap:
            at = last_end + gap
        if at + dur > total - 2.0:
            continue
        out.append({**it, "at": round(at, 3), "duration": dur})
        last_end = at + dur
        if len(out) >= limit:
            break
    return out
