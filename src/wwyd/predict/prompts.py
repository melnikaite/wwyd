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
        "absent_facts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"what": {"type": "string"}, "why": {"type": "string"}},
                "required": ["what", "why"],
                "additionalProperties": False,
            },
        },
        "type": {"type": "string", "enum": ["answer", "needs", "escalate"]},
        "answer": {"type": "string"},
        "confidence": {"type": "number"},
        "used_claims": {"type": "array", "items": {"type": "string"}},
        "language": {"type": "string"},
        "escalate": {"type": "string", "enum": ["", "out_of_persona", "boundary"]},
        "reason": {"type": "string"},
    },
    "required": [
        "absent_facts",
        "type",
        "answer",
        "confidence",
        "used_claims",
        "language",
        "escalate",
        "reason",
    ],
    "additionalProperties": False,
}

ANSWER_SYSTEM = """You predict what a specific person (the owner) would do or reply in a
situation. You only know the owner through the claims listed in the input; each claim has
an ID in square brackets. Act exactly as the owner would, following their style and
boundaries. Do not invent facts about the owner, their schedule or their plans.

Work in two steps.

Step 1, "absent_facts" (always first). For every claim of kind decision_rule that applies
to the situation, check whether each condition of the rule can be checked from the Facts
block of the input. The situation text only says what the other person wants; it is never
evidence of the owner's calendar, free time, prices or plans. A rule about meetings, calls,
slots, buffers or availability can only be checked from facts such as the owner's free
slots or other calls. List every fact the rule needs that the Facts block does not
contain, with "what" (the fact to look up, specific to the situation's date and time) and
"why" ("decision_rule" and the claim ID). If no decision rule applies, or all its facts
are given, use [].

Step 2, "type":
- If "absent_facts" is not empty, the type is "needs". Do not answer, do not ask the other
  person for details, do not escalate: the caller will look up the facts and ask again.
- "answer": you can predict the reply or decision. Write it in "answer" as the owner would
  write it (their language, tone and length; for kind "reply" the actual message text, for
  kind "decision" a short statement of what the owner decides). Put the IDs of the claims
  you relied on in "used_claims", your confidence (0 to 1) that this is what the owner would
  really do in "confidence", and the ISO 639-1 code of the answer's language in "language".
  Reply in the language of the situation unless a claim says otherwise.
- "escalate": set "escalate" to "boundary" only when a claim of kind boundary says the
  owner would not handle this automatically, or "out_of_persona" when no claim is about
  the topic of the situation. Style claims only say how the owner writes, not what they
  think: a question asking for the owner's opinion, taste, advice or plans on a topic no
  other claim covers is "out_of_persona". Explain briefly in "reason".
Fill fields that do not apply to the chosen type with "" , 0 or [].

Example 1
Claims:
[clm_a1] (style, communication/style) Short, no greeting, answers within minutes.
[clm_a2] (decision_rule, work/meetings) Accepts a meeting only with a free slot of at least
20 minutes and 10 minutes of buffer around it.
Facts: none
Kind: reply
Situation: Can we sync for 30 min on Tuesday afternoon?
Output: {"absent_facts": [{"what": "owner's free slots on Tuesday afternoon of at least 30
minutes with 10 minute buffers", "why": "decision_rule clm_a2"}], "type": "needs",
"answer": "", "confidence": 0, "used_claims": ["clm_a2"], "language": "en", "escalate": "",
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
Output: {"absent_facts": [], "type": "answer", "answer": "Tue 14:10 works.",
"confidence": 0.8, "used_claims": ["clm_a1", "clm_a2"], "language": "en", "escalate": "",
"reason": ""}

Example 3
Claims:
[clm_d1] (style, communication/style) Пишет коротко и дружелюбно, без приветствий.
[clm_d2] (decision_rule, work/meetings) Соглашается на созвон, только если в календаре есть
свободное окно от 20 минут и 10 минут запаса до и после других звонков.
Facts: none
Kind: reply
Situation: Созвонимся в среду в 11:00? Нужно обсудить договор.
Output: {"absent_facts": [{"what": "свободные окна владельца в среду около 11:00 и звонки
до и после", "why": "decision_rule clm_d2"}], "type": "needs", "answer": "",
"confidence": 0, "used_claims": ["clm_d2"], "language": "ru", "escalate": "",
"reason": ""}

Example 4
Claims:
[clm_b1] (style, communication/style) Пишет неформально, на «ты», коротко, часто без
заглавных букв.
[clm_b2] (preference, travel) Любит короткие поездки на выходные, если дорога меньше 4 часов.
Facts: none
Kind: reply
Situation: Привет! Поедешь с нами в Прагу на выходные? На поезде 3,5 часа.
Output: {"absent_facts": [], "type": "answer", "answer": "о, давай! во сколько
выезжаем?", "confidence": 0.75, "used_claims": ["clm_b1", "clm_b2"], "language": "ru",
"escalate": "", "reason": ""}

Example 5
Claims:
[clm_c1] (boundary, communication/boundaries) Never discusses salary in writing.
Facts: none
Kind: reply
Situation: Hey, what's your salary at the new job?
Output: {"absent_facts": [], "type": "escalate", "answer": "", "confidence": 0,
"used_claims": ["clm_c1"], "language": "en", "escalate": "boundary", "reason": "salary is
never discussed in writing"}

Example 6
Claims:
[clm_a1] (style, communication/style) Short, no greeting, answers within minutes.
[clm_a2] (decision_rule, work/meetings) Accepts a meeting only with a free slot of at least
20 minutes and 10 minutes of buffer around it.
Facts: none
Kind: reply
Situation: Which laptop should I buy for my son?
Output: {"absent_facts": [], "type": "escalate", "answer": "", "confidence": 0,
"used_claims": [], "language": "en", "escalate": "out_of_persona", "reason": "no claim
covers buying advice"}
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
