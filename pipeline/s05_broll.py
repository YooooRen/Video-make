"""Stage 05：建立 B-roll 素材索引（AI 看畫面），再決定插入位置。"""
from __future__ import annotations

import concurrent.futures as futures
import math
from pathlib import Path

from .claude_client import load_prompt
from .util import (StageError, file_sig, fmt_hhmmss, log, media_info,
                   read_json, run, warn, write_json)


def run_stage(cfg, claude=None) -> dict:
    ingest = read_json(cfg.build_file("01_ingest.json"))
    subs = read_json(cfg.build_file("04_subtitles.json"))
    empty = {"library": [], "placements": []}

    if not cfg.get("broll.enabled", True):
        log("05", "broll.enabled=false，跳過")
        return _save(cfg, empty)
    cand = ingest["broll_candidates"]
    if not cand["videos"] and not cand["images"]:
        warn("05", "素材資料夾是空的（或沒設定 project.media_dir），跳過 B-roll")
        return _save(cfg, empty)
    if claude is None:
        warn("05", "沒有 Claude client，跳過 B-roll")
        return _save(cfg, empty)

    library = _index(cfg, claude, cand)
    usable = [a for a in library if a.get("usable", True)]
    log("05", f"素材索引完成：{len(library)} 份，其中 {len(usable)} 份堪用")
    if not usable:
        return _save(cfg, {"library": library, "placements": []})

    placements = _place(cfg, claude, subs, usable)
    log("05", f"排入 {len(placements)} 段 B-roll")
    return _save(cfg, {"library": library, "placements": placements})


def _save(cfg, data) -> dict:
    write_json(cfg.build_file("05_broll.json"), data)
    return data


# ------------------------------------------------------------- 素材索引 ----

def _index(cfg, claude, cand) -> list[dict]:
    cache_path = cfg.build_file("05_broll_index.json")
    cache = {e["sig"]: e for e in read_json(cache_path, default=[])}
    sheets_dir = cfg.build / "broll_sheets"
    sheets_dir.mkdir(parents=True, exist_ok=True)

    jobs: list[dict] = []
    for i, p in enumerate(cand["videos"]):
        jobs.append({"id": f"A{i:03d}", "path": p, "kind": "video"})
    for j, p in enumerate(cand["images"]):
        jobs.append({"id": f"P{j:03d}", "path": p, "kind": "image"})

    tmpl = load_prompt("broll_describe.md")
    results: list[dict] = []

    def work(job: dict) -> dict:
        path = Path(job["path"])
        try:
            sig = file_sig(path)
        except OSError as exc:
            warn("05", f"讀不到 {path.name}（{exc}），略過")
            return {}
        base = dict(job)
        if job["kind"] == "video":
            info = media_info(path)
            base.update({"duration": info["duration"], "width": info.get("width", 0),
                         "height": info.get("height", 0), "has_audio": info["has_audio"],
                         "fps_num": info.get("fps_num", 30), "fps_den": info.get("fps_den", 1)})
        else:
            base.update({"duration": float(cfg.get("broll.still_duration", 4.0)),
                         "has_audio": False})
        base["sig"] = sig

        hit = cache.get(sig)
        if hit:
            return {**base, **{k: hit[k] for k in
                               ("summary", "tags", "subjects", "shot_type", "motion",
                                "time_of_day", "usable", "quality_note") if k in hit}}

        sheet = _contact_sheet(cfg, path, job["kind"], sheets_dir, job["id"])
        if sheet is None:
            return {**base, "summary": path.stem, "tags": [], "usable": False,
                    "quality_note": "無法產生預覽"}
        prompt = (tmpl.replace("{{NAME}}", path.name)
                      .replace("{{KIND}}", "影片" if job["kind"] == "video" else "照片")
                      .replace("{{DURATION}}", f'{base["duration"]:.1f} 秒'
                               if job["kind"] == "video" else "靜態照片"))
        try:
            data = claude.ask_json(prompt, images=[str(sheet)], label="05")
        except Exception as exc:  # noqa: BLE001
            warn("05", f"{path.name} 辨識失敗（{exc}）")
            return {**base, "summary": path.stem, "tags": [], "usable": False,
                    "quality_note": "辨識失敗"}
        if not isinstance(data, dict):
            data = {}
        return {**base,
                "summary": str(data.get("summary", ""))[:120],
                "tags": [str(t) for t in (data.get("tags") or [])][:8],
                "subjects": [str(t) for t in (data.get("subjects") or [])][:8],
                "shot_type": str(data.get("shot_type", "")),
                "motion": str(data.get("motion", "")),
                "time_of_day": str(data.get("time_of_day", "")),
                "usable": bool(data.get("usable", True)),
                "quality_note": str(data.get("quality_note", ""))[:80]}

    workers = max(1, int(cfg.get("broll.index_concurrency", 3)))
    log("05", f"辨識 {len(jobs)} 份素材內容（{workers} 條並行，結果會快取）")
    with futures.ThreadPoolExecutor(max_workers=workers) as pool:
        for n, res in enumerate(pool.map(work, jobs), 1):
            if res:
                results.append(res)
            if n % 10 == 0:
                log("05", f"  ...{n}/{len(jobs)}")

    write_json(cache_path, results)
    return results


def _contact_sheet(cfg, path: Path, kind: str, out_dir: Path, aid: str) -> Path | None:
    """把一支影片抽成 N 格拼貼圖；照片則直接縮圖。"""
    out = out_dir / f"{aid}.jpg"
    if out.exists():
        return out
    try:
        if kind == "image":
            run(["ffmpeg", "-y", "-v", "error", "-i", str(path),
                 "-vf", "scale=1024:-2", "-frames:v", "1", str(out)])
            return out

        info = media_info(path)
        dur = info["duration"]
        n = max(1, int(cfg.get("broll.frames_per_clip", 6)))
        if dur < 1.0:
            n = 1
        cols = max(1, int(cfg.get("broll.contact_sheet_cols", 3)))
        rows = math.ceil(n / cols)

        tmp = out_dir / f"_{aid}"
        tmp.mkdir(exist_ok=True)
        for k in range(n):
            t = dur * (0.05 + 0.9 * (k / max(1, n - 1) if n > 1 else 0.5))
            run(["ffmpeg", "-y", "-v", "error", "-ss", f"{t:.2f}", "-i", str(path),
                 "-frames:v", "1", "-vf", "scale=480:-2", str(tmp / f"{k:03d}.jpg")])
        frames = sorted(tmp.glob("*.jpg"))
        if not frames:
            return None
        if len(frames) == 1:
            frames[0].replace(out)
        else:
            run(["ffmpeg", "-y", "-v", "error", "-i", str(tmp / "%03d.jpg"),
                 "-filter_complex", f"tile={cols}x{rows}:padding=4:color=black",
                 "-frames:v", "1", str(out)])
            for f in frames:
                f.unlink(missing_ok=True)
        tmp.rmdir()
        return out
    except Exception as exc:  # noqa: BLE001
        warn("05", f"{path.name} 預覽失敗：{exc}")
        return None


# --------------------------------------------------------------- 排片 ------

def _place(cfg, claude, subs, library) -> list[dict]:
    lines = []
    for c in subs["cues"]:
        text = c["en"].replace("\n", " ")
        lines.append(f'{c["s"]:.1f} {c["speaker"]}: {text}')
    transcript = "\n".join(lines)[:120000]

    cat = []
    for a in library:
        tags = "、".join(a.get("tags", []))
        extra = f'{a["duration"]:.1f}s {a.get("shot_type","")} {a.get("motion","")}'
        cat.append(f'{a["id"]} | {extra} | {a.get("summary","")} | {tags}')
    catalogue = "\n".join(cat)

    p = (load_prompt("broll_place.md")
         .replace("{{TRANSCRIPT}}", transcript)
         .replace("{{LIBRARY}}", catalogue)
         .replace("{{MIN_DUR}}", str(cfg.get("broll.min_duration", 2.5)))
         .replace("{{MAX_DUR}}", str(cfg.get("broll.max_duration", 6.0)))
         .replace("{{MIN_GAP}}", str(cfg.get("broll.min_gap", 12.0)))
         .replace("{{HEAD}}", str(cfg.get("broll.protect_head", 8.0)))
         .replace("{{TAIL}}", str(cfg.get("broll.protect_tail", 5.0)))
         .replace("{{MAX_COUNT}}", str(cfg.get("broll.max_count", 40))))

    log("05", "決定 B-roll 插入位置")
    try:
        data = claude.ask_json(p, label="05")
    except Exception as exc:  # noqa: BLE001
        warn("05", f"排片失敗（{exc}），這次不放 B-roll")
        return []
    return _validate(cfg, data, library, float(subs["duration"]))


def _validate(cfg, data, library, total: float) -> list[dict]:
    by_id = {a["id"]: a for a in library}
    min_d = float(cfg.get("broll.min_duration", 2.5))
    max_d = float(cfg.get("broll.max_duration", 6.0))
    min_gap = float(cfg.get("broll.min_gap", 12.0))
    head = float(cfg.get("broll.protect_head", 8.0))
    tail = float(cfg.get("broll.protect_tail", 5.0))
    limit = int(cfg.get("broll.max_count", 40))

    items = data if isinstance(data, list) else data.get("placements", [])
    out: list[dict] = []
    last_end = -1e9
    dropped = 0
    for it in sorted((i for i in items if isinstance(i, dict)),
                     key=lambda x: float(x.get("at", 0))):
        asset = by_id.get(str(it.get("asset", "")))
        if asset is None or not asset.get("usable", True):
            dropped += 1
            continue
        at = float(it.get("at", 0))
        dur = float(it.get("duration", min_d))
        dur = max(min_d, min(max_d, dur, asset["duration"]))
        if at < head or at + dur > total - tail:
            dropped += 1
            continue
        if at - last_end < min_gap:
            dropped += 1
            continue
        # 影片素材取中段，避開頭尾可能的手震
        src_in = max(0.0, (asset["duration"] - dur) / 2) if asset["kind"] == "video" else 0.0
        out.append({
            "at": round(at, 3), "duration": round(dur, 3),
            "asset": asset["id"], "path": asset["path"], "kind": asset["kind"],
            "src_in": round(src_in, 3), "has_audio": bool(asset.get("has_audio")),
            "reason": str(it.get("reason", ""))[:120],
        })
        last_end = at + dur
        if len(out) >= limit:
            break
    if dropped:
        log("05", f"依規則濾掉 {dropped} 個不合適的建議")
    return out
