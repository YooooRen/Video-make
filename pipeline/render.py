"""說明短片的實際繪製：航線地圖動畫、名詞說明卡。輸出含 alpha 的 ProRes 4444。"""
from __future__ import annotations

import math
import shutil
from pathlib import Path

from .util import StageError, log, run, warn

_THEMES = {
    "dark": {
        "sea": (0.055, 0.098, 0.157, 0.86),
        "land": (0.129, 0.180, 0.235, 1.0),
        "coast": (0.30, 0.40, 0.48, 1.0),
        "grid": (1, 1, 1, 0.07),
        "route": (0.99, 0.75, 0.24, 1.0),
        "glow": (0.99, 0.75, 0.24, 0.25),
        "dot": (1, 1, 1, 1.0),
        "text": (1, 1, 1, 1.0),
        "sub": (0.72, 0.80, 0.86, 1.0),
        "panel": (0.02, 0.04, 0.07, 0.72),
    },
    "light": {
        "sea": (0.85, 0.91, 0.96, 0.88),
        "land": (0.96, 0.95, 0.92, 1.0),
        "coast": (0.55, 0.60, 0.64, 1.0),
        "grid": (0, 0, 0, 0.06),
        "route": (0.86, 0.31, 0.16, 1.0),
        "glow": (0.86, 0.31, 0.16, 0.20),
        "dot": (0.10, 0.12, 0.15, 1.0),
        "text": (0.08, 0.10, 0.13, 1.0),
        "sub": (0.30, 0.35, 0.40, 1.0),
        "panel": (1, 1, 1, 0.80),
    },
}


# ------------------------------------------------------------------ 字型 ---

def resolve_font(cfg) -> str:
    explicit = cfg.get("explainers.font_path", "")
    if explicit and Path(explicit).exists():
        return explicit
    for cand in cfg.get("explainers.font_candidates", []) or []:
        if Path(cand).exists():
            return cand
    raise StageError(
        "找不到中文字型。請在 project.yaml 的 explainers.font_path 指定一個 "
        ".ttf/.ttc（macOS 常見：/System/Library/Fonts/PingFang.ttc）。")


def _pil_font(path: str, size: int):
    from PIL import ImageFont
    try:
        return ImageFont.truetype(path, size)
    except OSError:
        # .ttc 需要指定 index
        for idx in range(1, 8):
            try:
                return ImageFont.truetype(path, size, index=idx)
            except OSError:
                continue
        raise


# ------------------------------------------------------------ ffmpeg 收尾 ---

def _encode(frames_glob: str, out: Path, fps: int, *, fade: float,
            duration: float, loop_image: bool = False) -> Path:
    vf = []
    if fade > 0:
        vf.append(f"fade=t=in:st=0:d={fade:.2f}:alpha=1")
        vf.append(f"fade=t=out:st={max(0.0, duration - fade):.2f}:d={fade:.2f}:alpha=1")
    vf.append("format=yuva444p10le")

    cmd = ["ffmpeg", "-y", "-v", "error"]
    if loop_image:
        cmd += ["-loop", "1", "-t", f"{duration:.3f}", "-framerate", str(fps), "-i", frames_glob]
    else:
        cmd += ["-framerate", str(fps), "-i", frames_glob]
    cmd += ["-vf", ",".join(vf), "-c:v", "prores_ks", "-profile:v", "4444",
            "-pix_fmt", "yuva444p10le", "-r", str(fps), str(out)]
    run(cmd)
    return out


# -------------------------------------------------------------- 航線地圖 ---

def render_route(cfg, item: dict, out: Path) -> Path | None:
    """畫一段航線動畫：底圖 + 逐漸推進的航跡 + 地點標籤。"""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib import font_manager

    wps = [w for w in item.get("waypoints", [])
           if isinstance(w, dict) and w.get("lat") is not None and w.get("lon") is not None]
    if len(wps) < 2:
        return None

    theme = _THEMES.get(cfg.get("explainers.route_style", "dark"), _THEMES["dark"])
    W = int(cfg.get("explainers.width", 1920))
    H = int(cfg.get("explainers.height", 1080))
    fps = int(cfg.get("explainers.fps", 30))
    duration = float(item.get("duration") or cfg.get("explainers.duration", 6.0))
    n_frames = max(2, int(duration * fps))

    font_path = resolve_font(cfg)
    font_manager.fontManager.addfont(font_path)
    fam = font_manager.FontProperties(fname=font_path).get_name()
    plt.rcParams["font.family"] = fam
    plt.rcParams["axes.unicode_minus"] = False

    lats = [float(w["lat"]) for w in wps]
    lons = [float(w["lon"]) for w in wps]
    pad = max(1.2, max(max(lats) - min(lats), max(lons) - min(lons)) * 0.45)
    extent = [min(lons) - pad, max(lons) + pad, min(lats) - pad, max(lats) + pad]
    # 依畫面比例調整經度範圍，避免地圖被拉扁
    aspect = W / H
    lat_span = extent[3] - extent[2]
    want_lon = lat_span * aspect / math.cos(math.radians(sum(lats) / len(lats)))
    if want_lon > extent[1] - extent[0]:
        cx = (extent[0] + extent[1]) / 2
        extent[0], extent[1] = cx - want_lon / 2, cx + want_lon / 2

    frames_dir = out.parent / f"_{out.stem}"
    if frames_dir.exists():
        shutil.rmtree(frames_dir)
    frames_dir.mkdir(parents=True)

    # 航跡取樣點（沿折線等距內插）
    path_pts = _densify(list(zip(lons, lats)), 400)

    ccrs = cfeature = None
    try:
        import cartopy.crs as _ccrs
        import cartopy.feature as _cfeature
        ccrs, cfeature = _ccrs, _cfeature
    except Exception:
        warn("06", "沒有 cartopy，航線圖改用簡化底圖（沒有海岸線）。"
                   "想要真實海岸線請 `pip install cartopy`。")

    for i in range(n_frames):
        p = i / (n_frames - 1)
        # 前 15% 淡入底圖，之後畫線，最後 15% 停住
        draw = min(1.0, max(0.0, (p - 0.12) / 0.68))
        fig = plt.figure(figsize=(W / 100, H / 100), dpi=100)
        if ccrs is not None:
            ax = fig.add_axes([0, 0, 1, 1], projection=ccrs.PlateCarree())
            ax.set_extent(extent, crs=ccrs.PlateCarree())
            ax.add_feature(cfeature.OCEAN.with_scale("50m"), facecolor=theme["sea"], zorder=0)
            ax.add_feature(cfeature.LAND.with_scale("50m"), facecolor=theme["land"], zorder=1)
            ax.add_feature(cfeature.COASTLINE.with_scale("50m"),
                           edgecolor=theme["coast"], linewidth=0.8, zorder=2)
            tf = {"transform": ccrs.PlateCarree()}
        else:
            ax = fig.add_axes([0, 0, 1, 1])
            ax.set_xlim(extent[0], extent[1])
            ax.set_ylim(extent[2], extent[3])
            ax.set_facecolor(theme["sea"])
            ax.add_patch(plt.Rectangle((extent[0], extent[2]),
                                       extent[1] - extent[0], extent[3] - extent[2],
                                       facecolor=theme["sea"], zorder=0))
            tf = {}
        ax.set_axis_off()
        for gl in _grid_lines(extent):
            ax.plot(*gl, color=theme["grid"], linewidth=0.8, zorder=3, **tf)

        k = max(2, int(len(path_pts) * draw))
        seg = path_pts[:k]
        if len(seg) > 1:
            xs, ys = zip(*seg)
            ax.plot(xs, ys, color=theme["glow"], linewidth=9, solid_capstyle="round",
                    zorder=4, **tf)
            ax.plot(xs, ys, color=theme["route"], linewidth=3.2, solid_capstyle="round",
                    zorder=5, **tf)
            ax.scatter([xs[-1]], [ys[-1]], s=110, color=theme["route"],
                       edgecolors=theme["dot"], linewidths=1.6, zorder=7, **tf)

        # 標籤：航跡推進到該點才顯示
        for wi, w in enumerate(wps):
            frac = wi / (len(wps) - 1)
            if draw + 1e-6 < frac and wi > 0:
                continue
            ax.scatter([float(w["lon"])], [float(w["lat"])], s=70, color=theme["dot"],
                       edgecolors=theme["route"], linewidths=2, zorder=8, **tf)
            label = w.get("name_zh") or w.get("name_en") or ""
            # 用資料座標做位移，cartopy 與純 matplotlib 兩種底圖都適用
            dx = (extent[1] - extent[0]) * 0.012
            dy = (extent[3] - extent[2]) * 0.015
            ax.text(float(w["lon"]) + dx, float(w["lat"]) + dy, label,
                    color=theme["text"], fontsize=19, fontweight="bold", zorder=9,
                    va="bottom", ha="left",
                    bbox=dict(boxstyle="round,pad=0.34", facecolor=theme["panel"],
                              edgecolor="none"),
                    **tf)

        _title_block(fig, item.get("title_zh", ""), item.get("note_zh", ""), theme, font_path)
        fig.savefig(frames_dir / f"{i:05d}.png", transparent=True, dpi=100)
        plt.close(fig)

    _encode(str(frames_dir / "%05d.png"), out, fps,
            fade=float(cfg.get("explainers.fade", 0.5)), duration=duration)
    shutil.rmtree(frames_dir, ignore_errors=True)
    return out


def _densify(pts, n: int):
    """沿折線等距取樣，讓動畫推進速度均勻。"""
    segs = []
    total = 0.0
    for a, b in zip(pts, pts[1:]):
        d = math.dist(a, b)
        segs.append((a, b, d))
        total += d
    if total == 0:
        return list(pts)
    out = []
    for i in range(n):
        target = total * i / (n - 1)
        acc = 0.0
        for a, b, d in segs:
            if acc + d >= target or (a, b, d) is segs[-1]:
                t = 0.0 if d == 0 else (target - acc) / d
                t = min(1.0, max(0.0, t))
                out.append((a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t))
                break
            acc += d
    return out


def _grid_lines(extent):
    x0, x1, y0, y1 = extent
    step = 1.0
    span = max(x1 - x0, y1 - y0)
    for s in (0.5, 1, 2, 5, 10, 20):
        if span / s <= 8:
            step = s
            break
    lines = []
    v = math.ceil(x0 / step) * step
    while v <= x1:
        lines.append(([v, v], [y0, y1]))
        v += step
    v = math.ceil(y0 / step) * step
    while v <= y1:
        lines.append(([x0, x1], [v, v]))
        v += step
    return lines


def _title_block(fig, title: str, note: str, theme, font_path: str) -> None:
    if not title:
        return
    fig.text(0.045, 0.905, title, color=theme["text"], fontsize=44, fontweight="bold",
             va="top", ha="left",
             bbox=dict(boxstyle="round,pad=0.5", facecolor=theme["panel"], edgecolor="none"))
    if note:
        fig.text(0.05, 0.845, note, color=theme["sub"], fontsize=24, va="top", ha="left")


# -------------------------------------------------------------- 名詞小卡 ---

def render_term(cfg, item: dict, out: Path) -> Path | None:
    """畫一張名詞說明卡（下三分之一的橫幅），輸出短片。"""
    from PIL import Image, ImageDraw

    theme = _THEMES.get(cfg.get("explainers.route_style", "dark"), _THEMES["dark"])
    W = int(cfg.get("explainers.width", 1920))
    H = int(cfg.get("explainers.height", 1080))
    fps = int(cfg.get("explainers.fps", 30))
    duration = float(item.get("duration") or cfg.get("explainers.duration", 6.0))
    font_path = resolve_font(cfg)

    term_zh = item.get("term_zh") or item.get("term_en") or ""
    term_en = item.get("term_en", "")
    explain = item.get("explain_zh", "")
    if not term_zh:
        return None

    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    f_title = _pil_font(font_path, int(H * 0.052))
    f_en = _pil_font(font_path, int(H * 0.027))
    f_body = _pil_font(font_path, int(H * 0.030))

    margin = int(W * 0.055)
    pad = int(H * 0.035)
    body_lines = _wrap_cjk(explain, 24)

    box_h = pad * 2 + f_title.size + int(f_en.size * 1.4) + \
        len(body_lines) * int(f_body.size * 1.5)
    box_w = int(W * 0.52)
    x0 = margin
    y0 = H - int(H * 0.13) - box_h

    def rgba(c):
        return tuple(int(v * 255) for v in c)

    d.rounded_rectangle([x0, y0, x0 + box_w, y0 + box_h], radius=int(H * 0.022),
                        fill=rgba(theme["panel"]))
    d.rounded_rectangle([x0, y0, x0 + int(W * 0.006), y0 + box_h],
                        radius=int(H * 0.004), fill=rgba(theme["route"]))

    tx, ty = x0 + pad + int(W * 0.008), y0 + pad
    d.text((tx, ty), term_zh, font=f_title, fill=rgba(theme["text"]))
    ty += int(f_title.size * 1.15)
    if term_en:
        d.text((tx, ty), term_en, font=f_en, fill=rgba(theme["route"]))
    ty += int(f_en.size * 1.6)
    for line in body_lines:
        d.text((tx, ty), line, font=f_body, fill=rgba(theme["sub"]))
        ty += int(f_body.size * 1.5)

    png = out.parent / f"_{out.stem}.png"
    img.save(png)
    _encode(str(png), out, fps, fade=float(cfg.get("explainers.fade", 0.5)),
            duration=duration, loop_image=True)
    png.unlink(missing_ok=True)
    return out


def _wrap_cjk(text: str, per_line: int) -> list[str]:
    text = (text or "").strip()
    if not text:
        return []
    out, cur = [], ""
    for ch in text:
        cur += ch
        if len(cur) >= per_line and ch in "，。、；：！？ ":
            out.append(cur.strip())
            cur = ""
        elif len(cur) >= per_line + 6:
            out.append(cur.strip())
            cur = ""
    if cur.strip():
        out.append(cur.strip())
    return out[:4]
