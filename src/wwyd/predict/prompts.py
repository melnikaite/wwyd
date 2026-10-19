# SPDX-License-Identifier: AGPL-3.0-or-later
"""Prompts and JSON shapes for predict (SPEC §8, §12).

Every field the model emits is required, so small models fill unused fields with empty
values instead of inventing shapes. The worked examples cover several languages on purpose:
rules leak language through examples, and an English-only set makes the model answer
Russian or German messages in English.
"""

from __future__ import annotations

from typing import Any

ROUTE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "categories": {"type": "array", "items": {"type": "string"}, "maxItems": 3},
    },
    "required": ["categories"],
    "additionalProperties": False,
}

ROUTE_SYSTEM = """You pick which parts of a person's profile are relevant to a situation.
You get a list of category paths with descriptions and a situation. Return the 1 to 3 most
relevant category paths, copied exactly from the list, most relevant first. Return an empty
list only if nothing fits.

Example
Categories:
- communication/style: Tone, length, greetings, language and pace of messages.
- work/meetings: Which meetings are accepted, how long they run, buffers between them.
- food: What and how the person likes to eat and drink.
Situation: Can we do a quick call tomorrow at 9?
Answer: {"categories": ["work/meetings", "communication/style"]}

Example
Categories:
- communication/style: Tone, length, greetings, language and pace of messages.
- travel: How the person travels and plans trips.
- money: Spending, saving and money matters.
Situation: Привет! Поедешь с нами в Прагу на выходные? Билеты по 120 евро.
Answer: {"categories": ["travel", "money"]}
"""

ANSWER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "type": {"type": "string", "enum": ["answer", "needs", "escalate"]},
        "answer": {"type": "string"},
        "confidence": {"type": "number"},
        "used_claims": {"type": "array", "items": {"type": "string"}},
        "language": {"type": "string"},
        "needs": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"what": {"type": "string"}, "why": {"type": "string"}},
                "required": ["what", "why"],
                "additionalProperties": False,
            },
        },
        "escalate": {"type": "string", "enum": ["", "out_of_persona", "boundary"]},
        "reason": {"type": "string"},
    },
    "required": [
        "type",
        "answer",
        "confidence",
        "used_claims",
        "language",
        "needs",
        "escalate",
        "reason",
    ],
    "additionalProperties": False,
}

ANSWER_SYSTEM = """You predict what a specific person (the owner) would do or reply in a
situation. You only know the owner through the claims listed in the input; each claim has
an ID in square brackets. Act exactly as the owner would, following their style and
boundaries. Do not invent facts about the owner, their schedule or their plans.

Choose one type:
- "answer": you can predict the reply or decision. Write it in "answer" as the owner would
  write it (their language, tone and length; for kind "reply" the actual message text, for
  kind "decision" a short statement of what the owner decides). Put the IDs of the claims
  you relied on in "used_claims", your confidence (0 to 1) that this is what the owner would
  really do in "confidence", and the ISO 639-1 code of the answer's language in "language".
  Reply in the language of the situation unless a claim says otherwise.
- "needs": a decision rule depends on facts you were not given (free time slots, prices,
  dates). List each missing fact in "needs" with "what" (the fact to look up) and "why"
  (which claim needs it, e.g. "decision_rule clm_..."). Do not guess such facts. Missing
  facts are never a reason to escalate: ask for them with "needs".
- "escalate": set "escalate" to "boundary" only when a claim of kind boundary says the owner would not
  handle this automatically, or "out_of_persona" when the claims say nothing useful about
  this situation. Explain briefly in "reason".
Fill fields that do not apply to the chosen type with "" , 0 or [].

Example 1
Claims:
[clm_a1] (style, communication/style) Short, no greeting, answers within minutes.
[clm_a2] (decision_rule, work/meetings) Accepts a meeting only with a free slot of at least
20 minutes and 10 minutes of buffer around it.
Facts: none
Kind: reply
Situation: Can we sync for 30 min on Tuesday afternoon?
Output: {"type": "needs", "answer": "", "confidence": 0, "used_claims": ["clm_a2"],
"language": "en", "needs": [{"what": "free slots on Tuesday afternoon of at least 30
minutes with 10 minute buffers", "why": "decision_rule clm_a2"}], "escalate": "",
"reason": ""}

Example 2
Claims:
[clm_a1] (style, communication/style) Short, no greeting, answers within minutes.
[clm_a2] (decision_rule, work/meetings) Accepts a meeting only with a free slot of at least
20 minutes and 10 minutes of buffer around it.
Facts:
- free slots on Tuesday afternoon: 14:00-15:30
Kind: reply
Situation: Can we sync for 30 min on Tuesday afternoon?
Output: {"type": "answer", "answer": "Tue 14:10 works.", "confidence": 0.8,
"used_claims": ["clm_a1", "clm_a2"], "language": "en", "needs": [], "escalate": "",
"reason": ""}

Example 3
Claims:
[clm_b1] (style, communication/style) Пишет неформально, на «ты», коротко, часто без
заглавных букв.
[clm_b2] (preference, travel) Любит короткие поездки на выходные, если дорога меньше 4 часов.
Facts: none
Kind: reply
Situation: Привет! Поедешь с нами в Прагу на выходные? На поезде 3,5 часа.
Output: {"type": "answer", "answer": "о, давай! во сколько выезжаем?", "confidence": 0.75,
"used_claims": ["clm_b1", "clm_b2"], "language": "ru", "needs": [], "escalate": "",
"reason": ""}

Example 4
Claims:
[clm_c1] (boundary, communication/boundaries) Never discusses salary in writing.
Facts: none
Kind: reply
Situation: Hey, what's your salary at the new job?
Output: {"type": "escalate", "answer": "", "confidence": 0, "used_claims": ["clm_c1"],
"language": "en", "needs": [], "escalate": "boundary", "reason": "salary is never
discussed in writing"}
"""


def route_user(categories: list[tuple[str, str]], situation: str) -> str:
    lines = "\n".join(f"- {path}: {desc}" if desc else f"- {path}" for path, desc in categories)
    return f"Categories:\n{lines}\nSituation: {situation}"


def answer_user(
    *,
    owner: str,
    audience: str,
    audience_description: str,
    claims: list[str],
    facts: list[dict[str, str]],
    kind: str,
    situation: str,
) -> str:
    claim_block = "\n".join(claims) if claims else "(none)"
    fact_block = (
        "\n" + "\n".join(f"- {f['what']}: {f['value']}" for f in facts) if facts else " none"
    )
    audience_line = f"{audience} ({audience_description})" if audience_description else audience
    return (
        f"Owner: {owner}\n"
        f"Talking to: {audience_line}\n"
        f"Claims:\n{claim_block}\n"
        f"Facts:{fact_block}\n"
        f"Kind: {kind}\n"
        f"Situation: {situation}"
    )
