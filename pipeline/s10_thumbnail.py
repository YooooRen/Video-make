"""Stage 10：挑一張最好的畫面，合成 YouTube 封面圖。"""
from __future__ import annotations

from pathlib import Path

from . import render
from .timeline import EditMap, Rate
from .util import clamp, log, read_json, run, warn, write_json


def run_stage(cfg, claude=None) -> dict:
    if not cfg.get("thumbnail.enabled", True):
        log("10", "thumbnail.enabled=false，跳過")
        return {}
    ingest = read_json(cfg.build_file("01_ingest.json"))
    cuts = read_json(cfg.build_file("03_cuts.json"))
    pkg = read_json(cfg.build_file("09_package.json"), default={})
    rate = Rate(ingest["sequence"]["fps_num"], ingest["sequence"]["fps_den"])
    emap = EditMap([tuple(k) for k in cuts["keeps"]], rate)

    frames = _candidates(cfg, ingest["source"]["path"], emap)
    if not frames:
        warn("10", "抽不到候選畫面，跳過封面圖")
        return {}

    pick, focus = _choose(cfg, claude, frames)
    title = cfg.get("thumbnail.title") or _auto_title(pkg)
    subtitle = cfg.get("thumbnail.subtitle") or _auto_subtitle(pkg)

    out = _compose(cfg, pick, title, subtitle, focus)
    log("10", f"封面圖完成 → {out}")
    result = {"path": str(out), "source_frame": str(pick),
              "title": title, "subtitle": subtitle}
    write_json(cfg.build_file("10_thumbnail.json"), result)
    return result


def _candidates(cfg, src: str, emap: EditMap) -> list[Path]:
    n = max(4, int(cfg.get("thumbnail.candidates", 12)))
    out_dir = cfg.build / "thumb_candidates"
    out_dir.mkdir(parents=True, exist_ok=True)
    frames: list[Path] = []
    for i in range(n):
        # 在成品時間軸上均勻取樣，再換算回原始影片的時間
        edit_t = emap.duration * (0.08 + 0.84 * i / max(1, n - 1))
        t = emap.edit_to_src(edit_t)
        p = out_dir / f"c{i:02d}.jpg"
        if not p.exists():
            try:
                run(["ffmpeg", "-y", "-v", "error", "-ss", f"{t:.2f}", "-i", src,
                     "-frames:v", "1", "-vf", "scale=1280:-2", str(p)])
            except Exception as exc:  # noqa: BLE001
                warn("10", f"抽格失敗 {t:.1f}s：{exc}")
                continue
        frames.append(p)
    return frames


def _choose(cfg, claude, frames: list[Path]) -> tuple[Path, dict | None]:
    if claude is None:
        return frames[len(frames) // 2], None
    listing = "\n".join(f"{i}: {p.name}" for i, p in enumerate(frames))
    prompt = (
        "這些是同一支帆船訪談影片的候選封面畫面。\n" + listing + "\n\n"
        "請挑出**最適合當 YouTube 封面**的一張。判斷標準：\n"
        "- 人物表情生動、有情緒（在講話、在笑、手勢自然），不要面無表情或閉眼\n"
        "- 臉部清楚、對焦準確、光線好\n"
        "- 構圖有留白可以放標題文字\n"
        "- 不要挑到眨眼、講話講一半嘴型很怪的畫面\n\n"
        "同時告訴我畫面中主要人臉的中心位置（x、y 正規化到 0–1）。\n\n"
        '只輸出 JSON：{"index": 5, "face": {"x": 0.62, "y": 0.38}, "why": "他正在笑，光線好"}')
    try:
        data = claude.ask_json(prompt, images=[str(p) for p in frames], label="10")
    except Exception as exc:  # noqa: BLE001
        warn("10", f"挑圖失敗（{exc}），改用中間那張")
        return frames[len(frames) // 2], None
    idx = data.get("index", len(frames) // 2) if isinstance(data, dict) else len(frames) // 2
    try:
        idx = int(idx)
    except (TypeError, ValueError):
        idx = len(frames) // 2
    idx = int(clamp(idx, 0, len(frames) - 1))
    face = data.get("face") if isinstance(data, dict) else None
    if isinstance(face, dict):
        face = {"x": clamp(float(face.get("x", 0.5)), 0, 1),
                "y": clamp(float(face.get("y", 0.4)), 0, 1)}
    else:
        face = None
    if isinstance(data, dict) and data.get("why"):
        log("10", f'選第 {idx} 張：{data["why"]}')
    return frames[idx], face


def _auto_title(pkg) -> str:
    titles = pkg.get("titles") or []
    return str(titles[0])[:16] if titles else "帆船船長訪談"


def _auto_subtitle(pkg) -> str:
    people = [p for p in (pkg.get("people") or []) if isinstance(p, dict)]
    for p in people:
        if "船長" in str(p.get("role", "")) or "captain" in str(p.get("role", "")).lower():
            return str(p.get("name", ""))[:28]
    return ""


def _compose(cfg, frame: Path, title: str, subtitle: str, focus: dict | None) -> Path:
    from PIL import Image, ImageDraw, ImageFilter

    W = int(cfg.get("thumbnail.width", 1280))
    H = int(cfg.get("thumbnail.height", 720))
    font_path = render.resolve_font(cfg)

    img = Image.open(frame).convert("RGB")
    img = _crop_to(img, W, H, focus)

    # 文字放在人臉的另一邊，避免蓋到臉
    left_side = (focus or {}).get("x", 0.5) > 0.5

    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(overlay)
    band_w = int(W * 0.55)
    x0 = 0 if left_side else W - band_w
    for i in range(band_w):
        a = int(200 * (1 - i / band_w) ** 1.4)
        x = x0 + i if left_side else x0 + band_w - 1 - i
        d.line([(x, 0), (x, H)], fill=(4, 10, 18, a))
    img = Image.alpha_composite(img.convert("RGBA"), overlay)

    d = ImageDraw.Draw(img)
    pad = int(W * 0.055)
    tx = pad if left_side else W - band_w + pad

    f_title = render._pil_font(font_path, int(H * 0.135))
    lines = render._wrap_cjk(title, 8) or [title]
    lines = lines[:2]
    ty = int(H * 0.30) if not subtitle else int(H * 0.26)
    for line in lines:
        # 描邊讓字在任何背景上都看得清楚
        for dx in (-3, 0, 3):
            for dy in (-3, 0, 3):
                if dx or dy:
                    d.text((tx + dx, ty + dy), line, font=f_title, fill=(0, 0, 0, 220))
        d.text((tx, ty), line, font=f_title, fill=(255, 255, 255, 255))
        ty += int(f_title.size * 1.16)

    d.rectangle([tx, ty + int(H * 0.02), tx + int(W * 0.14), ty + int(H * 0.035)],
                fill=(253, 191, 61, 255))

    if subtitle:
        f_sub = render._pil_font(font_path, int(H * 0.052))
        sy = ty + int(H * 0.075)
        for dx in (-2, 0, 2):
            for dy in (-2, 0, 2):
                if dx or dy:
                    d.text((tx + dx, sy + dy), subtitle, font=f_sub, fill=(0, 0, 0, 200))
        d.text((tx, sy), subtitle, font=f_sub, fill=(235, 242, 248, 255))

    out = cfg.build_file("10_thumbnail.png")
    img.convert("RGB").save(out, quality=95)
    return out


def _crop_to(img, W: int, H: int, focus: dict | None):
    """裁成 16:9，並盡量把臉留在畫面裡。"""
    from PIL import Image
    iw, ih = img.size
    target = W / H
    if iw / ih > target:
        new_w = int(ih * target)
        cx = int((focus or {}).get("x", 0.5) * iw)
        left = int(clamp(cx - new_w / 2, 0, iw - new_w))
        img = img.crop((left, 0, left + new_w, ih))
    else:
        new_h = int(iw / target)
        cy = int((focus or {}).get("y", 0.45) * ih)
        top = int(clamp(cy - new_h / 2, 0, ih - new_h))
        img = img.crop((0, top, iw, top + new_h))
    return img.resize((W, H), Image.LANCZOS)
