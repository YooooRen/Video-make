#!/usr/bin/env bash
# macOS 一鍵安裝
#
# 設計原則：可選元件失敗只警告，絕不中止整支腳本。
# 必要元件失敗會記錄下來，最後一次列出，並告訴你怎麼補。
set -uo pipefail

BLOCKERS=()
NOTES=()

say()   { printf '\n▶ %s\n' "$1"; }
ok()    { printf '  ✅ %s\n' "$1"; }
warn()  { printf '  ⚠️  %s\n' "$1"; NOTES+=("$1"); }
fail()  { printf '  ❌ %s\n' "$1"; BLOCKERS+=("$1"); }

# ---------------------------------------------------------------- Python ---
say "檢查 Python"
if command -v python3 >/dev/null; then
  ok "python3 $(python3 -V 2>&1 | cut -d' ' -f2)"
else
  fail "找不到 python3 → 執行 xcode-select --install，或 brew install python"
fi

# ---------------------------------------------------------------- ffmpeg ---
say "檢查 ffmpeg"
if command -v ffmpeg >/dev/null; then
  ok "ffmpeg 已安裝"
elif command -v brew >/dev/null; then
  echo "  安裝 ffmpeg…"
  if brew install ffmpeg; then
    ok "ffmpeg 安裝完成"
  else
    fail "ffmpeg 安裝失敗 → 手動執行 brew install ffmpeg"
  fi
else
  fail "找不到 ffmpeg，也沒有 Homebrew → 先裝 https://brew.sh 再 brew install ffmpeg"
fi

# ----------------------------------------------------------- Claude Code ---
# 這一段失敗不影響其他安裝，最後再提醒即可。
say "檢查 Claude Code CLI"
if command -v claude >/dev/null; then
  ok "claude 已安裝"
elif [ -x "$HOME/.local/bin/claude" ]; then
  warn "claude 裝在 ~/.local/bin 但不在 PATH → 執行：export PATH=\"\$HOME/.local/bin:\$PATH\""
else
  echo "  安裝 Claude Code（官方獨立安裝，不需要 Node.js）…"
  if curl -fsSL https://claude.ai/install.sh | bash; then
    if command -v claude >/dev/null || [ -x "$HOME/.local/bin/claude" ]; then
      ok "Claude Code 安裝完成"
      command -v claude >/dev/null || \
        warn '請把 ~/.local/bin 加進 PATH：export PATH="$HOME/.local/bin:$PATH"'
    else
      warn "安裝腳本跑完但找不到 claude，請重開終端機再確認"
    fi
  elif command -v npm >/dev/null; then
    echo "  獨立安裝失敗，改用 npm…"
    npm install -g @anthropic-ai/claude-code \
      && ok "Claude Code 安裝完成" \
      || warn "npm 安裝也失敗 → 見 https://docs.claude.com/en/docs/claude-code/setup"
  else
    warn "Claude Code 安裝失敗 → 手動安裝：curl -fsSL https://claude.ai/install.sh | bash"
  fi
fi
# 注意：訊息裡的 claude 不要用反引號包，雙引號內的反引號會被 shell 當成指令執行
NOTES+=('裝好 Claude Code 後，請先執行一次 claude 用你的訂閱帳號登入，然後離開')

# ------------------------------------------------------------ Python 環境 --
if command -v python3 >/dev/null; then
  say "建立 Python 虛擬環境"
  if [ -d .venv ]; then
    ok ".venv 已存在，沿用"
  elif python3 -m venv .venv; then
    ok ".venv 建立完成"
  else
    fail "虛擬環境建立失敗 → 確認 python3 -m venv 可用"
  fi

  if [ -f .venv/bin/activate ]; then
    # shellcheck disable=SC1091
    source .venv/bin/activate
    python -m pip install --quiet --upgrade pip

    say "安裝必要套件"
    if python -m pip install --quiet pyyaml "faster-whisper>=1.0.3" \
        "matplotlib>=3.8" "Pillow>=10.2"; then
      ok "pyyaml / faster-whisper / matplotlib / Pillow"
    else
      fail "必要套件安裝失敗 → 重跑 pip install -r requirements.txt 看詳細訊息"
    fi

    say "安裝可選套件（失敗不影響主流程）"
    python -m pip install --quiet "pyannote.audio>=3.1.1" \
      && ok "pyannote.audio（說話者分離）" \
      || warn "pyannote.audio 安裝失敗 → 不會自動分鏡，其餘功能正常"
    python -m pip install --quiet "cartopy>=0.23" \
      && ok "cartopy（航線圖海岸線）" \
      || warn "cartopy 安裝失敗 → 航線圖改用簡化底圖（可先 brew install geos proj 再試）"
  fi
fi

# ---------------------------------------------------------------- 設定檔 ---
say "設定檔"
if [ -f project.yaml ]; then
  ok "project.yaml 已存在，不覆蓋"
else
  cp project.example.yaml project.yaml && ok "已從範本建立 project.yaml"
fi

# ------------------------------------------------------------------ 總結 ---
echo
echo "══════════════════════════════════════════════════════════════"
if [ ${#BLOCKERS[@]} -gt 0 ]; then
  echo "  安裝未完成，以下 ${#BLOCKERS[@]} 項必須先解決："
  for b in "${BLOCKERS[@]}"; do echo "    ❌ $b"; done
else
  echo "  ✅ 安裝完成"
fi
if [ ${#NOTES[@]} -gt 0 ]; then
  echo
  echo "  提醒："
  for n in "${NOTES[@]}"; do echo "    ⚠️  $n"; done
fi
echo "══════════════════════════════════════════════════════════════"
echo
if [ ${#BLOCKERS[@]} -gt 0 ]; then
  echo "  解決上面的 ❌ 之後，重跑 ./setup.sh"
  echo
  exit 1
fi
echo "  接下來："
echo
echo "    source .venv/bin/activate     # 進虛擬環境後才有 python 這個指令"
echo "    python run.py --check         # 十秒體檢，會列出還缺什麼"
echo
exit 0
