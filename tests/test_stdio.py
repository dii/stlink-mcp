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

import anyio

from stlink_mcp import __version__

TOOLS = {
    "board_state", "capture_console", "core_registers", "flash_firmware",
    "list_probes", "read_fault_status", "read_memory", "release_probe",
    "reset_target", "run_firmware",
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
    with anyio.fail_after(60):
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                init = await session.initialize()
                listed = await session.list_tools()
                probes = await session.call_tool("list_probes", {})
                read_hex = await session.call_tool(
                    "read_memory", {"address": "0x20000000", "probe_serial": "NONE"}
                )
    return {
        "server": init.server_info,
        "tools": {tool.name: tool for tool in listed.tools},
        "list_probes": json.loads(probes.content[0].text),
        "read_memory": json.loads(read_hex.content[0].text),
    }


def test_a_client_sees_the_ten_tools_and_contract_shaped_results() -> None:
    seen = anyio.run(_session)
    assert seen["server"].name == "stlink-mcp"
    assert seen["server"].version == __version__
    assert set(seen["tools"]) == TOOLS
    assert seen["list_probes"]["status"] in ("verified", "failed")
    # A hex string is accepted by the schema and reaches the tool: the answer
    # is about the toolchain or the probe, not a validation error.
    assert seen["read_memory"]["status"] == "failed"
    assert "0x20000000" not in seen["read_memory"]["error"]
