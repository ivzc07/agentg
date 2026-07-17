"""ADR 0001: all Telegram code lives in channels/; nothing else imports aiogram."""

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "agentg"


def imports_aiogram(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(alias.name.split(".")[0] == "aiogram" for alias in node.names):
                return True
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and (node.module or "").split(".")[0] == "aiogram":
                return True
    return False


def test_only_the_channels_adapter_imports_aiogram():
    offenders = [
        str(path.relative_to(SRC))
        for path in SRC.rglob("*.py")
        if "channels" not in path.relative_to(SRC).parts
        and imports_aiogram(ast.parse(path.read_text(encoding="utf-8")))
    ]
    assert offenders == []
