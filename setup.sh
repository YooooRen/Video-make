#!/usr/bin/env bash
# macOS 一鍵安裝
set -euo pipefail

echo "▶ 檢查 Homebrew / ffmpeg"
command -v brew >/dev/null || { echo "請先安裝 Homebrew: https://brew.sh"; exit 1; }
command -v ffmpeg >/dev/null || brew install ffmpeg

echo "▶ 檢查 Claude Code CLI"
if ! command -v claude >/dev/null; then
  echo "  安裝 Claude Code…"
  npm install -g @anthropic-ai/claude-code
  echo "  ⚠️  裝好後請先執行一次 `claude` 完成登入，再回來跑 pipeline"
fi

echo "▶ 建立 Python 虛擬環境"
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip

echo "▶ 安裝套件（cartopy 若失敗會跳過，不影響主流程）"
pip install pyyaml "faster-whisper>=1.0.3" "matplotlib>=3.8" "Pillow>=10.2"
pip install "pyannote.audio>=3.1.1" || echo "  ⚠️  pyannote 安裝失敗 → 不會自動分鏡，其餘功能正常"
pip install "cartopy>=0.23" || echo "  ⚠️  cartopy 安裝失敗 → 航線圖改用簡化底圖"

echo "▶ 建立設定檔"
[ -f project.yaml ] || cp project.example.yaml project.yaml

cat <<'MSG'

✅ 安裝完成。接下來：

  1. 編輯 project.yaml，填入訪談影片與素材資料夾的路徑
  2. （建議）到 https://hf.co/settings/tokens 申請 token，並同意
     pyannote/speaker-diarization-3.1 與 pyannote/segmentation-3.0 的授權，
     然後 export HF_TOKEN=hf_xxx   ← 沒有這個就不會自動分鏡
  3. source .venv/bin/activate && python run.py

MSG
