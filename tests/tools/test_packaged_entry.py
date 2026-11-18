import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _entry_scripts():
    spec = (ROOT / "packaging" / "FinishReview.spec").read_text(encoding="utf-8")
    names = re.findall(r'ROOT / "realtime" / "([a-z_]+\.py)"', spec)
    assert names, "no entry script found in the PyInstaller spec"
    return [ROOT / "realtime" / name for name in names]


def test_packaged_entry_scripts_use_absolute_imports():
    """The packaged exe runs its entry file as a plain script, where a relative
    import fails at start-up although every test (run as a module) passes."""

    for script in _entry_scripts():
        tree = ast.parse(script.read_text(encoding="utf-8"))
        relative = [node.lineno for node in ast.walk(tree)
                    if isinstance(node, ast.ImportFrom) and node.level > 0]
        assert relative == [], f"{script.name} has relative imports on lines {relative}"
