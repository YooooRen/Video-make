"""Stage 02：faster-whisper 逐字轉錄（詞級時間戳）+ pyannote 說話者分離。"""
from __future__ import annotations

import os
from pathlib import Path

from .util import StageError, log, read_json, warn, write_json


def run_stage(cfg, claude=None) -> dict:
    ingest = read_json(cfg.build_file("01_ingest.json"))
    audio = ingest["audio"]
    duration = float(ingest["source"]["duration"])

    words, segments, language = _transcribe(cfg, audio)
    log("02", f"轉錄完成：{len(words)} 個字、{len(segments)} 個段落（語言 {language}）")

    speakers = _diarize(cfg, audio, duration)
    if speakers:
        _assign_speakers(words, segments, speakers)
        found = sorted({s["spk"] for s in segments if s.get("spk")})
        log("02", f"說話者分離：{len(found)} 位 {found}")
    else:
        for w in words:
            w["spk"] = "SPEAKER_00"
        for s in segments:
            s["spk"] = "SPEAKER_00"

    roles = _label_speakers(cfg, claude, segments)

    out = {
        "language": language,
        "duration": duration,
        "speakers": roles,
        "words": words,
        "segments": segments,
    }
    write_json(cfg.build_file("02_transcript.json"), out)
    _write_readable(cfg, out)
    return out


# --------------------------------------------------------------- whisper ----

def _transcribe(cfg, audio: str):
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise StageError(
            "缺少 faster-whisper。請執行：pip install faster-whisper") from exc

    device = cfg.get("transcribe.device", "auto")
    compute = cfg.get("transcribe.compute_type", "auto")
    if device == "auto":
        device = "cpu"
        try:
            import torch
            if torch.cuda.is_available():
                device = "cuda"
        except Exception:
            pass
    if compute == "auto":
        compute = "float16" if device == "cuda" else "int8"

    model_name = cfg.get("transcribe.model", "large-v3")
    log("02", f"載入 faster-whisper {model_name}（{device}/{compute}），首次執行會下載模型")
    model = WhisperModel(model_name, device=device, compute_type=compute)

    seg_iter, info = model.transcribe(
        audio,
        language=cfg.get("project.language") or None,
        beam_size=int(cfg.get("transcribe.beam_size", 5)),
        word_timestamps=True,
        vad_filter=bool(cfg.get("transcribe.vad_filter", True)),
        vad_parameters={"min_silence_duration_ms": 350},
    )

    words: list[dict] = []
    segments: list[dict] = []
    for si, seg in enumerate(seg_iter):
        w0 = len(words)
        for w in (seg.words or []):
            token = (w.word or "").strip()
            if not token:
                continue
            words.append({
                "i": len(words),
                "w": token,
                "s": round(float(w.start), 3),
                "e": round(float(w.end), 3),
                "p": round(float(getattr(w, "probability", 1.0) or 1.0), 3),
            })
        if len(words) == w0:
            continue
        segments.append({
            "id": len(segments),
            "s": words[w0]["s"],
            "e": words[-1]["e"],
            "w0": w0,
            "w1": len(words) - 1,
            "text": (seg.text or "").strip(),
        })
        if si % 25 == 0:
            log("02", f"  ...已處理到 {segments[-1]['e']/60:.1f} 分鐘")

    if not words:
        raise StageError("轉錄結果是空的，請確認影片有清楚的人聲。")
    return words, segments, info.language


# ------------------------------------------------------------- diarization --

def _diarize(cfg, audio: str, duration: float):
    if not cfg.get("transcribe.diarize", True):
        return None
    token = os.environ.get(cfg.get("transcribe.hf_token_env", "HF_TOKEN"), "")
    if not token:
        warn("02", "沒有 HF_TOKEN，跳過說話者分離（分鏡會退化成單一鏡位）。"
                   "取得方式見 README。")
        return None
    try:
        from pyannote.audio import Pipeline
    except ImportError:
        warn("02", "沒安裝 pyannote.audio，跳過說話者分離")
        return None

    log("02", "執行說話者分離（pyannote）…這一步比較慢")
    try:
        pipe = Pipeline.from_pretrained(
            "pyannote/speaker-diarization-3.1", use_auth_token=token)
        kwargs = {}
        n = int(cfg.get("transcribe.num_speakers", 0) or 0)
        if n > 0:
            kwargs["num_speakers"] = n
        ann = pipe(audio, **kwargs)
    except Exception as exc:  # noqa: BLE001
        warn("02", f"說話者分離失敗（{exc}），繼續但不分鏡")
        return None

    turns = [{"s": float(t.start), "e": float(t.end), "spk": str(label)}
             for t, _, label in ann.itertracks(yield_label=True)]
    turns.sort(key=lambda x: x["s"])
    return turns


def _assign_speakers(words, segments, turns):
    """每個字取「與 diarization 區間重疊最多」的說話者。"""
    for w in words:
        best, best_ov = None, 0.0
        for t in turns:
            if t["e"] <= w["s"]:
                continue
            if t["s"] >= w["e"]:
                break
            ov = min(t["e"], w["e"]) - max(t["s"], w["s"])
            if ov > best_ov:
                best, best_ov = t["spk"], ov
        w["spk"] = best or "SPEAKER_00"

    # 段落取多數決；並在說話者改變的地方切開段落
    for seg in segments:
        counts: dict[str, float] = {}
        for w in words[seg["w0"]:seg["w1"] + 1]:
            counts[w["spk"]] = counts.get(w["spk"], 0.0) + (w["e"] - w["s"])
        seg["spk"] = max(counts, key=counts.get) if counts else "SPEAKER_00"


# ------------------------------------------------------------ 角色標記 -----

def _label_speakers(cfg, claude, segments) -> dict:
    ids = sorted({s.get("spk", "SPEAKER_00") for s in segments})
    manual = cfg.get("speakers.map", {}) or {}
    roles = {sid: {"id": sid, "name": manual.get(sid, ""), "role": ""} for sid in ids}

    if all(roles[s]["name"] for s in ids) or claude is None or len(ids) < 2:
        for sid in ids:
            roles[sid]["name"] = roles[sid]["name"] or sid
            roles[sid]["role"] = "guest" if sid == ids[0] else "host"
        return roles

    sample = "\n".join(
        f'{s["spk"]}: {s["text"]}' for s in segments[:60])[:6000]
    prompt = (
        "以下是一段英文訪談的開頭逐字稿，說話者已被自動分離成代號。\n"
        f"提示：{cfg.get('speakers.host_hint', '')}\n\n"
        f"{sample}\n\n"
        "請判斷每個代號是主持人(host)還是受訪的帆船船長(guest)，"
        "並盡量從對話中找出他們的名字（找不到就填空字串）。\n"
        '只輸出 JSON：{"SPEAKER_00": {"role": "host", "name": "..."}, ...}')
    try:
        data = claude.ask_json(prompt, label="02")
    except Exception as exc:  # noqa: BLE001
        warn("02", f"角色判斷失敗（{exc}），使用預設值")
        data = {}

    for sid in ids:
        got = data.get(sid, {}) if isinstance(data, dict) else {}
        roles[sid]["role"] = got.get("role") or ("guest" if sid == ids[0] else "host")
        roles[sid]["name"] = manual.get(sid) or got.get("name") or ""
    log("02", "角色：" + "、".join(
        f"{k}={v['role']}{('/' + v['name']) if v['name'] else ''}" for k, v in roles.items()))
    return roles


def _write_readable(cfg, data) -> None:
    """輸出人看的逐字稿，方便你先掃一遍再進下一階段。"""
    roles = data["speakers"]
    lines = []
    for seg in data["segments"]:
        who = roles.get(seg.get("spk", ""), {})
        name = who.get("name") or who.get("role") or seg.get("spk", "")
        m, s = divmod(int(seg["s"]), 60)
        lines.append(f"[{m:02d}:{s:02d}] {name}: {seg['text']}")
    (cfg.build_file("02_transcript.txt")).write_text("\n".join(lines), encoding="utf-8")
