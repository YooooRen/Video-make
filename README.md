# 訪談影片 AI 自動處理 pipeline

把一支**英文訪談原始檔**變成剪好的 Final Cut Pro 時間軸，外加 YouTube 用的
中英雙語字幕檔，以及提到航線與專有名詞時的說明動畫。

```
訪談 .mov ─► pipeline ─► 06_timeline.fcpxml     ← 匯入 Final Cut Pro
                         subtitles_zh-Hant.srt  ← YouTube 中文 CC
                         subtitles_en.srt       ← YouTube 英文 CC
                         explainers/*.mov       ← 航線圖／名詞卡（含透明背景）
```

AI 判斷的部分全部走 **Claude Code 無介面模式**（`claude -p`），吃你現有的
Claude 訂閱額度，不需要另外儲值 API credits。

---

## 它會幫你做三件事

| # | 階段 | 做的事 |
|---|------|--------|
| 01 | `ingest` | 檢查影片、決定時間軸格式、讀出嵌入時間碼、抽音軌 |
| 02 | `transcribe` | faster-whisper 逐字轉錄（**詞級**時間戳）+ pyannote 分辨說話者 |
| 03 | `clean` | **① 剪掉過久停頓與結巴**：um / uh、口吃重複、講到一半改口 |
| 04 | `subtitles` | **② 中英雙語字幕檔**：在剪完的時間軸上生成，含術語統一譯法 |
| 05 | `explainers` | **③ 說明動畫**：提到航線 → 地圖動畫；提到術語 → 說明小卡 |
| 06 | `fcpxml` | 把剪輯決策與說明動畫組成一份 FCPXML |

### 產出的時間軸長這樣

```
lane  1   ▁▁▁▁[航線圖]▁▁▁▁▁▁▁▁▁▁▁▁[名詞卡]▁▁▁▁▁   說明動畫（含 alpha）
spine     ██│███│████│██│█████│███│███│██████████   訪談本體，每一刀都是剪掉的停頓或結巴
lane -1   ▔▔▔▔▔▔▔▔▔ 字幕（選用，預設只放中文）▔▔▔▔▔
```

---

## 安裝

```bash
git clone https://github.com/YooooRen/Video-make.git
cd Video-make
./setup.sh
```

`setup.sh` 會裝 ffmpeg、Claude Code CLI、Python 套件，並從 `project.example.yaml`
複製一份 `project.yaml` 給你。它不進版控，所以你怎麼改都不會跟 `git pull` 衝突。

### 還需要兩件事

**1. Claude Code 要先登入過一次**

```bash
claude
```

跑一次、用你的 Claude 訂閱帳號登入，然後離開。腳本用的是官方獨立安裝方式
（`curl -fsSL https://claude.ai/install.sh | bash`），不需要 Node.js。裝完如果
`claude` 找不到，多半是 `~/.local/bin` 不在 PATH：

```bash
export PATH="$HOME/.local/bin:$PATH"
```

**2. HuggingFace token（說話者分離需要）**

1. 到 https://hf.co/settings/tokens 建一個 read token
2. 到這兩個頁面各按一次同意：
   - https://hf.co/pyannote/speaker-diarization-3.1
   - https://hf.co/pyannote/segmentation-3.0
3. `export HF_TOKEN=hf_xxxxx`（建議寫進 `~/.zshrc`）

沒有 token 也能跑，只是逐字稿不會標出誰在講話。

---

## 使用

編輯 `project.yaml`，最少只要填一行：

```yaml
project:
  source_video: "~/Movies/captain-interview.mov"
```

**開跑前先做十秒體檢**：

```bash
source .venv/bin/activate
python run.py --check
```

它會一次告訴你所有會讓你等半小時才失敗的問題 —— 少裝的套件、找不到的檔案、
沒設的 token、沒有的中文字型、讀不到的時間碼。全綠之後：

```bash
python run.py
```

一小時的訪談大約要 30–50 分鐘，最慢的是轉錄與說話者分離。

### 分段執行

每個階段的產出都存成 JSON，隨時可以從中間接著跑：

```bash
python run.py --check          # 開跑前體檢
python run.py --probe          # 產生最小 FCPXML，排查 FCP 匯入問題
python run.py --list           # 看有哪些階段
python run.py --from 04        # 從字幕開始重跑
python run.py --only 05        # 只重做說明動畫
python run.py --only 06        # 改完設定，只重組 FCPXML（幾秒鐘）
```

Claude 的回覆會快取在 `build/.claude_cache/`，重跑不會重複燒額度。
想強制重問：`python run.py --only 04 --no-cache`。

---

## 建議的實際工作流程

一次跑到底的結果通常有 85 分，最後 15 分靠這兩個檢查點：

### 檢查點 1：轉錄完成後

```bash
python run.py --to 02
open build/02_transcript.txt
```

說話者代號認錯的話，在 `project.yaml` 寫死：

```yaml
speakers:
  map:
    SPEAKER_00: "Captain Lee"
    SPEAKER_01: "主持人"
```

### 檢查點 2：剪輯決策出來後

```bash
python run.py --only 03
open build/03_cuts.txt
```

這份報表列出**每一句被剪掉的話**。覺得剪太兇就調鬆：

```yaml
cleanup:
  remove_false_starts: false   # 保留改口
  max_pause: 1.0               # 只砍超過 1 秒的停頓
```

然後 `python run.py --from 03` 繼續。

### 最後：進 Final Cut Pro

1. **檔案 → 匯入 → XML…** 選 `build/06_timeline.fcpxml`
2. FCP 會建立事件與專案，時間軸已經剪好、說明動畫也疊在 lane 1
3. 剩下的手工活：調色、配樂、B-roll、微調幾個你不同意的剪點

> FCPXML 只是「剪輯指令」，不含影像資料。**匯入前不要搬動原始影片**，
> 也不要清掉 `build/explainers/`，否則 FCP 會找不到檔案。

### 上傳 YouTube

字幕 → 上傳檔案（含時間碼）→ 分別傳 `subtitles_zh-Hant.srt` 與
`subtitles_en.srt`，觀眾就能在播放器裡切換中／英隱藏式字幕。

**不要把字幕燒進影片**。燒進去的字幕沒辦法關掉、不能被搜尋、
也吃不到 YouTube 的自動翻譯。

---

## 調整輸出

### 某個航海名詞翻得不好

編輯 `build/04_glossary.txt`。它是翻譯用的術語對照表，會被塞進**每一批**
翻譯的提示詞，確保同一個詞在整支影片裡翻得一致。

```
beam reach	橫風航行	風從船側吹來的航向
jury rig	應急帆裝	桅杆斷了之後臨時搭的帆裝
```

每行三欄、用 Tab 分隔（連續空白也讀得進來），`#` 開頭是註解。

**你的編輯優先**：重跑 stage 04 時，檔案裡已有的詞不會被 AI 覆蓋，它只會在
檔尾補上還沒收錄到的新詞。不要某個詞就刪掉那一行；想完全重新產生就刪掉整個檔案。

想把它放到 `build/` 外面（避免清理時被刪）：

```yaml
subtitles:
  glossary_file: "glossary.txt"
```

### 人名、船名被聽錯

在 `project.yaml` 加一張修正表。它會套用到英文字幕與中文翻譯，並且寫進翻譯的
提示詞，讓 AI 一開始就用正確拼法：

```yaml
corrections:
  Issa: "Isa"
  荷巴特: "霍巴特"
```

英數詞比對不分大小寫、且認單字邊界 —— `Issa` 不會誤中 `Issabella`。

### 字幕換行太頻繁

一張字幕能裝多少字由 `max_chars_en × max_lines` 決定 —— 英文的字數預算同時
決定了中英兩邊的斷句位置，因為中文是逐句翻譯過來的：

```yaml
subtitles:
  max_chars_en: 55        # 每張字卡的容量
  max_chars_zh: 26        # 每行中文字數上限
  max_duration: 8.0       # 字卡停留更久、數量更少
```

想減少換行的話，先調 `max_duration` 通常比拉長行寬更好讀。

### FCP 裡的字幕

預設只放中文一條（`fcp_captions: "zh"`）。設成 `"both"` 會中英同時疊在畫面上 ——
那是 FCP 的顯示行為，可在**時間軸索引**（`Shift+Cmd+2`）→「角色」分頁取消勾選。
設成 `"none"` 則 FCP 裡完全不放字幕，YouTube 的 CC 不受影響。

### 說明動畫

```yaml
explainers:
  max_count: 12       # 全片最多幾段
  duration: 6.0       # 每段幾秒
  min_gap: 15.0       # 兩段之間至少間隔
  route_style: "dark" # dark | light
```

`build/05_explainers.json` 會列出 AI 選了哪些航線與名詞。不想要某一段就刪掉
`build/explainers/` 裡對應的 .mov，再重跑 stage 06。

### 想改 AI 的判斷標準

所有提示詞都是 `prompts/` 底下的 markdown，直接改就生效，不用動程式碼。
例如覺得結巴刪得不夠乾淨，就去 `prompts/clean_transcript.md` 加規則。

---

## 常見狀況

**FCP 說「沒有個別媒體，剪輯無效」**

嵌入時間碼沒讀到。相機檔常帶拍攝當下的時間碼（例如 `21:43:27;18`），FCP 認為
媒體存在於時間軸的 21 小時 43 分處；素材的 `start` 寫成 `0s` 就會指向一個沒有
媒體的位置。先跑 `python run.py --check` 看「嵌入時間碼」那一行，讀不到就手動指定：

```yaml
project:
  source_timecode: "21:43:27;18"
```

查法：把素材匯入 FCP，看瀏覽器裡顯示的起始時間碼。

**FCP 說「項目不在剪輯影格的界限內」**

時間軸上的 `offset` 與 `duration` 必須是序列影格的整數倍。stage 06 會在寫檔前
自己檢查並指出是哪個項目差多少格 —— 看終端機的警告。

**FCP 匯入失敗，想知道問題出在哪一層**

```bash
python run.py --probe
```

產生 `build/probe_minimal.fcpxml` —— 只有一段主畫面，沒有字幕也沒有說明動畫。
匯入成功代表媒體沒問題、是時間軸上加的東西有問題；匯入失敗則是媒體檔本身
（路徑、編碼、VFR）的問題。

**說話者分離失敗：`hf_hub_download() got an unexpected keyword argument 'use_auth_token'`**

套件版本打架。`huggingface_hub` 0.26 之後移除了 `use_auth_token` 參數（改名為
`token`），而 3.3 以前的 `pyannote.audio` 內部還在傳舊名字。

```bash
pip install -U "pyannote.audio>=3.3.2"
python run.py --only 02
```

升級後仍不行就走另一條：`pip install "huggingface_hub<0.26"`。

程式本身會依安裝的版本自動挑正確的參數名，都不合時則不傳 token、
讓 huggingface_hub 自己讀 `HF_TOKEN` 環境變數。

**說明動畫的中文變成豆腐字 □□□**

```yaml
explainers:
  font_path: "/System/Library/Fonts/PingFang.ttc"
```

**航線圖沒有海岸線**

cartopy 沒裝成功，自動退回簡化底圖。要真實海岸線：

```bash
brew install geos proj && pip install cartopy
python run.py --only 05
```

**轉錄太慢**

`transcribe.model` 從 `large-v3` 換成 `medium`，速度快 2–3 倍，英文訪談的
準確度差距不大。

**`zsh: command not found: python`**

macOS 只有 `python3`。虛擬環境啟動後才會有 `python`：

```bash
source .venv/bin/activate
```

---

## 開發

```bash
python tests/smoke_test.py          # 端到端煙霧測試，不需要 Claude／GPU
python tests/smoke_test.py --keep   # 保留產出以便檢查
python tests/test_claude_cli.py     # claude CLI 呼叫方式的回歸測試
```

煙霧測試會用 ffmpeg 合成帶 drop-frame 時間碼的假素材、餵假的 Claude 回覆，
跑完 stage 03–06，驗證時間軸數學、字幕不重疊、詞彙表的編輯優先規則、
說明動畫算圖、FCPXML 的屬性合法性／媒體範圍／影格對齊。

### 專案結構

```
run.py                  總控制台
project.example.yaml    設定範本
prompts/                所有 AI 提示詞（改這裡就能調 AI 行為）
pipeline/
  config.py             設定載入與預設值
  claude_client.py      claude -p 無介面呼叫 + 快取 + API fallback
  timeline.py           分數時間、剪輯映射、SRT 輸出  ← 最核心的數學
  render.py             航線地圖動畫、名詞卡繪製
  preflight.py          開跑前體檢
  s01…s06_*.py          六個階段
tests/
  smoke_test.py         端到端測試
  test_claude_cli.py    CLI 呼叫方式回歸測試
```

### 時間軸的兩套座標

整個 pipeline 最容易搞混的地方：

- **原始時間**：在來源影片裡的秒數
- **成品時間**：剪掉停頓之後，在時間軸上的秒數

`timeline.EditMap` 負責兩者互換。字幕與說明動畫全部用**成品時間**思考。

FCPXML 還有第三層要注意：`start` 屬於**素材自己的時間軸**（要加上嵌入時間碼、
用素材的幀率），而 `offset` 與 `duration` 屬於**成品時間軸**（要用序列的幀率）。
用錯會讓 Final Cut Pro 拒收 —— `s06_fcpxml.py` 在寫檔前會自己驗證這三件事。
