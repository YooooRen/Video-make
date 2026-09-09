"""
呼叫 Claude 的統一介面。

backend:
  cli  — 用 Claude Code 無介面模式 (`claude -p`)，吃你的 Claude 訂閱額度，不需要 API key
  api  — 用 Anthropic API（需要 ANTHROPIC_API_KEY）
  auto — 有 API key 就走 api，否則退回 cli

所有回覆都會依 prompt 內容做磁碟快取，重跑某個階段時不會重複燒額度。
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Sequence

from .util import StageError, extract_json, have, log, run, warn


class ClaudeClient:
    def __init__(self, cfg):
        self.cfg = cfg
        self.binary = cfg.get("claude.cli_binary", "claude")
        self.model = cfg.get("claude.model", "") or ""
        self.timeout = int(cfg.get("claude.timeout", 900))
        self.max_retries = int(cfg.get("claude.max_retries", 3))
        self.use_cache = bool(cfg.get("claude.use_cache", True))
        self.cache_dir = cfg.path(cfg.get("claude.cache_dir", "build/.claude_cache"))
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.prompt_mode = cfg.get("claude.prompt_mode", "auto")
        self.extra_args: list[str] = list(cfg.get("claude.extra_args", []) or [])
        self.backend = self._pick_backend(cfg.get("claude.backend", "cli"))
        self._api = None
        self.calls = 0
        self.cache_hits = 0

    # ------------------------------------------------------------ backend --
    def _pick_backend(self, want: str) -> str:
        key_env = self.cfg.get("claude.api_key_env", "ANTHROPIC_API_KEY")
        has_key = bool(os.environ.get(key_env))
        if want == "auto":
            return "api" if has_key else "cli"
        if want == "api" and not has_key:
            raise StageError(f"backend=api 但環境變數 {key_env} 是空的。")
        if want == "cli" and not have(self.binary):
            raise StageError(
                f"找不到 `{self.binary}`。請先安裝 Claude Code："
                " npm install -g @anthropic-ai/claude-code，並執行一次 `claude` 完成登入。")
        return want

    # -------------------------------------------------------------- cache --
    def _cache_key(self, prompt: str, system: str, images: Sequence[str]) -> str:
        img_sig = "|".join(sorted(str(i) for i in images))
        raw = f"{self.backend}\n{self.model}\n{system}\n{prompt}\n{img_sig}"
        return hashlib.sha256(raw.encode()).hexdigest()[:32]

    def _cache_get(self, key: str) -> str | None:
        if not self.use_cache:
            return None
        p = self.cache_dir / f"{key}.json"
        if p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8"))["text"]
            except Exception:
                return None
        return None

    def _cache_put(self, key: str, text: str) -> None:
        if not self.use_cache:
            return
        (self.cache_dir / f"{key}.json").write_text(
            json.dumps({"text": text}, ensure_ascii=False), encoding="utf-8")

    # ---------------------------------------------------------------- ask --
    def ask(self, prompt: str, *, system: str = "", images: Sequence[str] = (),
            label: str = "claude") -> str:
        """送出一次請求，回傳純文字回覆。"""
        images = [str(Path(i).resolve()) for i in images]
        key = self._cache_key(prompt, system, images)
        cached = self._cache_get(key)
        if cached is not None:
            self.cache_hits += 1
            log(label, f"快取命中（省下一次呼叫）")
            return cached

        last_err: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                if self.backend == "cli":
                    text = self._ask_cli(prompt, system, images)
                else:
                    text = self._ask_api(prompt, system, images)
                self.calls += 1
                self._cache_put(key, text)
                return text
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                if attempt < self.max_retries:
                    wait = 2 ** attempt
                    warn(label, f"第 {attempt} 次失敗（{exc}），{wait}s 後重試")
                    time.sleep(wait)
        raise StageError(f"Claude 呼叫失敗（已重試 {self.max_retries} 次）：{last_err}")

    def ask_json(self, prompt: str, *, system: str = "", images: Sequence[str] = (),
                 label: str = "claude", retries_on_parse: int = 2) -> Any:
        """送出請求並強制解析成 JSON；解析失敗會補一句要求重出並重試。"""
        attempt = 0
        cur_prompt = prompt
        while True:
            text = self.ask(cur_prompt, system=system, images=images, label=label)
            try:
                return extract_json(text)
            except StageError:
                attempt += 1
                if attempt > retries_on_parse:
                    raise
                warn(label, "回覆不是合法 JSON，要求重新輸出")
                cur_prompt = (prompt +
                              "\n\n【重要】上一次的回覆無法解析。請只輸出合法 JSON，"
                              "不要加任何說明文字、不要用 markdown 程式碼區塊。")
                # 換一個 cache key，避免又拿到同一份壞回覆
                cur_prompt += f"\n<retry>{attempt}</retry>"

    # ---------------------------------------------------------------- CLI --
    def _ask_cli(self, prompt: str, system: str, images: Sequence[str]) -> str:
        full = prompt
        if images:
            listing = "\n".join(f"- {p}" for p in images)
            full = (f"請先用 Read 工具讀取以下圖片檔，再回答問題：\n{listing}\n\n{prompt}")

        cmd = [self.binary, "-p", "--output-format", "json"]
        if self.model:
            cmd += ["--model", self.model]
        if system:
            cmd += ["--append-system-prompt", system]
        if images:
            cmd += ["--allowed-tools", "Read"]
            dirs = sorted({str(Path(p).parent) for p in images})
            for d in dirs:
                cmd += ["--add-dir", d]
        cmd += self.extra_args

        use_stdin = self.prompt_mode == "stdin" or (
            self.prompt_mode == "auto" and len(full) > 60000)
        if use_stdin:
            proc = run(cmd, stdin=full, timeout=self.timeout, check=False)
        else:
            proc = run(cmd + [full], timeout=self.timeout, check=False)

        if proc.returncode != 0:
            raise StageError(f"claude CLI 回傳 {proc.returncode}: "
                             f"{(proc.stderr or proc.stdout or '')[-1500:]}")
        out = (proc.stdout or "").strip()
        if not out:
            raise StageError("claude CLI 沒有輸出")
        try:
            payload = json.loads(out)
        except json.JSONDecodeError:
            return out          # --output-format json 沒生效時，直接當純文字用
        if isinstance(payload, dict):
            if payload.get("is_error"):
                raise StageError(f"claude 回報錯誤：{payload.get('result')}")
            result = payload.get("result")
            if isinstance(result, str):
                return result
        return out

    # ---------------------------------------------------------------- API --
    def _ask_api(self, prompt: str, system: str, images: Sequence[str]) -> str:
        import base64
        import mimetypes
        if self._api is None:
            try:
                import anthropic
            except ImportError as exc:
                raise StageError("backend=api 需要 `pip install anthropic`") from exc
            self._api = anthropic.Anthropic(
                api_key=os.environ[self.cfg.get("claude.api_key_env", "ANTHROPIC_API_KEY")])

        content: list[dict] = []
        for img in images:
            mime = mimetypes.guess_type(img)[0] or "image/jpeg"
            data = base64.b64encode(Path(img).read_bytes()).decode()
            content.append({"type": "image",
                            "source": {"type": "base64", "media_type": mime, "data": data}})
        content.append({"type": "text", "text": prompt})

        msg = self._api.messages.create(
            model=self.model or "claude-sonnet-5",
            max_tokens=16000,
            system=system or None,
            messages=[{"role": "user", "content": content}],
        )
        return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")

    # -------------------------------------------------------------- 統計 ---
    def summary(self) -> str:
        return f"Claude 呼叫 {self.calls} 次，快取命中 {self.cache_hits} 次（backend={self.backend}）"


def load_prompt(name: str) -> str:
    """讀取 prompts/ 底下的提示詞範本。"""
    p = Path(__file__).resolve().parent.parent / "prompts" / name
    if not p.exists():
        raise StageError(f"找不到提示詞範本 {p}")
    return p.read_text(encoding="utf-8")
