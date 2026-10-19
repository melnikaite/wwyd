# SPDX-License-Identifier: AGPL-3.0-or-later
"""Command line entry point: ``wwyd [serve]``, ``wwyd stdio --user <login>``, ``wwyd invite``,
``wwyd reset-password <login>``, ``wwyd eval <pairs.jsonl> --user <login>``."""

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
    serve = sub.add_parser("serve", help="run the HTTP server (default)")
    serve.add_argument("--host", help="bind address (default 127.0.0.1)")
    serve.add_argument("--port", type=int, help="port (default 8740)")
    sub.add_parser("invite", help="print a one-time invite link (valid 7 days)")
    reset = sub.add_parser("reset-password", help="print a one-time password reset link")
    reset.add_argument("login")
    stdio = sub.add_parser("stdio", help="serve one user's persona over stdio (owner scope)")
    stdio.add_argument("--user", required=True, metavar="LOGIN", help="login of the owner")
    ev = sub.add_parser("eval", help="blind-compare predictions with real replies (docs/eval.md)")
    ev.add_argument("pairs", metavar="PAIRS_JSONL", help="JSONL file of situation/audience pairs")
    ev.add_argument("--user", required=True, metavar="LOGIN", help="login of the owner")
    return parser


def _runtime() -> None:
    """Shared startup for the non-HTTP commands: logging, limits and the LLM backend."""
    from wwyd.limits import configure as configure_limits
    from wwyd.logs import configure_logging

    configure_logging()
    config = _config()
    configure_limits(config.limits)
    config.llm.apply()


def run_eval(path: str, login: str) -> int:
    from wwyd import eval as wwyd_eval

    _runtime()
    return wwyd_eval.main(path, login)


def run_stdio(login: str) -> int:
    from wwyd.config import db_path
    from wwyd.db import open_db
    from wwyd.ingest.jobs import mark_interrupted
    from wwyd.mcp.caller import Caller, fixed_caller
    from wwyd.mcp.server import build_server
    from wwyd.store.personas import get_or_create_user

    _runtime()
    conn = open_db(db_path())
    mark_interrupted(conn)
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


def _config(**overrides):  # noqa: ANN003, ANN202
    from wwyd.config import ConfigError, load_config

    try:
        return load_config(**overrides)
    except ConfigError as exc:
        print(f"wwyd: {exc}", file=sys.stderr)
        raise SystemExit(2) from None


def run_serve(host: str | None, port: int | None) -> int:
    import uvicorn

    from wwyd.app import create_app
    from wwyd.config import ConfigError
    from wwyd.logs import configure_logging

    configure_logging()
    # uvicorn's info lines would be redacted to a fixed text anyway (wwyd.logs); skip them.
    logging.getLogger("uvicorn").setLevel(logging.WARNING)
    config = _config(host=host, port=port)
    try:
        app = create_app(config)
    except ConfigError as exc:
        print(f"wwyd: {exc}", file=sys.stderr)
        return 2
    print(
        f"wwyd {__version__} listening on http://{config.host}:{config.port}"
        f" (public URL {config.public_url})",
        file=sys.stderr,
        flush=True,
    )
    uvicorn.run(
        app,
        host=config.host,
        port=config.port,
        log_config=None,
        proxy_headers=False,  # client IPs come from trusted_proxies only (wwyd.auth.throttle)
        server_header=False,
    )
    return 0


def run_link(kind: str, login: str | None = None) -> int:
    from wwyd.auth import accounts
    from wwyd.config import db_path
    from wwyd.db import open_db

    config = _config()
    conn = open_db(db_path())
    try:
        if kind == "invite":
            token = accounts.create_link(conn, "invite", created_by="cli")
            print(f"{config.public_url}/invite?t={token}")
            print("One-time invite link, valid for 7 days.", file=sys.stderr)
            return 0
        assert login is not None
        user = accounts.user_by_login(conn, login)
        if user is None:
            print(f"wwyd: no user with login {login!r}", file=sys.stderr)
            return 1
        token = accounts.create_link(conn, "reset", user_id=user.id, created_by="cli")
        print(f"{config.public_url}/reset?t={token}")
        print(
            "One-time password reset link, valid for 24 hours. Using it signs the user out "
            "everywhere and shows them that an administrator reset the password.",
            file=sys.stderr,
        )
        return 0
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "stdio":
        return run_stdio(args.user)
    if args.command == "eval":
        return run_eval(args.pairs, args.user)
    if args.command == "invite":
        return run_link("invite")
    if args.command == "reset-password":
        return run_link("reset", args.login)
    return run_serve(getattr(args, "host", None), getattr(args, "port", None))
