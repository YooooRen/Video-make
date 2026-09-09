這張圖是一支影片素材（或一張照片）的**縮圖拼貼**，格子由左到右、由上到下代表影片從頭到尾的幾個時間點。

請看圖並描述這份素材的內容，讓後續的剪輯 AI 能判斷它適合墊在訪談的哪一段。

檔名：{{NAME}}
類型：{{KIND}}
長度：{{DURATION}}

只輸出 JSON：

```
{
  "summary": "一句話描述畫面內容（繁體中文，30字以內）",
  "tags": ["帆船", "航行中", "海上", "船員操作"],
  "subjects": ["帆船", "人物", "海面"],
  "shot_type": "wide",
  "motion": "medium",
  "time_of_day": "day",
  "usable": true,
  "quality_note": "畫面晃動嚴重" 
}
```

欄位說明：
- `tags`：3–8 個繁體中文關鍵字，描述主題、動作、場景
- `shot_type`：wide（遠景）/ medium（中景）/ close（特寫）/ detail（細節）/ aerial（空拍）
- `motion`：static（幾乎不動）/ slow / medium / fast（劇烈晃動）
- `time_of_day`：day / golden / night / unknown
- `usable`：畫面是否堪用（嚴重失焦、全黑、手震到無法看則為 false）
- `quality_note`：有問題才寫，沒問題填空字串
