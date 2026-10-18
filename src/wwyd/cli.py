# SPDX-License-Identifier: AGPL-3.0-or-later
"""Command line entry point: ``wwyd [serve]`` and ``wwyd stdio --user <login>``."""

from __future__ import annotations

import argparse
import logging
import sys

from wwyd import __version__

log = logging.getLogger("wwyd.cli")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wwyd", description="what would you do: persona MCP server"
    )
    parser.add_argument("--version", action="version", version=f"wwyd {__version__}")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("serve", help="run the HTTP server (milestone 2)")
    stdio = sub.add_parser("stdio", help="serve one user's persona over stdio (owner scope)")
    stdio.add_argument("--user", required=True, metavar="LOGIN", help="login of the owner")
    return parser


def run_stdio(login: str) -> int:
    from wwyd.config import db_path
    from wwyd.db import open_db
    from wwyd.logs import configure_logging
    from wwyd.mcp.caller import Caller, fixed_caller
    from wwyd.mcp.server import build_server
    from wwyd.store.personas import get_or_create_user

    configure_logging()
    conn = open_db(db_path())
    user, created = get_or_create_user(conn, login)
    if created:
        log.info(
            "created user %s with an empty persona%s",
            user.login,
            " (first user: admin)" if user.is_admin else "",
        )
    caller = Caller(
        user_id=user.user_id, persona_id=user.persona_id, scope="owner", client_name="stdio"
    )
    server = build_server(conn, fixed_caller(caller))
    try:
        server.run("stdio")
    finally:
        conn.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "stdio":
        return run_stdio(args.user)
    print(
        "wwyd: the HTTP server arrives in milestone 2; use `wwyd stdio --user <login>`.",
        file=sys.stderr,
    )
    return 2
