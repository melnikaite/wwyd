# SPDX-License-Identifier: AGPL-3.0-or-later
"""Output contract and host key filter (SPEC §7.2, §13)."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from analyzer_data import BERLIN

from wwyd.analyzers.contract import ContractError, validate_output
from wwyd.analyzers.keyfilter import NUMERIC, TEXT, VOCAB, KeyFilter, TextStats
from wwyd.ingest.episodes import Episode


def test_valid_output_normalised():
    out = validate_output({"gap": {"2026-09": {"5": 3, "10": 1.5}}})
    assert out == {"gap": {"2026-09": {"5": 3.0, "10": 1.5}}}


@pytest.mark.parametrize(
    ("output", "reason"),
    [
        ([], "shape"),
        ({"m": {"2026-09": {"k": float("nan")}}}, "value"),
        ({"m": {"2026-09": {"k": float("inf")}}}, "value"),
        ({"m": {"2026-09": {"k": True}}}, "value"),
        ({"m": {"2026-09": {"k": "1"}}}, "value"),
        ({"m": {"2026-13": {"k": 1}}}, "month"),
        ({"m": {"2026-9": {"k": 1}}}, "month"),
        ({"m": {"2026-09": {"x" * 65: 1}}}, "key_length"),
        ({"m": {"2026-09": {"": 1}}}, "key_length"),
        ({"Bad": {"2026-09": {"k": 1}}}, "metric_name"),
        ({f"m{i}": {} for i in range(21)}, "too_many_metrics"),
        ({"m": {"2026-09": {str(i): 1 for i in range(201)}}}, "too_many_keys"),
        ({"m": {"2026-09": [1]}}, "shape"),
    ],
)
def test_contract_violations(output, reason):
    with pytest.raises(ContractError) as exc:
        validate_output(output)
    assert exc.value.reason == reason
    assert exc.value.code == "invalid"


def test_contract_size_limit():
    big = {
        f"m{i}": {f"20{10 + j}-01": {"k" * 60 + str(k): 1 for k in range(200)} for j in range(1)}
        for i in range(20)
    }
    validate_output(big)  # ~300 KB
    huge = {
        f"m{i}": {f"20{10 + j}-01": {"k" * 60 + str(k): 1 for k in range(200)} for j in range(5)}
        for i in range(20)
    }
    with pytest.raises(ContractError) as exc:
        validate_output(huge)
    assert exc.value.reason == "too_large"


def test_numeric_and_vocab_classes():
    f = KeyFilter.build(audiences=["family", "aud_x1"], labels=["comedy"])
    classes = {"h": (VOCAB, NUMERIC), "l": (VOCAB,)}
    out, dropped = f.apply(
        {
            "h": {"2026-09": {"mon.08": 1, "family.08:30": 1, "monx.08": 1, "mon.8a": 1, "mon": 1}},
            "l": {"2026-09": {"comedy": 1, "aud_x1": 1, "drama": 1, "_": 2, "Velmorine": 1}},
        },
        classes,
    )
    assert out == {
        "h": {"2026-09": {"mon.08": 1, "family.08:30": 1}},
        "l": {"2026-09": {"comedy": 1, "aud_x1": 1, "_": 2}},
    }
    assert dropped == 5


def _said(i: int, day: int, text: str) -> Episode:
    start = datetime(2026, 9, day, 12, tzinfo=BERLIN)
    return Episode(
        index=i, category_id="c", category="x", audience=None, start=start,
        end=start + timedelta(minutes=1), said=text,
    )  # fmt: skip


def _batch() -> list[Episode]:
    eps = []
    texts = [
        "ok see you soon, tell Velmorine hi",
        "yes coffee later. kolya says hi",
        "coffee? zq7x-canary-41b9 ping me",
        "Velmorine and I want coffee",
        "sure coffee, mail me at a@b.co",
    ]
    for i in range(10):
        eps.append(_said(i, 1 + i % 5, texts[i % 5]))
    # "mira": lowercase, often, but all on one day; "tomas": 3 days but only 3 episodes
    eps += [_said(20 + i, 7, "mira mira coffee") for i in range(8)]
    eps += [_said(40 + i, 10 + i, "tomas called") for i in range(3)]
    return eps


def test_text_class_canaries():
    stats = TextStats(_batch(), ("said",))
    f = KeyFilter.build(text_stats=stats)
    words = ["coffee", "velmorine", "Velmorine", "zq7x-canary-41b9", "zq7x", "mira", "tomas",
             "kolya", "hi", "a@b.co", "McKay", "coffee hi"]  # fmt: skip
    out, dropped = f.apply({"w": {"2026-09": {w: 1 for w in words}}}, {"w": (TEXT,)})
    # "hi" is in only 4 episodes; "kolya" is lowercase but in 2 episodes; "mira" is on one
    # day; "tomas" in 3 episodes; Velmorine is capitalised mid-sentence; the canary has digits.
    assert set(out["w"]["2026-09"]) == {"coffee"}
    assert dropped == len(words) - 1


def test_text_fields_parameter():
    eps = [_said(i, 1 + i, "x") for i in range(6)]
    for ep in eps:
        ep.situation = "lunch plans"
    f_said = KeyFilter.build(text_stats=TextStats(eps, ("said",)))
    f_sit = KeyFilter.build(text_stats=TextStats(eps, ("situation",)))
    assert not f_said.key_passes("lunch", (TEXT,))
    assert f_sit.key_passes("lunch", (TEXT,))


def test_lowercase_name_residual_risk_documented():
    """A lowercase name used in >= 5 episodes on >= 3 days passes (SPEC §2 residual risk)."""
    eps = [_said(i, 1 + i % 4, "ask mira first") for i in range(6)]
    f = KeyFilter.build(text_stats=TextStats(eps))
    assert f.key_passes("mira", (TEXT,))


def test_text_without_stats_drops():
    assert not KeyFilter.build().key_passes("coffee", (TEXT,))


def test_segment_count_mismatch_drops():
    f = KeyFilter.build()
    assert not f.key_passes("mon.08.extra", (VOCAB, NUMERIC))
    assert f.key_passes("_", (VOCAB, NUMERIC))
