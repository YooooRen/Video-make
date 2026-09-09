"""時間軸核心：分數時間、剪輯映射（原始時間 <-> 剪完時間）、字幕輸出。"""
from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Iterable, Sequence

from .util import StageError, write_text


# ------------------------------------------------------------------ Rate ----

@dataclass(frozen=True)
class Rate:
    """幀率。FCPXML 的時間一律要能被 frame duration 整除，否則 FCP 會拒收。"""
    num: int   # 例如 30000
    den: int   # 例如 1001

    @classmethod
    def parse(cls, text: str | None, fallback: "Rate | None" = None) -> "Rate":
        if not text:
            if fallback is None:
                raise StageError("沒有可用的幀率")
            return fallback
        text = str(text).strip()
        if "/" in text:
            n, d = text.split("/", 1)
            return cls(int(n), int(d))
        f = float(text)
        # 常見的 NTSC 小數幀率轉回精確分數
        for base in (24, 30, 60, 120):
            if abs(f - base * 1000 / 1001) < 0.01:
                return cls(base * 1000, 1001)
        frac = Fraction(f).limit_denominator(1001)
        return cls(frac.numerator, frac.denominator)

    @property
    def fps(self) -> float:
        return self.num / self.den

    @property
    def frame_duration(self) -> str:
        """FCPXML 的 frameDuration，例如 '1001/30000s'。"""
        return f"{self.den}/{self.num}s"

    def frames(self, seconds: float) -> int:
        return int(round(seconds * self.num / self.den))

    def seconds(self, frames: int) -> float:
        return frames * self.den / self.num

    def snap(self, seconds: float) -> float:
        """把秒數對齊到最近的幀邊界。"""
        return self.seconds(self.frames(seconds))

    def time(self, seconds: float) -> str:
        """秒 -> FCPXML 分數時間字串（必為幀的整數倍）。"""
        f = self.frames(seconds)
        if f == 0:
            return "0s"
        return f"{f * self.den}/{self.num}s"

    def time_frames(self, frames: int) -> str:
        if frames == 0:
            return "0s"
        return f"{frames * self.den}/{self.num}s"


# --------------------------------------------------------------- EditMap ----

class EditMap:
    """
    由「保留片段」清單構成的剪輯映射。

    keeps 是原始影片時間軸上的 [(start, end), ...]，依序接在一起就是成品。
    src_to_edit() 把原始時間換算成剪完後的時間，被剪掉的地方回傳 None。
    """

    def __init__(self, keeps: Sequence[tuple[float, float]], rate: Rate):
        self.rate = rate
        self.keeps: list[tuple[float, float]] = []
        self.starts: list[float] = []   # 每段在成品時間軸上的起點
        cursor = 0.0
        for s, e in keeps:
            s, e = rate.snap(s), rate.snap(e)
            if e - s <= 0:
                continue
            self.keeps.append((s, e))
            self.starts.append(cursor)
            cursor += e - s
        self.duration = cursor
        if not self.keeps:
            raise StageError("剪輯後沒有任何保留片段，請檢查 cleanup 設定。")

    @classmethod
    def passthrough(cls, duration: float, rate: Rate) -> "EditMap":
        return cls([(0.0, duration)], rate)

    @property
    def source_duration(self) -> float:
        return sum(e - s for s, e in self.keeps)

    def _index(self, t: float) -> int | None:
        lo, hi = 0, len(self.keeps) - 1
        while lo <= hi:
            mid = (lo + hi) // 2
            s, e = self.keeps[mid]
            if t < s:
                hi = mid - 1
            elif t >= e:
                lo = mid + 1
            else:
                return mid
        return None

    def src_to_edit(self, t: float) -> float | None:
        i = self._index(t)
        if i is None:
            return None
        s, _ = self.keeps[i]
        return self.starts[i] + (t - s)

    def src_to_edit_clamped(self, t: float) -> float:
        """被剪掉的時間點就吸附到最近的保留邊界，永遠回傳一個值。"""
        v = self.src_to_edit(t)
        if v is not None:
            return v
        for i, (s, e) in enumerate(self.keeps):
            if t < s:
                return self.starts[i]
        return self.duration

    def edit_to_src(self, t: float) -> float:
        for i, (s, e) in enumerate(self.keeps):
            start = self.starts[i]
            if start <= t < start + (e - s):
                return s + (t - start)
        return self.keeps[-1][1]


def build_keeps(duration: float, removals: Iterable[tuple[float, float]], *,
                rate: Rate, min_removal: float, min_keep: float,
                pad: float) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
    """
    把「要刪掉的區間」轉成「要保留的區間」。

    回傳 (keeps, effective_removals)。effective_removals 是實際生效的刪除
    （已套用 pad 縮回、已濾掉太短的），方便報表跟除錯。
    """
    # 1. 內縮 pad，避免切到相鄰字的字頭/字尾
    padded: list[tuple[float, float]] = []
    for s, e in removals:
        s2, e2 = s + pad, e - pad
        if e2 - s2 >= min_removal:
            padded.append((max(0.0, s2), min(duration, e2)))
    if not padded:
        return [(0.0, rate.snap(duration))], []

    # 2. 合併重疊的刪除區間
    padded.sort()
    merged: list[list[float]] = [list(padded[0])]
    for s, e in padded[1:]:
        if s <= merged[-1][1] + 1e-6:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])

    # 3. 取補集
    keeps: list[tuple[float, float]] = []
    cursor = 0.0
    for s, e in merged:
        if s - cursor > 1e-6:
            keeps.append((cursor, s))
        cursor = max(cursor, e)
    if duration - cursor > 1e-6:
        keeps.append((cursor, duration))

    # 4. 太短的保留片段：與前一段合併（把中間那刀取消），避免出現一格閃動
    cleaned: list[tuple[float, float]] = []
    for s, e in keeps:
        if e - s < min_keep and cleaned:
            cleaned[-1] = (cleaned[-1][0], e)
        else:
            cleaned.append((s, e))
    cleaned = [(rate.snap(s), rate.snap(e)) for s, e in cleaned if rate.snap(e) > rate.snap(s)]

    # 5. 依照最終保留區間反推真正被刪掉的部分
    effective: list[tuple[float, float]] = []
    cursor = 0.0
    for s, e in cleaned:
        if s - cursor > 1e-6:
            effective.append((cursor, s))
        cursor = e
    if duration - cursor > 1e-6:
        effective.append((cursor, duration))
    return cleaned, effective


def find_pauses(words: Sequence[dict], *, max_pause: float, keep_pause: float,
                duration: float) -> list[dict]:
    """純程式判斷過久停頓（不用 AI）：字與字之間的空白超過門檻就砍掉多餘部分。"""
    out: list[dict] = []
    prev_end = 0.0
    for w in words:
        gap = w["s"] - prev_end
        if gap > max_pause:
            out.append({
                "s": prev_end + keep_pause / 2,
                "e": w["s"] - keep_pause / 2,
                "kind": "pause",
                "text": "",
                "reason": f"靜默 {gap:.2f}s",
            })
        prev_end = max(prev_end, w["e"])
    tail = duration - prev_end
    if tail > max_pause:
        out.append({"s": prev_end + keep_pause, "e": duration, "kind": "pause",
                    "text": "", "reason": f"片尾靜默 {tail:.2f}s"})
    return out


# --------------------------------------------------------------- 字幕輸出 ---

def _srt_ts(t: float) -> str:
    t = max(0.0, t)
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _vtt_ts(t: float) -> str:
    return _srt_ts(t).replace(",", ".")


def write_srt(cues: Sequence[dict], path: str | Path, field: str = "text") -> Path:
    lines: list[str] = []
    n = 0
    for cue in cues:
        text = (cue.get(field) or "").strip()
        if not text:
            continue
        n += 1
        lines.append(str(n))
        lines.append(f"{_srt_ts(cue['s'])} --> {_srt_ts(cue['e'])}")
        lines.append(text)
        lines.append("")
    return write_text(path, "\n".join(lines))


def write_vtt(cues: Sequence[dict], path: str | Path, field: str = "text") -> Path:
    lines = ["WEBVTT", ""]
    for cue in cues:
        text = (cue.get(field) or "").strip()
        if not text:
            continue
        lines.append(f"{_vtt_ts(cue['s'])} --> {_vtt_ts(cue['e'])}")
        lines.append(text)
        lines.append("")
    return write_text(path, "\n".join(lines))
