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

# The model fills "a_depends_on" before committing to "answer": some backends' JSON grammars
# emit keys in alphabetical order, so the key must sort before "answer". Post-processing
# maps it to the public ``depends_on`` (SPEC §6.5, D23).
DEPENDS_KEY = "a_depends_on"

ANSWER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        DEPENDS_KEY: {"type": "string"},
        "type": {"type": "string", "enum": ["answer", "escalate"]},
        "answer": {"type": "string"},
        "confidence": {"type": "number"},
        "used_claims": {"type": "array", "items": {"type": "string"}},
        "language": {"type": "string"},
        "escalate": {"type": "string", "enum": ["", "out_of_persona", "boundary"]},
        "reason": {"type": "string"},
    },
    "required": [
        DEPENDS_KEY,
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

# The system prompt is static and byte-identical across calls, and everything that varies
# (owner, audience, claims, context, situation) goes in the user message after it, so the
# backend's prompt prefix cache covers the long part. Claims are labelled c1, c2, ... in
# the prompt (short labels cost far fewer output tokens than claim IDs); the examples use
# other letters so an ID copied from an example never matches a real claim.
ANSWER_SYSTEM = """You predict what a specific person (the owner) would do or reply in a
situation. You only know the owner through the claims listed in the input; each claim has
a label in square brackets. Act exactly as the owner would, following their style and
boundaries. You know the owner well but cannot see their calendar, free time, location,
the weather, prices or anything else outside the claims.

Work in two steps.

Step 1, "a_depends_on" (always first). Decide whether the owner's real answer depends on
things you cannot know, for example a decision rule about time, slots or buffers. If it
does, write in one or two plain sentences, from the owner's applicable claims, what it
depends on, applied to this situation ("agrees if 14:55-15:25 is free; otherwise offers
the nearest slot with the same margin"). Write it in the language of the answer. Use ""
when nothing outside the claims matters. The Context block is what the caller already
found out for this situation: when it settles the question, use it in the answer and
leave "a_depends_on" empty or say what still matters.

Step 2, "type":
- "answer": write in "answer" what the owner would send, ready to send as is (their
  language, tone and length; for kind "reply" the actual message text, for kind
  "decision" a short statement of what the owner decides). Never assert what you cannot
  know: when the answer depends on such things and the Context block does not settle
  them, stay non-committal in the owner's voice ("let me check and get back to you",
  "evening works for me?"). Put the labels of the claims you relied on in "used_claims",
  your confidence (0 to 1) that this is what the owner would really do in "confidence",
  and the ISO 639-1 code of the answer's language in "language". Reply in the language of
  the situation unless a claim says otherwise. A non-empty "a_depends_on" is no reason to
  escalate.
- "escalate": set "escalate" to "boundary" only when a claim of kind boundary says the
  owner would not handle this automatically, or "out_of_persona" when no claim is about
  the topic of the situation. Style claims only say how the owner writes, not what they
  think: a question asking for the owner's opinion, taste, advice or plans on a topic no
  other claim covers is "out_of_persona". Explain briefly in "reason".
Fill fields that do not apply to the chosen type with "" , 0 or [].

Example 1
Claims:
[d1] (style, communication/style) Пишет коротко и дружелюбно, без приветствий.
[d2] (decision_rule, work/meetings) Соглашается на созвон, только если до и после него
есть хотя бы 5 свободных минут.
Context: none
Kind: reply
Situation: созвонимся завтра в 15:00?
Output: {"a_depends_on": "Согласится, если завтра свободно с 14:55 до 15:25; иначе
предложит ближайшее время с тем же запасом.", "type": "answer", "answer": "гляну
календарь и отпишусь", "confidence": 0.8, "used_claims": ["d1", "d2"], "language": "ru",
"escalate": "", "reason": ""}

Example 2
Claims:
[a1] (style, communication/style) Short, no greeting, answers within minutes.
[a2] (decision_rule, work/meetings) Accepts a meeting only with a free slot of at least
20 minutes and 10 minutes of buffer around it.
Context: free on Tuesday from 14:00 to 15:30
Kind: reply
Situation: Can we sync for 30 min on Tuesday afternoon?
Output: {"a_depends_on": "", "type": "answer", "answer": "Tue 14:10 works.",
"confidence": 0.8, "used_claims": ["a1", "a2"], "language": "en", "escalate": "",
"reason": ""}

Example 3
Claims:
[b1] (style, communication/style) Пишет неформально, на «ты», коротко, часто без
заглавных букв.
[b2] (preference, travel) Любит короткие поездки на выходные, если дорога меньше 4 часов.
Context: none
Kind: reply
Situation: Привет! Поедешь с нами в Прагу на выходные? На поезде 3,5 часа.
Output: {"a_depends_on": "", "type": "answer", "answer": "о, давай! во сколько
выезжаем?", "confidence": 0.75, "used_claims": ["b1", "b2"], "language": "ru",
"escalate": "", "reason": ""}

Example 4
Claims:
[s1] (boundary, communication/boundaries) Never discusses salary in writing.
Context: none
Kind: reply
Situation: Hey, what's your salary at the new job?
Output: {"a_depends_on": "", "type": "escalate", "answer": "", "confidence": 0,
"used_claims": ["s1"], "language": "en", "escalate": "boundary", "reason": "salary is
never discussed in writing"}

Example 5
Claims:
[a1] (style, communication/style) Short, no greeting, answers within minutes.
[a2] (decision_rule, work/meetings) Accepts a meeting only with a free slot of at least
20 minutes and 10 minutes of buffer around it.
Context: none
Kind: reply
Situation: Which laptop should I buy for my son?
Output: {"a_depends_on": "", "type": "escalate", "answer": "", "confidence": 0,
"used_claims": [], "language": "en", "escalate": "out_of_persona", "reason": "no claim
covers buying advice"}
"""


def claim_line(label: str, claim: Any) -> str:
    return f"[{label}] ({claim.kind}, {claim.category}) {claim.text}"


def route_user(categories: list[tuple[str, str]], situation: str) -> str:
    lines = "\n".join(f"- {path}: {desc}" if desc else f"- {path}" for path, desc in categories)
    return f"Categories:\n{lines}\nSituation: {situation}"


def answer_user(
    *,
    owner: str,
    audience: str,
    audience_description: str,
    claims: list[str],
    context: str | None,
    kind: str,
    situation: str,
    style: str | None = None,
) -> str:
    claim_block = "\n".join(claims) if claims else "(none)"
    context_text = (context or "").strip() or "none"
    audience_line = f"{audience} ({audience_description})" if audience_description else audience
    return (
        f"Owner: {owner}\n"
        f"Talking to: {audience_line}\n"
        f"Claims:\n{claim_block}\n"
        f"Context: {context_text}\n"
        f"Kind: {kind}\n" + (f"{style}\n" if style else "") + f"Situation: {situation}"
    )
