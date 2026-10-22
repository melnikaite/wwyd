# SPDX-License-Identifier: AGPL-3.0-or-later
"""Claim text is written as behaviour, not as an identity label (SPEC §7.6)."""

from wwyd.analyzers.authoring import AUTHOR_SYSTEM
from wwyd.ingest.passes import DECISIONS_SYSTEM, REPLIES_SYSTEM, STATEMENTS_SYSTEM


def test_behaviour_not_label_rule_in_prompts() -> None:
    for prompt in (REPLIES_SYSTEM, STATEMENTS_SYSTEM, DECISIONS_SYSTEM):
        assert "not a label or identity" in prompt
        assert '"Teetotaler."' in prompt
    assert "not a label or identity" in AUTHOR_SYSTEM
