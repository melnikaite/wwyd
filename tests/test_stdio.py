# SPDX-License-Identifier: AGPL-3.0-or-later
"""Smoke tests for `wwyd stdio --user <login>` as a real subprocess."""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest
from mcp import Client, StdioServerParameters

pytestmark = pytest.mark.anyio

LOGIN = "test@example.com"


def _env(data_dir) -> dict[str, str]:
    return {**os.environ, "WWYD_DATA_DIR": str(data_dir), "WWYD_LOG_LEVEL": "DEBUG"}


async def test_stdio_client_lists_seeded_categories(data_dir):
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "wwyd", "stdio", "--user", LOGIN], env=_env(data_dir)
    )
    async with Client(params) as client:
        names = {t.name for t in (await client.list_tools()).tools}
        assert "list_categories" in names
        result = await client.call_tool("list_categories", {})
        assert not result.is_error
        roots = {n["path"] for n in result.structured_content["categories"]}
        assert {"communication", "work", "schedule", "food", "tech"} <= roots
    assert (data_dir / "wwyd.db").exists()


def test_stdout_carries_only_protocol_messages(data_dir):
    messages = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "raw-test", "version": "0"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "list_categories", "arguments": {}},
        },
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "add_claims", "arguments": {"claims": "not a list"}},
        },
    ]
    proc = subprocess.Popen(
        [sys.executable, "-m", "wwyd", "stdio", "--user", LOGIN],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_env(data_dir),
    )
    parsed = []
    try:
        for message in messages:
            proc.stdin.write(json.dumps(message) + "\n")
            proc.stdin.flush()
            if "id" not in message:
                continue
            while True:  # every stdout line must be a JSON-RPC message
                line = proc.stdout.readline()
                assert line, "server closed stdout early"
                reply = json.loads(line)
                assert reply.get("jsonrpc") == "2.0"
                parsed.append(reply)
                if reply.get("id") == message["id"]:
                    break
        proc.stdin.close()
        rest, stderr = proc.stdout.read(), proc.stderr.read()
        proc.wait(timeout=30)
    finally:
        if proc.poll() is None:
            proc.kill()
    for line in rest.splitlines():
        if line.strip():
            assert json.loads(line).get("jsonrpc") == "2.0"
    by_id = {p["id"]: p for p in parsed if "id" in p}
    paths = {n["path"] for n in by_id[2]["result"]["structuredContent"]["categories"]}
    assert "work" in paths
    assert by_id[3]["result"]["isError"] is True
    assert by_id[3]["result"]["structuredContent"]["error"]["code"] == "invalid"
    assert f"created user {LOGIN}" in stderr and "admin" in stderr


def test_serve_is_not_available_yet(data_dir):
    proc = subprocess.run(
        [sys.executable, "-m", "wwyd"],
        capture_output=True,
        text=True,
        env=_env(data_dir),
        timeout=30,
    )
    assert proc.returncode != 0 and "milestone 2" in proc.stderr and proc.stdout == ""
