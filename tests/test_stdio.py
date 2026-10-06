"""The server as an MCP client meets it: spawned over stdio, then asked.

Every other test calls the tool functions directly, past the MCP layer, so none
of them would notice the SDK failing to register a tool, a schema changing
shape, or the process failing to start. No hardware is needed: with or without
STM32CubeCLT installed, list_probes answers in the result contract.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile

import anyio

from stlink_mcp import __version__

TOOLS = {
    "board_state", "capture_console", "capture_swo", "core_registers", "disassemble", "flash_firmware",
    "list_probes", "locate_fault", "read_fault_status", "read_memory", "read_option_bytes",
    "release_probe", "reset_target", "run_firmware", "run_until", "sample_values",
}


async def _session() -> dict:
    from mcp.client.session import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "stlink_mcp"],
        # Seeding parses a hundred XML files when CubeCLT is present, which this
        # test has no use for.
        env={**os.environ, "STLINK_MCP_NO_VENDOR_SEED": "1"},
    )
    errlog = tempfile.TemporaryFile("w+")
    with anyio.fail_after(60):
        async with stdio_client(params, errlog=errlog) as (read, write):
            async with ClientSession(read, write) as session:
                init = await session.initialize()
                listed = await session.list_tools()
                probes = await session.call_tool("list_probes", {})
                read_hex = await session.call_tool(
                    "read_memory", {"address": "0x20000000", "probe_serial": "NONE"}
                )
                misspelt = await session.call_tool("read_memory", {"adress": "0x20000000", "lenght": 8})
                wrong_interface = await session.call_tool("board_state", {"interfase": "jtag"})
    return {
        "server": init.server_info,
        "instructions": init.instructions,
        "misspelt": json.loads(misspelt.content[0].text),
        "wrong_interface": json.loads(wrong_interface.content[0].text),
        "tools": {tool.name: tool for tool in listed.tools},
        "list_probes": json.loads(probes.content[0].text),
        "read_memory": json.loads(read_hex.content[0].text),
        "stderr": (errlog.seek(0), errlog.read())[1],
    }


def test_a_client_sees_every_tool_and_contract_shaped_results() -> None:
    seen = anyio.run(_session)
    assert seen["server"].name == "stlink-mcp"
    assert seen["server"].version == __version__
    assert set(seen["tools"]) == TOOLS
    assert seen["list_probes"]["status"] in ("verified", "failed")
    # A hex string is accepted by the schema and reaches the tool: the answer
    # is about the toolchain or the probe, not a validation error.
    assert seen["read_memory"]["status"] == "failed"
    assert "0x20000000" not in seen["read_memory"]["error"]
    # N8: every tool that connects offers the interface as a choice of two, so
    # a client sees the options without reading a docstring.
    connecting = {"board_state", "run_firmware", "flash_firmware", "read_memory",
                  "reset_target", "core_registers", "read_fault_status", "locate_fault",
                  "run_until", "read_option_bytes", "disassemble", "sample_values"}
    for name, tool in seen["tools"].items():
        prop = tool.input_schema.get("properties", {}).get("interface")
        if name in connecting:
            assert prop is not None, f"{name} has no interface parameter"
            assert prop.get("enum") == ["swd", "jtag"] and prop.get("default") == "swd", (name, prop)
        else:
            assert prop is None, f"{name} does not connect and should not take an interface"
        assert "ctx" not in tool.input_schema.get("properties", {}), f"{name} exposes its context"
        # AGENTS.md: an agent loads every description on every session, so each
        # is held to the 40 to 80 words the repo sets.
        words = len((tool.description or "").split())
        assert 40 <= words <= 80, f"{name}'s description is {words} words"
    # N9: every call is traced to who asked, on the server's stderr.
    assert "connection 1, request" in seen["stderr"] and "list_probes(" in seen["stderr"]


def test_a_client_is_told_how_to_use_the_server_even_without_the_tool_descriptions() -> None:
    """X9. Claude Code shows server instructions for tools it has deferred; Google's
    Antigravity command line gives its model the tools through one generic call. Every tool named in
    them must exist, and they say how to read a result."""
    import re
    seen = anyio.run(_session)
    text = seen["instructions"] or ""
    named = set(re.findall(r"`([a-z_]+)`", text))
    assert named and named - {"probe_serial"} <= TOOLS, named - TOOLS
    assert {"run_firmware", "sample_values", "release_probe"} <= named
    assert "unverified" in text and "not success" in text


def test_an_argument_no_parameter_has_is_refused_not_dropped() -> None:
    """X10. The SDK dropped a misspelt argument without a word, so `lenght` read
    the default length and a misspelt `interface` would have used SWD."""
    seen = anyio.run(_session)
    misspelt, interface = seen["misspelt"], seen["wrong_interface"]
    assert misspelt["status"] == "failed" and misspelt["unknown_arguments"] == ["adress", "lenght"]
    assert "address for adress" in misspelt["error"] and "length_bytes" in misspelt["error"]
    assert interface["status"] == "failed" and "interface for interfase" in interface["error"]


def test_the_readme_gives_users_the_same_guide_the_server_sends() -> None:
    """X9. For a client that does not show server instructions to its agent,
    the README's copy is what goes into the project's AGENTS.md; it must not
    drift from what the server says."""
    from pathlib import Path
    from stlink_mcp.server import INSTRUCTIONS
    readme = (Path(__file__).parent.parent / "README.md").read_text()
    assert INSTRUCTIONS.strip() in readme


def test_the_guide_names_every_tool() -> None:
    """X9. A client that shows the agent no tool descriptions leaves the guide
    as the only place the agent learns a tool exists."""
    from stlink_mcp import server
    missing = [name for name in server.mcp._tool_manager._tools if f"`{name}`" not in server.INSTRUCTIONS]
    assert not missing, f"INSTRUCTIONS does not name {missing}"
