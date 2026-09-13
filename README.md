# 帆船訪談影片 AI 自動剪輯 pipeline

把一支**英文訪談原始檔** + **一資料夾帆船素材**，自動變成一條剪好的
Final Cut Pro 時間軸，外加 YouTube 上架需要的所有東西。

```
訪談 .mov ─┐
           ├─► pipeline ─► 08_timeline.fcpxml   ← 匯入 Final Cut Pro
素材資料夾 ─┘              subtitles_zh-Hant.srt ← YouTube 中文 CC
                          subtitles_en.srt      ← YouTube 英文 CC
                          09_description.md     ← 標題／說明欄／章節／人名
                          10_thumbnail.png      ← 封面圖
```

AI 判斷的部分全部走 **Claude Code 無介面模式**（`claude -p`），吃你現有的
Claude 訂閱額度，不需要另外儲值 API credits。

---

## 它會幫你做什麼

| # | 階段 | 做的事 |
|---|------|--------|
| 01 | `ingest` | 檢查影片、決定時間軸格式、抽出音軌 |
| 02 | `transcribe` | faster-whisper 逐字轉錄（**詞級**時間戳）+ pyannote 分辨兩個人 |
| 03 | `clean` | 找出 um / uh / 口吃 / 講到一半改口 / 過久停頓，算出每一刀切在哪 |
| 04 | `subtitles` | 在**剪完的**時間軸上生成中英雙語字幕，含航海名詞統一譯法 |
| 05 | `broll` | AI 看過素材資料夾每支影片的內容，再依講話內容決定墊在哪 |
| 06 | `explainers` | 提到航線 → 生成航線地圖動畫；提到術語 → 生成說明小卡 |
| 07 | `framing` | 判斷兩人各坐在畫面哪一邊，依誰在講話推近特寫 |
| 08 | `fcpxml` | 把以上全部組成一份 FCPXML |
| 09 | `package` | 標題、說明欄、章節、**提到的人名**、標籤 |
| 10 | `thumbnail` | 挑最好的一格畫面合成封面圖 |

### 產出的時間軸長這樣

```
lane  2   ▁▁▁▁[航線圖]▁▁▁▁▁▁▁▁▁▁▁▁[名詞卡]▁▁▁▁▁     說明短片（含透明背景）
lane  1   ▁▁[B-roll]▁▁▁▁▁▁▁[B-roll]▁▁▁▁▁▁▁▁▁▁▁     帆船素材（只用畫面）
spine     ██│███│████│██│█████│███│███│██████████   訪談本體，每一刀都是剪掉的贅詞
lane -1   ▔▔▔▔▔▔▔▔ 中文字幕 ▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔
lane -2   ▔▔▔▔▔▔▔▔ 英文字幕 ▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔
           ↑ spine 上帶關鍵影格，依說話者推近／拉遠
```

---

## 安裝

```bash
git clone https://github.com/YooooRen/Video-make.git
cd Video-make
./setup.sh
```

`setup.sh` 會裝 ffmpeg、Claude Code CLI、Python 套件，並複製一份 `project.yaml`。
可選元件（pyannote、cartopy、Claude Code）安裝失敗只會警告，不會中斷其他步驟；
必要元件缺了會在最後一次列出，並附上補救指令。

### 還需要兩件事

**1. Claude Code 要先登入過一次**

```bash
claude          # 跑一次，用你的 Claude 訂閱帳號登入，然後離開
```

腳本用的是官方獨立安裝方式（`curl -fsSL https://claude.ai/install.sh | bash`），
不需要 Node.js。裝完如果 `claude` 找不到，多半是 `~/.local/bin` 不在 PATH：

```bash
export PATH="$HOME/.local/bin:$PATH"     # 建議寫進 ~/.zshrc
```

**2. HuggingFace token（自動分鏡需要）**

說話者分離（誰在講話）靠 pyannote，它要求你同意模型授權：

1. 到 https://hf.co/settings/tokens 建一個 read token
2. 到這兩個頁面各按一次同意：
   - https://hf.co/pyannote/speaker-diarization-3.1
   - https://hf.co/pyannote/segmentation-3.0
3. `export HF_TOKEN=hf_xxxxx`（建議寫進 `~/.zshrc`）

沒有 token 也能跑，只是不會自動分鏡（第 07 階段會跳過，其他功能正常）。

---

## 使用

`setup.sh` 會從 `project.example.yaml` 複製一份 `project.yaml` 給你。
它不進版控，所以你怎麼改都不會跟 `git pull` 衝突。

`project.yaml` 已經填好這支影片的路徑。**開跑前先做十秒體檢**：

```bash
source .venv/bin/activate
python run.py --check
```

它會一次告訴你所有會讓你等半小時才失敗的問題 —— 少裝的套件、找不到的檔案、
沒設的 token、沒有的中文字型。全綠（或只剩你能接受的 ⚠️）之後：

```bash
python run.py
```

跑完 `build/` 就有全部東西。**一小時的訪談大約要 30–60 分鐘**，
其中最慢的是轉錄與說話者分離。

### 分段執行

每個階段的產出都存成 JSON，隨時可以從中間接著跑：

```bash
python run.py --check          # 開跑前體檢
python run.py --probe          # 產生最小 FCPXML，排查 FCP 匯入問題
python run.py --list           # 看有哪些階段
python run.py --from 04        # 從字幕開始重跑
python run.py --only 05 06     # 只重做 B-roll 與說明短片
python run.py --only 08        # 改完設定，只重組 FCPXML（很快）
```

Claude 的回覆會快取在 `build/.claude_cache/`，重跑不會重複燒額度。
想強制重新問：`python run.py --only 03 --no-cache`。

---

## 建議的實際工作流程

一次跑到底的結果通常有 85 分，最後 15 分靠這兩個檢查點：

### 檢查點 1：轉錄完成後（跑完 02）

```bash
python run.py --to 02
open build/02_transcript.txt
```

看一下說話者代號對不對。如果 AI 認錯了，在 `project.yaml` 寫死：

```yaml
speakers:
  map:
    SPEAKER_00: "Captain Lee"
    SPEAKER_01: "主持人"
```

### 檢查點 2：剪輯決策出來後（跑完 03）

```bash
python run.py --only 03
open build/03_cuts.txt
```

這份報表列出**每一句被剪掉的話**。掃一眼，如果覺得剪太兇，調鬆一點：

```yaml
cleanup:
  remove_false_starts: false   # 保留改口
  max_pause: 1.0               # 只砍超過 1 秒的停頓
```

然後 `python run.py --from 03` 繼續。

### 最後：進 Final Cut Pro

1. **檔案 → 匯入 → XML…** 選 `build/08_timeline.fcpxml`
2. FCP 會建立一個事件和一個專案，時間軸已經剪好
3. 剩下的手工活：調色、配樂、微調幾個你不同意的剪點

> FCPXML 只是「剪輯指令」，不含影像資料。**匯入前不要搬動原始影片與素材的位置**，
> 否則 FCP 會找不到檔案（真的搬了，用 FCP 的「重新連結檔案」指回去即可）。

### 上傳 YouTube

- 字幕 → 上傳檔案（含時間碼）→ 分別傳 `subtitles_zh-Hant.srt` 與 `subtitles_en.srt`
  → 觀眾就能在播放器裡切換中／英隱藏式字幕
- 標題、說明欄、章節、標籤 → 從 `build/09_description.md` 複製
- 封面 → `build/10_thumbnail.png`

**字幕請用 YouTube 的 CC 功能，不要燒進影片**。燒進去的字幕沒辦法關掉、
不能被搜尋、也吃不到自動翻譯。

---

## 常見狀況

**分鏡沒有作用／畫面完全沒有推近**

先看 `build/07_framing.json` 的 `positions`。空的代表 AI 判斷不出兩人的位置
（常見於兩人靠很近、或是單機正面拍攝）。直接手動指定就好：

```yaml
framing:
  speaker_positions:
    SPEAKER_00: left
    SPEAKER_01: right
```

**推近的幅度不喜歡**

```yaml
framing:
  punch_scale: 1.25        # 小一點
  transition_frames: 12    # 從硬切改成慢慢推近
```

**stage 05 報 `Input must be provided either through stdin or as a prompt argument`**

已修掉，`git pull` 即可。成因是 Claude Code 的 `--add-dir` 接受多個值，會把接在
後面的 prompt 當成「又一個目錄」吃掉；現在 prompt 一律走 stdin。同時，素材辨識
失敗的結果不再寫入快取，重跑會自動重試（原本失敗會被快取成永久狀態）。

**Final Cut Pro 匯入時說「DTD 驗證失敗 / No declaration for attribute role of element asset-clip」**

已修掉，`git pull` 後 `python run.py --only 08` 重新產生即可。成因是 FCPXML 的
`asset-clip` 沒有 `role` 屬性（它有 `audioRole` 和 `videoRole`），只有 `<video>`、
`<audio>`、`<caption>` 才用 `role`。現在寫檔前會先自我檢查所有屬性名稱，
不合 DTD 就直接中止並指出是哪個元素的哪個屬性，不會等到 FCP 才發現。

**FCP 匯入失敗，想知道問題出在哪一層**

```bash
python run.py --probe
```

產生 `build/probe_minimal.fcpxml` —— 只有一段主畫面，沒有字幕、B-roll、
說明短片、鏡位關鍵影格。這是能成立的最小結構，用來一刀切開兩種可能：

- 探針**匯入成功** → 媒體本身沒問題，是時間軸上加的東西有問題
- 探針**匯入失敗** → 問題在媒體檔（路徑不對、編碼 FCP 不吃、VFR 變動幀率）

**FCP 說「沒有個別媒體，剪輯無效」**

已修掉，`git pull` 後 `python run.py --only 08` 重新產生即可。

成因是**嵌入式時間碼**。相機拍出來的檔案常帶「拍攝當下的時間」，例如
`21:43:27;18`。Final Cut Pro 認為這段媒體存在於它自己時間軸的 21 小時 43 分處
（78207 秒），而原本的程式一律把素材的 `start` 寫成 `0s`，clip 就指向一個
沒有媒體的位置 —— 錯誤訊息是字面意思。

現在會用 ffprobe 讀出時間碼，換算成秒（含 drop-frame 的跳號規則）寫進素材的
`start`，所有 clip 的 `start` 也跟著位移，`tcFormat` 標成 `DF` 或 `NDF`。
訪談影片與 B-roll 素材都適用。

> 排錯小技巧：把素材匯入 FCP、拖到時間軸，再「檔案 → 輸出 XML」，
> 就得到一份 FCP 自己寫的標準答案，拿來跟 `build/08_timeline.fcpxml`
> 逐屬性比對，比猜快得多。

**FCP 還是說「沒有個別媒體」，但時間碼已經修過了**

先跑 `python run.py --check`，看「嵌入時間碼」那一行：

- 顯示時間碼與素材起點 → 偵測正常，問題在別處
- 顯示「ffprobe 讀不到」→ 手動指定。把素材匯入 FCP，看瀏覽器裡的起始
  時間碼，填進 `project.yaml`：

```yaml
project:
  source_timecode: "21:43:27;18"     # 分號代表 drop frame
media_timecodes:
  DJI_xxxx.MP4: "18:02:11;04"        # B-roll 素材各自指定
```

另外 stage 08 現在會在寫檔前檢查每個 clip 取用的範圍是否落在素材的媒體範圍內，
越界會直接警告並指出是哪個檔案 —— 不必等 FCP 才發現。

**FCP 說「項目不在剪輯影格的界限內」**

已修掉。`duration` 與 `offset` 屬於**時間軸**座標系，必須對齊序列的影格網格；
只有 `start` 屬於素材自己的時間軸，才用素材的幀率。說明短片用 30fps 算圖、
時間軸是 29.97fps 時，6.000 秒 = 179.82 格，不是整數格，FCP 就會拒收。

現在寫檔前會檢查每個 offset / duration 是不是序列影格的整數倍，不合就警告
並指出是哪個項目差多少格。

**FCP 裡中英字幕疊在一起同時出現**

這是 FCP 的顯示設定，不是錯誤 —— 兩條字幕軌都被啟用時會一起畫上去。
關掉其中一條：**時間軸索引**（`Shift+Cmd+2`）→「角色」分頁 → 取消勾選該字幕角色。
選單列的 **檢視 → 字幕** 也可以整個關掉或切換語言。

不想在 FCP 裡放兩條的話：

```yaml
subtitles:
  fcp_captions: "zh"     # both / zh / en / none
```

YouTube 的隱藏式字幕不受影響 —— 那是上傳 `build/subtitles_zh-Hant.srt` 與
`subtitles_en.srt` 兩個檔案，跟 FCP 裡放幾條無關。

**素材路徑變動後 FCP 找不到檔案**

FCPXML 只是剪輯指令，不含影像資料。匯入前不要搬動原始影片與素材；真的搬了，
用 FCP 的「重新連結檔案」指回去即可。說明短片放在 `build/explainers/`，
清理 `build/` 會讓它們斷連。

**素材資料夾裡有很多無關的檔案**

`broll.max_index` 限制最多辨識幾份素材（預設 60），避免整個 temp 資料夾都送去
辨識、白燒額度。素材確定都是帆船畫面時再調高。想更精準就把要用的素材另外
複製到一個乾淨的資料夾，再把 `media_dir` 指過去。

**B-roll 墊得太密／墊錯地方**

`build/05_broll.json` 每一段都有 `reason` 說明為什麼選它。要調整：

```yaml
broll:
  min_gap: 20.0     # 兩段之間隔久一點
  max_count: 15     # 全片最多 15 段
```

素材辨識結果快取在 `build/05_broll_index.json`，改設定重跑不會重新辨識。

**航線圖沒有海岸線**

代表 cartopy 沒裝成功，自動退回簡化底圖。要真實海岸線：

```bash
brew install geos proj
pip install cartopy
python run.py --only 06
```

**說明短片的中文變成豆腐字 □□□**

找不到中文字型，手動指定：

```yaml
explainers:
  font_path: "/System/Library/Fonts/PingFang.ttc"
```

**`zsh: command not found: python`**

macOS 只有 `python3`，沒有 `python`。虛擬環境啟動後才會有 `python` 這個名字：

```bash
source .venv/bin/activate      # 先做這一步
python run.py --check
```

**轉錄太慢**

`transcribe.model` 從 `large-v3` 換成 `medium`，速度快 2–3 倍，
英文訪談的準確度差距不大。

**某個航海名詞翻得不好，想自己訂譯法**

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

人名與船名不收錄在這裡 —— 那些保持英文原樣，要訂正請用下面的 `corrections`。

**人名、船名被聽錯（例如 Issa 應該是 Isa）**

在 `project.yaml` 加一張修正表。它會同時套用到英文字幕、中文翻譯與說明欄，
並且會寫進翻譯的提示詞裡，讓 AI 一開始就用正確拼法：

```yaml
corrections:
  Issa: "Isa"
  RA Vig: "Ragnar Vig"
  荷巴特: "霍巴特"
```

英數詞比對時不分大小寫、且認單字邊界 —— `Issa` 不會誤中 `Issabella`。
改完 `python run.py --only 04 09` 重跑字幕與說明欄即可。

**字幕換行太頻繁／每張字卡想放更多字**

一張字幕能裝多少字由 `max_chars_en × max_lines` 決定（英文的字數預算同時
決定了中英兩邊的斷句位置，因為中文是逐句翻譯過來的）：

```yaml
subtitles:
  max_chars_en: 55        # 每行英文字元上限，同時放寬每張字卡的容量
  max_chars_zh: 26        # 每行中文字數上限，調大就少換行
  max_lines: 2
  max_duration: 8.0       # 一張字卡最長停留幾秒，調大則字卡更少更長
```

改完 `python run.py --only 04` 重跑即可（會重新翻譯，約數分鐘）。

**想改 AI 的判斷標準**

所有提示詞都是 `prompts/` 底下的 markdown，直接改就生效，不用動程式碼。
例如覺得贅詞刪得不夠乾淨，就去 `prompts/clean_transcript.md` 加規則。

---

## 開發

```bash
python tests/smoke_test.py          # 端到端煙霧測試，不需要 Claude／GPU
python tests/smoke_test.py --keep   # 保留產出以便檢查
python tests/test_claude_cli.py     # claude CLI 呼叫方式的回歸測試
```

測試會用 ffmpeg 合成假素材、餵假的 Claude 回覆，跑完 stage 03–10，
驗證時間軸數學、字幕不重疊、FCPXML 結構、各階段資料交接都正確。

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
  s01…s10_*.py          十個階段
tests/
  smoke_test.py         端到端測試
  test_claude_cli.py    CLI 呼叫方式回歸測試
```

### 時間軸的兩套座標

整個 pipeline 最容易搞混的地方，只有一個概念要記住：

- **原始時間**：在 `interview.mov` 裡的秒數
- **成品時間**：剪掉贅詞之後，在時間軸上的秒數

`timeline.EditMap` 負責兩者互換。字幕、B-roll、說明短片、分鏡全部
用**成品時間**思考；只有 FCPXML 的 `start` 屬性用原始時間。

FCPXML 的所有時間都必須是 `frameDuration` 的整數倍（例如 29.97fps 是
`1001/30000s`），不是的話 Final Cut Pro 會直接拒絕匯入 —— `Rate.time()`
就是在處理這件事。
