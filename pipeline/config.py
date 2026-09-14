"""專案設定載入與預設值。"""
from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any

from .util import StageError

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

DEFAULTS: dict[str, Any] = {
    "project": {
        "name": "Sailing Interview",
        "event": "Sailing Interview",
        "source_video": "",          # 必填：訪談原始影片
        "build_dir": "build",
        "language": "en",            # 原始語音語言
        "target_language": "zh-Hant",
        "source_timecode": "",       # ffprobe 讀不到嵌入時間碼時手動指定，例如 "21:43:27;18"
    },
    "sequence": {
        # 留空 = 沿用原始影片的解析度／幀率
        "width": 0,
        "height": 0,
        "fps": "",                   # 例如 "30000/1001"、"25"、"" = 自動
        "audio_rate": 48000,
    },
    "speakers": {
        # 把 diarization 的 SPEAKER_00/01 對到真人；留空則由 AI 依內容推測
        "map": {},                   # {"SPEAKER_00": "Captain Lee"}
        "host_hint": "問問題的人是主持人",
    },
    "transcribe": {
        "model": "large-v3",         # faster-whisper 模型
        "device": "auto",            # auto | cpu | cuda
        "compute_type": "auto",      # auto | int8 | float16 | float32
        "beam_size": 5,
        "vad_filter": True,
        "diarize": True,
        "num_speakers": 2,           # 已知人數可加速並提高準確度；0 = 自動
        "hf_token_env": "HF_TOKEN",  # pyannote 需要 HuggingFace token
    },
    "cleanup": {
        "enabled": True,
        "remove_fillers": True,
        "remove_repetitions": True,
        "remove_false_starts": True,
        "remove_tangents": False,    # 較激進：刪掉離題段落，預設關閉
        "max_pause": 0.60,           # 超過這個秒數的靜默視為過久停頓
        "keep_pause": 0.22,          # 過久停頓保留多少（保留呼吸感）
        "cut_pad": 0.06,             # 每個刪除區間左右各縮回多少，避免切到字頭字尾
        "min_removal": 0.10,         # 小於此長度的刪除直接略過（不值得切一刀）
        "min_keep": 0.30,            # 小於此長度的保留片段併入鄰居
        "chunk_segments": 40,        # 每次送給 Claude 的段落數
    },
    "subtitles": {
        "max_chars_en": 42,
        "max_chars_zh": 18,
        "max_lines": 2,
        "min_duration": 1.0,
        "max_duration": 6.0,
        "gap_break": 0.55,           # 字與字間隔超過此值就換一張字卡
        "lead_in": 0.06,             # 字幕提早出現
        "tail_out": 0.20,            # 字幕延後消失（不超過下一張）
        "translate_batch": 25,
        # 詞彙表檔案。留空 = build/04_glossary.txt。
        # 這個檔案可以編輯，你寫的譯法優先，AI 只會補上沒收錄的新詞。
        "glossary_file": "",
        "burn_in": False,            # True = 另外輸出燒錄字幕影片（YouTube CC 不需要）
        # FCPXML 裡要放哪幾條字幕軌：both（中英各一）/ zh / en / none。
        # 這只影響 FCP 裡的預覽；YouTube 的隱藏式字幕一律用輸出的 .srt 檔。
        "fcp_captions": "zh",
    },
    "explainers": {
        "enabled": True,
        "max_count": 12,
        "duration": 6.0,
        "min_gap": 15.0,             # 兩段說明之間至少間隔幾秒
        "fade": 0.5,
        "width": 1920,
        "height": 1080,
        "fps": 30,
        "route_style": "dark",       # dark | light
        "font_candidates": [
            "/System/Library/Fonts/PingFang.ttc",
            "/System/Library/Fonts/STHeiti Medium.ttc",
            "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
            "/Library/Fonts/Arial Unicode.ttf",
        ],
        "font_path": "",             # 手動指定則優先
    },
    # 字詞修正：訂正語音辨識聽錯的人名／船名／專有名詞，{聽錯的: 正確的}
    # 會同時套用到英文字幕、中文翻譯與說明欄
    "corrections": {},
    # 個別素材的時間碼覆寫：{檔名: "HH:MM:SS;FF"}
    "media_timecodes": {},
    "claude": {
        "backend": "cli",            # cli | api | auto
        "cli_binary": "claude",
        "model": "",                 # 留空 = 用 CLI/API 預設
        "timeout": 900,
        "max_retries": 3,
        "api_key_env": "ANTHROPIC_API_KEY",
        "cache_dir": "build/.claude_cache",
        "use_cache": True,
    },
}


def _deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


class Config:
    def __init__(self, data: dict, root: Path):
        self.data = data
        self.root = root

    # -- 存取 ---------------------------------------------------------------
    def __getitem__(self, key: str) -> Any:
        return self.data[key]

    def get(self, path: str, default: Any = None) -> Any:
        """以 'broll.min_gap' 這種點路徑取值。"""
        cur: Any = self.data
        for part in path.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur

    # -- 路徑 ---------------------------------------------------------------
    def path(self, value: str | Path) -> Path:
        p = Path(os.path.expanduser(str(value)))
        return p if p.is_absolute() else (self.root / p)

    @property
    def build(self) -> Path:
        p = self.path(self.get("project.build_dir", "build"))
        p.mkdir(parents=True, exist_ok=True)
        return p

    def build_file(self, name: str) -> Path:
        return self.build / name

    @property
    def source_video(self) -> Path:
        raw = self.get("project.source_video", "")
        if not raw:
            raise StageError("project.source_video 沒有設定，請在 project.yaml 填入訪談影片路徑。")
        p = self.path(raw)
        if not p.exists():
            raise StageError(f"找不到訪談影片：{p}")
        return p


def load_config(path: str | Path) -> Config:
    p = Path(os.path.expanduser(str(path))).resolve()
    if not p.exists():
        raise StageError(f"找不到設定檔 {p}。可從 project.example.yaml 複製一份。")
    if yaml is None:
        raise StageError("缺少 pyyaml，請執行 `pip install -r requirements.txt`。")
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return Config(_deep_merge(DEFAULTS, raw), p.parent)
