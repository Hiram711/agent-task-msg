#!/usr/bin/env python3
"""把 .ps1 规范化为 CRLF + 单个 UTF-8 BOM。

PowerShell 5.1 对这两点都敏感：
  - LF 行尾会让多行 try/catch、多行方法调用参数报 ParserError；
  - 缺 BOM 会让非 ASCII 字符按 ANSI 解码，中文变乱码。

注意必须用 utf-8-sig 读取并显式剥掉残留的 \\ufeff：
用 utf-8 读 + utf-8-sig 写会每次多叠一个 BOM，PowerShell 只吃掉第一个，
剩下的 \\ufeff 会留在脚本正文最前面，导致开头的 <# ... #> 注释块失效。
"""
import io
import sys
from pathlib import Path


def fix(path: Path) -> str:
    raw = path.read_bytes()
    text = raw.decode("utf-8-sig")
    stripped = text.lstrip("﻿")
    extra = len(text) - len(stripped)
    body = stripped.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\r\n")
    with io.open(path, "w", encoding="utf-8-sig", newline="") as f:
        f.write(body)
    return "ok" if extra == 0 else f"ok (剥掉 {extra} 个多余 BOM)"


def main(argv):
    targets = []
    if argv:
        for a in argv:
            p = Path(a)
            targets.extend(sorted(p.rglob("*.ps1")) if p.is_dir() else [p])
    else:
        targets = sorted(Path(".").rglob("*.ps1"))
    if not targets:
        print("没有找到 .ps1 文件")
        return 1
    for p in targets:
        try:
            print(f"{p}: {fix(p)}")
        except Exception as e:
            print(f"{p}: 失败 {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
