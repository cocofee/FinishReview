"""检查 Claude / Codex 入口规则，防止历史报告重新挤入常驻上下文。"""

from pathlib import Path
import re
import sys


MAX_FILE_BYTES = 8 * 1024
MAX_FILE_LINES = 140
MAX_TOTAL_BYTES = 12 * 1024
MAX_TOTAL_LINES = 200
ENTRY_NAMES = (
    "AGENTS.md",
    "AGENTS.override.md",
    "CLAUDE.md",
    "CLAUDE.local.md",
    ".claude/CLAUDE.md",
)
HISTORY_IMPORT = re.compile(r"@[^\s`\"'<>]*agents_audit_history\.md\b", re.I)


def check_instructions(root: Path) -> list[str]:
    """仅检查本项目入口和 Claude 项目规则，不改用户全局配置。"""
    errors = []  # 中文错误，供命令行与测试共用。
    paths = [root / name for name in ENTRY_NAMES if (root / name).is_file()]
    rules_dir = root / ".claude" / "rules"
    if rules_dir.is_dir():
        paths.extend(sorted(rules_dir.rglob("*.md")))
    if not (root / "AGENTS.md").is_file():
        errors.append("缺少共享入口 AGENTS.md。")

    total_bytes = total_lines = 0
    for path in paths:
        name = path.relative_to(root).as_posix()
        try:
            raw = path.read_bytes()
            text = raw.decode("utf-8-sig")
        except (OSError, UnicodeError) as exc:
            errors.append(f"无法按 UTF-8 读取 {name}：{exc}")
            continue
        line_count = len(text.splitlines())
        total_bytes += len(raw)
        total_lines += line_count
        if len(raw) > MAX_FILE_BYTES or line_count > MAX_FILE_LINES:
            errors.append(
                f"{name} 超限：{len(raw)} 字节 / {line_count} 行；"
                f"最多 {MAX_FILE_BYTES} 字节 / {MAX_FILE_LINES} 行，请移出历史明细。"
            )
        if HISTORY_IMPORT.search(text):
            errors.append(f"{name} 不得用 @ 自动导入历史归档；请按需检索。")
        if re.search(r"^#{1,6}\s+审核记录摘要\s*$", text, re.M):
            errors.append(f"{name} 不得重新堆积审核记录摘要。")

    if total_bytes > MAX_TOTAL_BYTES or total_lines > MAX_TOTAL_LINES:
        errors.append(
            f"入口规则合计超限：{total_bytes} 字节 / {total_lines} 行；"
            f"最多 {MAX_TOTAL_BYTES} 字节 / {MAX_TOTAL_LINES} 行。"
        )
    return errors


def main(root: Path | None = None) -> int:
    project_root = root if root is not None else Path(__file__).resolve().parents[1]
    errors = check_instructions(project_root)
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1
    print("Claude / Codex 入口规则检查通过：大小、行数及历史归档导入符合约定。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
