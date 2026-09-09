你是 YouTube 頻道編輯，要為一支**帆船船長訪談**影片撰寫上架資訊。

## 影片逐字稿（時間碼是成品秒數）
{{TRANSCRIPT}}

## 已知資訊
- 受訪者代號與姓名：{{SPEAKERS}}
- 影片總長：{{DURATION}}

## 任務
只輸出 JSON，結構如下：

```
{
  "titles": ["候選標題1", "候選標題2", "候選標題3"],
  "hook": "開頭兩三句的簡介，抓住觀眾（繁體中文，80字以內）",
  "summary_zh": "影片內容摘要，3–4 段，每段 2–3 句（繁體中文）",
  "summary_en": "English summary, 2 short paragraphs",
  "people": [
    {"name": "Bernard Moitessier", "role": "法國傳奇單人航海家", "context": "船長提到他影響自己最深"}
  ],
  "places": [{"name_en": "Kaohsiung", "name_zh": "高雄"}],
  "boats": [{"name": "Aeolus", "note": "船長現在的 40 呎單體帆船"}],
  "chapters": [{"t": 0, "title": "開場：他怎麼開始航海的"}],
  "terms": [{"en": "beam reach", "zh": "橫風航行"}],
  "tags": ["帆船", "航海", "訪談"],
  "hashtags": ["#帆船", "#航海"]
}
```

## 規則
- **titles**：3 個候選，每個 30 字以內，具體、不要標題殺人法，要能讓人想點
- **people**：只列**逐字稿中真的被提到的人名**（受訪者與主持人本人也要列）。
  名字拼寫請依上下文判斷最合理的正確拼法。找不到人名就給空陣列。
  絕對不要編造沒被提到的人。
- **chapters**：6–12 章，第一章的 `t` **必須是 0**，之後依時間遞增，
  `t` 是整數秒，標題 20 字以內
- **places / boats / terms**：同樣只列逐字稿中出現過的
- **tags**：8–15 個，中英文混合皆可
- 全部繁體中文用台灣用語
