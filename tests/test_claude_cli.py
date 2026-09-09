#!/usr/bin/env python3
"""
claude CLI 呼叫方式的回歸測試。

重點防守一個實際踩過的坑：Claude Code 的 --add-dir 與 --allowed-tools 都接受
多個值，任何接在它們後面的位置參數都會被當成「又一個值」吃掉，導致 CLI 收不到
prompt 而回報 "Input must be provided either through stdin or as a prompt
argument when using --print"。

    python tests/test_claude_cli.py
"""
from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline.claude_client import ClaudeClient, build_cli_command   # noqa: E402
from pipeline.config import load_config                              # noqa: E402

_failures: list[str] = []

# 這些旗標在 Claude Code 會吞掉後面所有非旗標的字
VARIADIC = {"--add-dir", "--allowed-tools", "--allowedTools",
            "--disallowed-tools", "--disallowedTools", "--mcp-config"}


def check(cond: bool, msg: str) -> None:
    print(("  ✅ " if cond else "  ❌ ") + msg)
    if not cond:
        _failures.append(msg)


# 忠實模擬 Claude Code 參數解析的假 CLI：可變長度旗標會吃掉後面的位置參數
STUB = '''#!/usr/bin/env python3
import json, sys
VARIADIC = {"--add-dir", "--allowed-tools", "--allowedTools",
            "--disallowed-tools", "--disallowedTools", "--mcp-config"}
SINGLE = {"--model", "--append-system-prompt", "--output-format",
          "--permission-mode", "--settings"}

args = sys.argv[1:]
positional, i, seen_ddash = [], 0, False
while i < len(args):
    a = args[i]
    if a == "--":
        seen_ddash = True
        positional.extend(args[i + 1:])
        break
    if a in VARIADIC:                      # 吃掉後面所有非旗標的字
        i += 1
        while i < len(args) and not args[i].startswith("-"):
            i += 1
        continue
    if a in SINGLE:
        i += 2
        continue
    if a.startswith("-"):
        i += 1
        continue
    positional.append(a)
    i += 1

if positional:
    prompt = positional[0]
elif sys.stdin.isatty():
    prompt = ""
else:
    prompt = sys.stdin.read()

if not prompt.strip():
    sys.stderr.write("Error: Input must be provided either through stdin or "
                     "as a prompt argument when using --print\\n")
    sys.exit(1)

json.dump({"type": "result", "subtype": "success", "is_error": False,
           "result": prompt}, sys.stdout)
'''


def make_stub(d: Path) -> Path:
    p = d / "claude_stub.py"
    p.write_text(STUB)
    p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return p


def test_command_shape() -> None:
    print("\n▶ 參數組裝")
    cmd = build_cli_command("claude", images=["/tmp/a/x.jpg", "/tmp/b/y.jpg"])
    check("--add-dir" in cmd, "帶圖片時會加上 --add-dir")
    check(cmd.count("--add-dir") == 2, "每個目錄各給一次 --add-dir")

    # 最關鍵的一條：指令尾端不能是可變長度旗標的值，否則 prompt 會被吃掉
    tail_is_variadic_value = False
    for i, tok in enumerate(cmd):
        if tok in VARIADIC:
            j = i + 1
            while j < len(cmd) and not cmd[j].startswith("-"):
                j += 1
            if j >= len(cmd):
                tail_is_variadic_value = True
    check(tail_is_variadic_value,
          "指令確實以可變長度旗標的值收尾 → 因此 prompt 絕不能用位置參數傳")

    plain = build_cli_command("claude", model="claude-opus-5", system="sys")
    check("--model" in plain and "claude-opus-5" in plain, "model 有帶進去")
    check("--append-system-prompt" in plain, "system prompt 有帶進去")
    check(all(not t.startswith("PROMPT") for t in plain), "組裝結果不包含 prompt 本身")


def test_roundtrip(tmp: Path) -> None:
    print("\n▶ 實際呼叫（假 CLI 模擬真實的參數解析）")
    stub = make_stub(tmp)
    cfg_path = tmp / "project.yaml"
    cfg_path.write_text(f"""
project:
  name: "CLI Test"
  source_video: "{tmp}/nope.mov"
  build_dir: "{tmp}/build"
claude:
  backend: "cli"
  cli_binary: "{stub}"
  use_cache: false
  max_retries: 1
""", encoding="utf-8")
    cfg = load_config(cfg_path)
    client = ClaudeClient(cfg)

    got = client.ask("PROMPT-NO-IMAGES", label="test")
    check(got.strip() == "PROMPT-NO-IMAGES", "純文字呼叫：prompt 完整送達")

    img_dir = tmp / "sheets"
    img_dir.mkdir(exist_ok=True)
    img = img_dir / "A000.jpg"
    img.write_bytes(b"\xff\xd8\xff")
    got = client.ask("PROMPT-WITH-IMAGES", images=[str(img)], label="test")
    check("PROMPT-WITH-IMAGES" in got,
          "帶圖片呼叫：prompt 完整送達（這正是原本會失敗的情境）")
    check("A000.jpg" in got, "圖片路徑有一起傳給模型")

    payload = client.ask_json(
        'JSON-TEST {"ok": true, "n": 42}', label="test")
    check(isinstance(payload, dict) and payload.get("n") == 42,
          "ask_json 能從回覆中抽出 JSON")


def test_arg_mode_fallback(tmp: Path) -> None:
    print("\n▶ prompt_mode=arg 的自動退回")
    stub = make_stub(tmp)
    cfg_path = tmp / "project_arg.yaml"
    cfg_path.write_text(f"""
project:
  name: "CLI Test"
  source_video: "{tmp}/nope.mov"
  build_dir: "{tmp}/build2"
claude:
  backend: "cli"
  cli_binary: "{stub}"
  prompt_mode: "arg"
  use_cache: false
  max_retries: 1
""", encoding="utf-8")
    client = ClaudeClient(load_config(cfg_path))
    img_dir = tmp / "sheets2"
    img_dir.mkdir(exist_ok=True)
    img = img_dir / "B000.jpg"
    img.write_bytes(b"\xff\xd8\xff")
    got = client.ask("ARG-MODE-PROMPT", images=[str(img)], label="test")
    check("ARG-MODE-PROMPT" in got, "設成 arg 模式時，用 -- 分隔仍能正確送達")


def main() -> int:
    test_command_shape()
    tmp = Path(tempfile.mkdtemp(prefix="claude-cli-test-"))
    try:
        test_roundtrip(tmp)
        test_arg_mode_fallback(tmp)
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "═" * 52)
    if _failures:
        print(f"  ❌ {len(_failures)} 項失敗：")
        for f in _failures:
            print(f"     - {f}")
        print("═" * 52)
        return 1
    print("  ✅ 全部通過")
    print("═" * 52)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
