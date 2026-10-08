"""The tool registry. Adding a tool = write one Tool class, add it to ALL_TOOLS."""

from __future__ import annotations

from karya.llm.base import ToolSpec
from karya.tools import api, browser, control, files, human, memory
from karya.tools.base import Tool

ALL_TOOLS: list[type[Tool]] = [
    browser.BrowserOpen,
    browser.BrowserSnapshot,
    browser.BrowserClick,
    browser.BrowserFill,
    browser.BrowserSelect,
    browser.BrowserPress,
    browser.BrowserFind,
    browser.BrowserBack,
    browser.BrowserLogin,
    api.ApiGet,
    files.FilesList,
    files.FilesRead,
    files.FilesExtract,
    files.FilesWrite,
    memory.MemorySearch,
    memory.RecordFact,
    human.AskHuman,
    control.UpdatePlan,
    control.Finish,
    control.SubmitVerdicts,
]

_BY_NAME = {tool.name: tool for tool in ALL_TOOLS}


def get_tool(name: str) -> type[Tool] | None:
    return _BY_NAME.get(name)


def specs(role: str) -> list[ToolSpec]:
    """Tool definitions for the model. The verifier gets only read-only tools."""
    return [tool.spec() for tool in ALL_TOOLS if role in tool.roles]
