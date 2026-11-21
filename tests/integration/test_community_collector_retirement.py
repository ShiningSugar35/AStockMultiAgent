"""Retired collectors must not expose an executable CLI or package API."""

from importlib.util import find_spec

from typer.core import TyperGroup
from typer.main import get_command

import astock.knowledge as knowledge
from astock.cli import app


def test_retired_community_collectors_are_absent():
    command = get_command(app)
    assert isinstance(command, TyperGroup)
    commands = command.commands
    assert not any(name.startswith("zhihu-") for name in commands)
    for module in (
        "capture",
        "capture_coordinator",
        "python_recovery",
        "article_recovery",
        "transport",
        "imports",
        "service",
        "adapter",
        "manual_tasks",
    ):
        assert find_spec(f"astock.knowledge.{module}") is None
    for name in ("ZhihuCollectionService", "ZhihuResponseImportService", "ZhihuHttpTransport"):
        assert not hasattr(knowledge, name)
    assert "knowledge-zhihu-visual-run" not in commands
    assert not hasattr(knowledge.ZhihuVisualPipelineService, "run")
    assert "knowledge-source-list" in commands
    assert "knowledge-completion-finalize" in commands
