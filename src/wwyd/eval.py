# SPDX-License-Identifier: AGPL-3.0-or-later
"""``wwyd eval pairs.jsonl``: can the owner tell their real reply from the prediction? (§13)

Each line is ``{"situation", "audience", "real_reply"}``. For every pair the script runs
predict in-process as the owner, shows both replies in random order and asks which one is
real. Detection rate 50% means indistinguishable. Nothing is stored (no audit rows); LLM
token usage is still counted against the persona's limits.
"""

from __future__ import annotations

import asyncio
import json
import random
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TextIO

from wwyd.config import db_path
from wwyd.db import open_db
from wwyd.mcp.caller import Caller
from wwyd.predict import predict
from wwyd.store.personas import get_user


def main(
    path: str | Path,
    login: str,
    *,
    input_fn: Callable[[str], str] = input,
    out: TextIO | None = None,
    rng: random.Random | None = None,
    conn=None,  # noqa: ANN001 - tests pass an open connection
) -> int:
    out = out or sys.stdout
    rng = rng or random.Random()
    own_conn = conn is None
    conn = conn or open_db(db_path())
    try:
        user = get_user(conn, login)
        if user is None:
            print(f"no such user: {login}", file=out)
            return 2
        caller = Caller(user.user_id, user.persona_id, "owner", client_name="wwyd eval")
        try:
            lines = Path(path).read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            print(f"cannot read {path}: {type(exc).__name__}", file=out)
            return 2
        guessed = correct = skipped = 0
        for number, line in enumerate(lines, 1):
            if not line.strip():
                continue
            try:
                pair = json.loads(line)
                situation, audience, real = pair["situation"], pair["audience"], pair["real_reply"]
            except (ValueError, KeyError, TypeError):
                print(f"line {number}: skipped (needs situation, audience, real_reply)", file=out)
                skipped += 1
                continue
            result = asyncio.run(
                predict(conn, caller, situation=situation, audience=audience, audit=False)
            )
            if result["type"] != "answer":
                code = result.get("escalate") or result["type"]
                print(f"line {number}: no prediction ({code})", file=out)
                skipped += 1
                continue
            options = [("real", real), ("predicted", result["answer"])]
            rng.shuffle(options)
            print(f"\n--- {number} [{audience}] ---\n{situation}\n", file=out)
            for label, (_, text) in zip("AB", options, strict=True):
                print(f"{label}: {text}", file=out)
            while True:
                choice = input_fn("Which one is real? [a/b, s = skip, q = quit] ").strip().lower()
                if choice in ("a", "b", "s", "q"):
                    break
            if choice == "q":
                break
            if choice == "s":
                skipped += 1
                continue
            guessed += 1
            picked = options[0 if choice == "a" else 1][0]
            correct += picked == "real"
        print(f"\nGuessed {guessed}, skipped {skipped}.", file=out)
        if guessed:
            rate = correct / guessed
            print(f"Detection rate: {rate:.0%} (50% = indistinguishable)", file=out)
        return 0
    finally:
        if own_conn:
            conn.close()
