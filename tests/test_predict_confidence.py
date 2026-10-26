# SPDX-License-Identifier: AGPL-3.0-or-later
"""The code-decided confidence of predict (SPEC §8.1 step 7)."""

from __future__ import annotations

import pytest

from wwyd.predict.answer import Analysis, score


@pytest.mark.parametrize(
    ("found", "kw", "expected"),
    [
        (Analysis("settled", ["c2"]), {}, 0.75),  # default claim confidence 0.5
        (Analysis("settled", ["c2", "c3"]), {}, 0.8),  # two claims agree
        (Analysis("open", ["c2"]), {}, 0.7),
        (Analysis("settled", ["c2"]), {"reasked": True}, 0.65),
        (Analysis("ruled_out", ["c2", "c4"], "c4"), {}, 0.55),  # the weak claim decides
        (Analysis("settled", ["c4"]), {"reasked": True}, 0.45),
        (Analysis("settled", ["c5"]), {}, 1.0),  # strong evidence, capped
    ],
)
def test_score(found, kw, expected):
    weights = {"c2": 0.5, "c3": 0.4, "c4": 0.1, "c5": 1.6}
    args = {"reasked": False, **kw}
    assert score(found, weights, **args) == expected
