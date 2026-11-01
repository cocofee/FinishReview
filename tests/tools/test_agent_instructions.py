"""入口规则的体积与历史归档隔离回归。"""

from pathlib import Path

import pytest

from tools.check_agent_instructions import (
    ENTRY_NAMES,
    MAX_FILE_BYTES,
    MAX_FILE_LINES,
    check_instructions,
    main,
)


def write_rule(root, name, text):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))  # 固定字节，避免 Windows 换行转换影响边界测试。


def test_current_project_instructions_pass():
    assert check_instructions(Path(__file__).resolve().parents[2]) == []


def test_claude_imports_shared_entry_without_history():
    root = Path(__file__).resolve().parents[2]
    text = (root / "CLAUDE.md").read_text(encoding="utf-8")
    assert text.splitlines() == ["# FinishReview Shared Instructions", "", "@AGENTS.md"]


def test_ci_runs_instruction_gate_before_dependency_install():
    root = Path(__file__).resolve().parents[2]
    workflow = (root / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert workflow.index("run: python tools/check_agent_instructions.py") < workflow.index(
        "- name: Install dependencies"
    )


def test_missing_shared_entry_fails(tmp_path):
    assert "缺少共享入口" in "\n".join(check_instructions(tmp_path))


@pytest.mark.parametrize("name", ENTRY_NAMES + (".claude/rules/nested/timing.md",))
def test_each_entry_rejects_oversized_utf8_content(tmp_path, name):
    write_rule(tmp_path, "AGENTS.md", "# 规则\n")
    write_rule(tmp_path, name, "规" * (MAX_FILE_BYTES // 3 + 1))
    assert "超限" in "\n".join(check_instructions(tmp_path))


def test_exact_file_limits_pass_and_one_more_line_fails(tmp_path):
    content = "x" * (MAX_FILE_BYTES - MAX_FILE_LINES) + "\n" * MAX_FILE_LINES
    write_rule(tmp_path, "AGENTS.md", content)
    assert check_instructions(tmp_path) == []
    write_rule(tmp_path, "AGENTS.md", "\n" * (MAX_FILE_LINES + 1))
    assert "超限" in "\n".join(check_instructions(tmp_path))


@pytest.mark.parametrize("content", ["x" * 7000, "x\n" * 110])
def test_combined_entries_have_a_separate_limit(tmp_path, content):
    write_rule(tmp_path, "AGENTS.md", content)
    write_rule(tmp_path, "CLAUDE.md", content)
    assert "合计超限" in "\n".join(check_instructions(tmp_path))


@pytest.mark.parametrize(
    "content",
    [
        "@docs/agents_audit_history.md",
        "请读取 @docs/agents_audit_history.md",
        r"@docs\agents_audit_history.md",
        "@./docs/AGENTS_AUDIT_HISTORY.md",
        "## 审核记录摘要\n",
    ],
)
def test_history_cannot_return_to_startup_context(tmp_path, content):
    write_rule(tmp_path, "AGENTS.md", content)
    assert check_instructions(tmp_path)


def test_plain_history_link_and_large_archive_are_allowed(tmp_path):
    write_rule(tmp_path, "AGENTS.md", "按需查阅 `docs/agents_audit_history.md`。\n")
    write_rule(tmp_path, "docs/agents_audit_history.md", "历史\n" * 10000)
    assert check_instructions(tmp_path) == []


def test_invalid_encoding_reports_error(tmp_path):
    (tmp_path / "AGENTS.md").write_bytes(b"\xff\xfe")
    assert "无法按 UTF-8 读取" in "\n".join(check_instructions(tmp_path))


def test_main_reports_failure_and_success(tmp_path, capsys):
    assert main(tmp_path) == 1
    assert "缺少共享入口" in capsys.readouterr().err
    write_rule(tmp_path, "AGENTS.md", "# 规则\n")
    assert main(tmp_path) == 0
    assert "检查通过" in capsys.readouterr().out
