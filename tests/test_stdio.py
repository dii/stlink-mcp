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
    "board_state", "capture_console", "capture_swo", "core_registers", "flash_firmware",
    "list_probes", "locate_fault", "read_fault_status", "read_memory",
    "release_probe", "reset_target", "run_firmware", "run_until",
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
    return {
        "server": init.server_info,
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
                  "run_until"}
    for name, tool in seen["tools"].items():
        prop = tool.input_schema.get("properties", {}).get("interface")
        if name in connecting:
            assert prop is not None, f"{name} has no interface parameter"
            assert prop.get("enum") == ["swd", "jtag"] and prop.get("default") == "swd", (name, prop)
        else:
            assert prop is None, f"{name} does not connect and should not take an interface"
        assert "ctx" not in tool.input_schema.get("properties", {}), f"{name} exposes its context"
    # N9: every call is traced to who asked, on the server's stderr.
    assert "connection 1, request" in seen["stderr"] and "list_probes(" in seen["stderr"]
