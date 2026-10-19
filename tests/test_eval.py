# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

import io
import json
import random

from test_predict import STYLE, answer, seed

from wwyd.eval import main


def test_eval_detection_rate(conn, user, pid, fake_llm, tmp_path):
    seed(conn, pid, [STYLE])
    fake_llm.on("answer", answer("predicted reply"))
    pairs = tmp_path / "pairs.jsonl"
    lines = [{"situation": f"s{i}", "audience": "everyone", "real_reply": "real"} for i in range(4)]
    pairs.write_text("\n".join(json.dumps(x) for x in lines) + "\nnot json\n")
    out = io.StringIO()
    shown: list[str] = []

    def guess(prompt: str) -> str:  # always pick whichever option says "real"
        text = out.getvalue()
        shown.append(text)
        return "a" if text.rstrip().splitlines()[-2].endswith("real") else "b"

    assert main(pairs, user.login, input_fn=guess, out=out, rng=random.Random(1), conn=conn) == 0
    report = out.getvalue()
    assert "Guessed 4, skipped 1." in report and "Detection rate: 100%" in report
    assert conn.execute("SELECT COUNT(*) FROM audit").fetchone()[0] == 0
    assert main(pairs, "nobody", out=io.StringIO(), conn=conn) == 2
