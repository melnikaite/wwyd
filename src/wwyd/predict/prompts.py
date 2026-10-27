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
        "when": {"type": "string"},
    },
    "required": ["categories", "when"],
    "additionalProperties": False,
}

ROUTE_SYSTEM = """You pick which parts of a person's profile are relevant to a situation.
You get a list of category paths with descriptions and a situation. Return the 1 to 3 most
relevant category paths, copied exactly from the list, most relevant first. Return an empty
list only if nothing fits. In "when" put the time the situation refers to, translated
into short English and copied as the situation says it, never worked out into a date:
"today", "tonight", "tomorrow 17:00", "day after tomorrow", "sat", "next thu 15:00",
"this weekend", "in 3 days", "2026-10-30"; a deadline or period stays one ("this week",
also for "by the end of the week"; "by fri"); "" when the situation names no time.

Example
Categories:
- communication/style: Tone, length, greetings, language and pace of messages.
- hobbies/beekeeping: How the person keeps bees and when they open the hives.
- hobbies/calligraphy: Which pens and scripts the person uses and when they write.
Situation: Can you check my hives with me tomorrow at 9?
Answer: {"categories": ["hobbies/beekeeping", "communication/style"], "when": "tomorrow 09:00"}

Example
Categories:
- communication/style: Tone, length, greetings, language and pace of messages.
- hobbies/model-trains: Which layouts the person builds and which meetups they go to.
- spending: How much the person spends on hobbies and what on.
Situation: Привет! Приедешь в субботу на слёт моделистов железных дорог? Взнос 15 евро.
Answer: {"categories": ["hobbies/model-trains", "spending"], "when": "sat"}
"""

# The time reference (SPEC §8.1 step 4): extracted and translated by the model, resolved
# by code (predict.when). Routing carries it as "when"; when routing is skipped this
# one-field call does. Its examples are set in unusual areas on purpose.
WHEN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"when": {"type": "string"}},
    "required": ["when"],
    "additionalProperties": False,
}

WHEN_SYSTEM = """You read a message and write down the time it refers to, in short English,
translated from any language and copied as the message says it, never worked out into a
date: "today", "tonight", "tomorrow 17:00", "day after tomorrow", "sat", "next thu 15:00",
"this weekend", "in 3 days", "2026-10-30". A weekday alone is just its name ("thu");
write "next" only when the message says it. "The day after tomorrow" is not "tomorrow". A
deadline or period stays one: "this week" (also for "by the end of the week"), "by fri";
write "weekend" only when the message names the weekend. Write "" when the message names no time.

Example
Situation: Можешь послезавтра в 9 утра покормить моих улиток?
Answer: {"when": "day after tomorrow 09:00"}

Example
Situation: Hast du am Samstagabend Lust auf Glockenläuten im Turm?
Answer: {"when": "sat evening"}

Example
Situation: Czy do końca tygodnia oprawisz mi zielnik z paprociami?
Answer: {"when": "this week"}

Example
Situation: Kannst du bis Freitag die Orgelpfeifen in der Kapelle stimmen?
Answer: {"when": "by fri"}

Example
Situation: Which glue works best for repairing a kite?
Answer: {"when": ""}
"""


def when_user(situation: str) -> str:
    return f"Situation: {situation}"


# Two calls (SPEC §8.1 steps 6-7). Some backends' JSON grammars emit keys in alphabetical
# order, so the internal field names are chosen to force the generation order: the scratch
# note first, then the applicable claims, the verdict, the deciding label and only then the
# conditions. None of these fields is returned, stored or logged; the public
# ``depends_on`` is taken from ANALYZE_DEPENDS for an "open" verdict.
ANALYZE_CHECK = "a_check"
ANALYZE_APPLICABLE = "b_applicable"
ANALYZE_EXCLUDED = "c_excluded_by"
ANALYZE_VERDICT = "d_verdict"
ANALYZE_LABEL = "e_label"
ANALYZE_DEPENDS = "f_depends_on"
ANALYZE_LANGUAGE = "g_language"
VERDICTS = ("none", "boundary", "ruled_out", "open", "settled")

ANALYZE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        ANALYZE_CHECK: {"type": "string"},
        ANALYZE_APPLICABLE: {"type": "array", "items": {"type": "string"}},
        ANALYZE_EXCLUDED: {"type": "string"},
        ANALYZE_VERDICT: {"type": "string", "enum": list(VERDICTS)},
        ANALYZE_LABEL: {"type": "string"},
        ANALYZE_DEPENDS: {"type": "string"},
        ANALYZE_LANGUAGE: {"type": "string"},
    },
    "required": [
        ANALYZE_CHECK,
        ANALYZE_APPLICABLE,
        ANALYZE_EXCLUDED,
        ANALYZE_VERDICT,
        ANALYZE_LABEL,
        ANALYZE_DEPENDS,
        ANALYZE_LANGUAGE,
    ],
    "additionalProperties": False,
}

# The system prompts are static and byte-identical across calls; everything that varies
# (current time, owner, audience, claims, context, situation, the server's instruction)
# goes in the user message after them, so the backend's prompt prefix cache covers the
# long part. Claims are labelled c1, c2, ... (short labels cost far fewer output tokens
# than claim IDs); the examples use other letters so a label copied from an example never
# matches a real claim. The examples are set in deliberately unusual areas (cacti,
# birdwatching, pottery, chess, gardening, boats, stargazing, archery, bell ringing,
# falconry): a small model copies an example when the input looks like it, so no example
# may resemble a common real input (calls, meals, trips, loans). Example 11 is a
# contrastive pair: one claim, one situation it limits and one it says nothing about.
ANALYZE_SYSTEM = """You decide which of a person's (the owner's) claims apply to a
situation and what they decide. Each claim has a label in square brackets. You cannot see
the owner's calendar, money, health, location, the weather, prices or anything else
outside the claims. The Now line is the owner's current weekday, date and time. A
When line, if there is one, is the day the situation refers to, already worked out: trust
it for the date, the weekday and whether it is a weekend (only a day marked (weekend) is
one), and never recount. It only dates the situation: whether a claim applies
depends on what the situation asks the owner to do, never on the day. Read
ambiguous references in their usual meaning for the activity ("pottery class at 6" is
18:00). The Context line is what the caller found out for this
situation; "Context: none" means nothing was checked.

Fill the fields in this order.

"a_check": one short line applying the claims to the situation and the Context line.
First name what the situation asks the owner to do, then compare it only with the claims
about that same thing. Compare the request with every such claim that limits it (a day, an hour, an amount, a
distance, a price, who comes ...: "22:30 is after 22:00", "60 EUR is more than 40 EUR").

"b_applicable": the labels of the claims about the topic of the situation. Style claims
(how the owner writes) never go here. A claim applies when the situation involves what
the claim is about, even if it does not name it (a claim about what the owner grows
applies to an invitation to swap seedlings). A claim about another activity does not
apply (a rule about chess evenings says nothing about a garden party, a wind rule for
birdwatching says nothing about a train trip). A claim may be a short label for the owner
("Early riser."): read it as what the owner does or avoids because of it and apply that
(an early riser's claim applies to an invitation to stargaze at 2 am).

"c_excluded_by": the label of an applicable claim that excludes the request (the day,
hour, amount, distance ... is outside what the claim allows, or the claim says the owner
does not do this), by the claim alone or together with the Context line, else "". Check
this before anything else: one excluding claim is enough, even if another claim would make the answer conditional.
When a boundary claim and another claim both exclude it, give the other claim. A claim
whose limit the request meets (the day, hour or amount is inside what it allows) does
not exclude it; the a_check line decides that.

"d_verdict", exactly one of:
- "none": no claim in "b_applicable". The owner's claims say nothing about this topic:
  opinions, advice, favours or plans no claim covers are "none". Never guess.
- "boundary": a boundary claim names a topic the owner does not let anyone answer for
  them, the situation is about that very topic, and any reply, even a refusal, would
  speak for the owner on it, and no applicable claim other than a boundary claim
  declines the request (if one does, the verdict is "ruled_out" and "c_excluded_by" is
  that claim). A boundary claim that is a rule for declining something
  ("never plays chess on Sundays") is not "boundary": it rules the request out.
- "ruled_out": "c_excluded_by" is not empty.
- "open": an applicable claim makes the answer conditional and neither the situation nor
  the Context line states whether the condition holds. Working a condition out from them
  is not stating it.
- "settled": the applicable claims, the situation or the Context line decide it.
Never add conditions of your own (free time, money, health, other plans): only the claims
make an answer conditional.

"e_label": for "boundary" or "ruled_out" the label of the deciding claim, else "".

"f_depends_on": only for "open", one or two plain sentences on the applicable claims'
conditions applied to this situation ("takes the cacti if there are at most five"), in
the language of the Situation line. Otherwise "".

"g_language": the ISO 639-1 code of the language the Situation line is written in
("en", "pl", "ja" ...), whatever the language of the claims.

Example 1
Claims:
[d1] (style, communication/style) Пишет коротко и дружелюбно, без приветствий.
[d2] (decision_rule, hobbies/plants) Берёт чужие кактусы на передержку, только если их не больше пяти.
Now: Wednesday 2026-03-11 12:10 (Europe/Moscow)
Situation: Возьмёшь мои кактусы, пока я в отпуске?
Context: none
Output: {"a_check": "Берёт не больше пяти кактусов; сколько их, не известно.",
"b_applicable": ["d2"], "c_excluded_by": "", "d_verdict": "open", "e_label": "", "f_depends_on": "Возьмёт,
если кактусов не больше пяти; иначе откажется.", "g_language": "ru"}

Example 2
Claims:
[e1] (style, communication/style) Short, lowercase, no greeting.
[e2] (preference, hobbies/birdwatching) Goes birdwatching only when the wind is under 20 km/h.
Now: Friday 2026-05-08 07:30 (America/Chicago)
Situation: Birding at the marsh tomorrow morning?
Context: Saturday 2026-05-09 morning: wind 35 km/h
Output: {"a_check": "Saturday morning; goes only under 20 km/h; the Context line says 35 km/h, more than 20.",
"b_applicable": ["e2"], "c_excluded_by": "e2", "d_verdict": "ruled_out", "e_label": "e2", "f_depends_on": "", "g_language": "en"}

Example 3
Claims:
[b1] (style, communication/style) Schreibt locker und kurz, duzt.
[b2] (boundary, hobbies/pottery) Töpfert sonntags nie, der Sonntag gehört der Familie.
[b3] (decision_rule, money) Gibt für einen Hobbykurs höchstens 40 Euro aus.
Now: Saturday 2026-06-13 10:05 (Europe/Berlin)
Situation: Kommst du morgen mit zum Töpferkurs? Kostet 25 Euro.
Context: none
Output: {"a_check": "Morgen ist Sonntag 14.06; b2 schließt Töpfern am Sonntag aus; 25 Euro liegt unter 40 Euro.",
"b_applicable": ["b2", "b3"], "c_excluded_by": "b2", "d_verdict": "ruled_out", "e_label": "b2", "f_depends_on": "", "g_language": "de"}

Example 4
Claims:
[r1] (style, communication/style) Пишет коротко, без приветствий.
[r2] (decision_rule, hobbies/chess) Не играет в шахматы после 22:00.
Now: Sunday 2026-04-12 19:00 (Europe/Riga)
Situation: Blitz at the chess club tonight at 20:00?
Context: none
Output: {"a_check": "Tonight 20:00; 20:00 is before 22:00, so r2 allows it.",
"b_applicable": ["r2"], "c_excluded_by": "", "d_verdict": "settled", "e_label": "", "f_depends_on": "", "g_language": "en"}

Example 5
Claims:
[s1] (boundary, family/inheritance) Never discusses inheritance matters in writing.
[s2] (style, communication/style) Short, no greeting.
Now: Tuesday 2026-09-01 20:00 (Europe/London)
Situation: So what are you going to do with your grandmother's cottage?
Context: none
Output: {"a_check": "Asks about the inherited cottage; s1 says inheritance is never discussed in writing.",
"b_applicable": ["s1"], "c_excluded_by": "", "d_verdict": "boundary", "e_label": "s1", "f_depends_on": "", "g_language": "en"}

Example 6
Claims:
[a1] (style, communication/style) Short, no greeting.
[a2] (preference, hobbies/chess) Plays correspondence chess every evening.
Now: Thursday 2026-02-19 16:45 (UTC)
Situation: Which fertiliser should I use for my tomatoes?
Context: none
Output: {"a_check": "Asks for gardening advice; only a1 (style) and a chess claim.",
"b_applicable": [], "c_excluded_by": "", "d_verdict": "none", "e_label": "", "f_depends_on": "", "g_language": "en"}

Example 7
Claims:
[g1] (style, communication/style) Пишет коротко и дружелюбно.
[g2] (preference, hobbies/plants) Разводит кактусы на балконе.
Now: Monday 2026-08-03 11:00 (Europe/Moscow)
Situation: Поможешь в субботу покрасить лодку?
Context: none
Output: {"a_check": "Просьба помочь покрасить лодку; про лодки и помощь ничего нет.",
"b_applicable": [], "c_excluded_by": "", "d_verdict": "none", "e_label": "", "f_depends_on": "", "g_language": "ru"}

Example 8
Claims:
[h1] (style, communication/style) Пишет коротко.
[h2] (preference, hobbies/music) Терпеть не может громкую музыку.
[h3] (decision_rule, money) Тратит на билеты не больше 30 евро.
Now: Thursday 2026-07-02 18:00 (Europe/Vilnius)
Situation: Пойдём в субботу на рок-фестиваль? Билет 25 евро.
Context: none
Output: {"a_check": "Рок-фестиваль — это громкая музыка, h2 про неё; 25 евро не больше 30.",
"b_applicable": ["h2", "h3"], "c_excluded_by": "h2", "d_verdict": "ruled_out", "e_label":
"h2", "f_depends_on": "", "g_language": "ru"}

Example 9
Claims:
[k1] (style, communication/style) Пишет коротко.
[k2] (preference, lifestyle) Жаворонок.
Now: Tuesday 2026-08-11 15:00 (Europe/Riga)
Situation: Поедешь в пятницу за город смотреть на звёзды? Выезжаем в полночь.
Context: none
Output: {"a_check": "Жаворонок рано ложится и рано встаёт; выезд в полночь слишком поздно.",
"b_applicable": ["k2"], "c_excluded_by": "k2", "d_verdict": "ruled_out", "e_label": "k2", "f_depends_on": "", "g_language": "ru"}

Example 10
Claims:
[m1] (style, communication/style) Short, no greeting.
[m2] (decision_rule, hobbies/archery) Shoots archery only on weekdays after 18:00.
Now: Monday 2026-11-02 09:00 (Europe/Dublin)
Situation: Bell ringing practice at the tower on Wednesday at 19:00?
Context: none
Output: {"a_check": "Bell ringing, not archery; m2 limits only archery, so Wednesday 19:00 means nothing here.",
"b_applicable": [], "c_excluded_by": "", "d_verdict": "none", "e_label": "", "f_depends_on": "", "g_language": "en"}

Example 11 (the same claims, two situations)
Claims:
[q1] (style, communication/style) Schreibt kurz, duzt.
[q2] (decision_rule, hobbies/falconry) Lässt den Falken nur vormittags fliegen, nie nach 12 Uhr.
Now: Thursday 2026-10-01 18:00 (Europe/Zurich)
Situation: Samstag um 15 Uhr Falkenflug auf der Wiese?
Context: none
Output: {"a_check": "Falkenflug am Samstag um 15 Uhr; q2 erlaubt ihn nur vor 12 Uhr, 15 Uhr ist danach.",
"b_applicable": ["q2"], "c_excluded_by": "q2", "d_verdict": "ruled_out", "e_label": "q2", "f_depends_on": "", "g_language": "de"}
Situation: Samstag um 15 Uhr Puppenspiel-Werkstatt?
Context: none
Output: {"a_check": "Puppenspiel, kein Falkenflug; q2 begrenzt nur den Falkenflug.",
"b_applicable": [], "c_excluded_by": "", "d_verdict": "none", "e_label": "", "f_depends_on": "", "g_language": "de"}
"""

# The answer call writes the message once the analysis decided what it must do. Its keys
# also sort into generation order: "answer" first, "z_commits" (whether the answer
# accepts, agrees, promises or declines) last.
COMMITS_KEY = "z_commits"

# The answer examples vary case with their style claims (only the lowercase-style example
# answers in lowercase, the formal ones use «Вы»/"Sie" and capitalised nouns): with
# lowercase-only examples the model lowercased every persona. Example 1 is an open
# condition about the owner, answered by the owner's own check rather than a question.
# Examples 3 and 4 carry the analyze call's conclusion (an "Analysis:" line, internal) so a
# settled answer follows it; example 4 turns a third-person claim into a first-person
# answer in another language, with normal case under an informal style.

ANSWER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "answer": {"type": "string"},
        "language": {"type": "string"},
        COMMITS_KEY: {"type": "boolean"},
    },
    "required": ["answer", "language", COMMITS_KEY],
    "additionalProperties": False,
}

ANSWER_SYSTEM = """You write the message a specific person (the owner) would send in a
situation, in the owner's voice. You know the owner through the claims listed in the
input; each claim has a label in square brackets. Style claims and style parameters say
how the owner writes: follow them closely (tone, length, greetings, case, punctuation).
When there is a Case line, follow it for how sentences start; otherwise use the normal
capitalisation of the answer's language (capitalised nouns where the language has them)
and write in lowercase only when a style claim says so.
The answer is the owner speaking in the first person, in any language. Claims describe
the owner in the third person: never copy their wording or their verb forms; change every
verb about the owner to the owner's own "I" form of the answer's language.
You cannot see the owner's calendar, money, health, location or anything else outside
the claims, so never state such facts ("I'm free on Sunday"): never state that the owner
is free or able to do something. An alternative may only be suggested or asked about,
never promised or stated as available. A When line, if there is one, is the day the
situation refers to, already worked out: take its date, weekday and whether it is a
weekend from it (only a day marked (weekend) is one), never recount, and never give a reason it contradicts.
The Instruction line says
what the message must do; follow it exactly. When the answer depends on a condition
about the owner (the owner's own plans, time, use or state), the owner says they will
check it or that it decides the answer; never ask the other person about it. Ask the
other person only about what they can know (how many, which, where, what they did).
Write in the language of the Answer
language line (when there is none, of the Situation line), whatever the language of the
claims, unless a claim says which language the owner uses.

Fill:
- "answer": what the owner would send, ready to send as is. For kind "reply" the actual
  message text, for kind "decision" a short statement of what the owner decides.
- "language": the ISO 639-1 code of the answer's language.
- "z_commits": true when "answer" accepts, agrees, promises or declines; false when it
  stays non-committal.

Example 1
Claims:
[d1] (style, communication/style) Пишет вежливо, на «Вы», полными предложениями.
[d2] (decision_rule, hobbies/candles) Делает свечи на заказ, только если на этой неделе нет своей ярмарки.
Kind: reply
Now: Wednesday 2026-03-11 12:10 (Europe/Moscow)
Answer language: ru
Situation: Сделаете к пятнице десять свечей для нашей лавки?
Context: none
Depends on: Сделает, если на этой неделе нет своей ярмарки.
Instruction: The answer depends on the condition above, which nobody has checked. Do not accept, agree, promise or decline; reply non-committally in the owner's voice. A condition about the owner is something the owner will check; ask the other person only about what they can know.
Output: {"answer": "Это зависит от того, будет ли у меня на этой неделе своя ярмарка. Я проверю и напишу Вам завтра.", "language": "ru",
"z_commits": false}

Example 2
Claims:
[b1] (style, communication/style) Schreibt förmlich und höflich, siezt immer.
[b2] (boundary, hobbies/lockpicking) Öffnet nie Schlösser, die anderen gehören, nur die eigenen Übungsschlösser.
Kind: reply
Now: Saturday 2026-06-13 10:05 (Europe/Berlin)
Answer language: de
Situation: Könnten Sie mir helfen, das Vorhängeschloss am Keller meines Nachbarn zu öffnen?
Context: none
Instruction: Claim b2 rules the request out. Decline clearly in the owner's voice, without making it conditional; you may suggest or ask about what the claims allow instead, but never state that the owner is free or able to do it.
Output: {"answer": "Das mache ich leider nicht: Fremde Schlösser öffne ich grundsätzlich nicht, nur meine eigenen Übungsschlösser. Vielleicht fragen Sie einen Schlüsseldienst?", "language": "de",
"z_commits": true}

Example 3
Claims:
[e1] (style, communication/style) Always writes in lowercase, short, no greeting.
[e2] (preference, hobbies/juggling) Goes to the juggling meetup in the park every Saturday at 11:00.
Kind: reply
Now: Thursday 2026-04-09 19:00 (Europe/Riga)
Answer language: en
Situation: Juggling meetup in the park this Saturday at 11?
When: Saturday 2026-04-11 (weekend)
Context: none
Analysis: The meetup is on Saturday at 11, which is exactly when the owner goes to it.
Instruction: The claims, the situation and the Context line settle it, as the Analysis line concludes. Answer as that analysis concludes.
Output: {"answer": "yes, i'll be there at 11", "language": "en", "z_commits": true}

Example 4
Claims:
[g1] (style, communication/style) Пише неформально, коротко, на «ти».
[g2] (preference, hobbies/clock_repair) Охоче лагодить старі механічні годинники друзям, але не більше одного за вечір.
Kind: reply
Now: Tuesday 2026-02-17 18:30 (Europe/Kyiv)
Answer language: uk
Situation: Глянеш сьогодні ввечері мій дідів годинник? Зупинився.
Context: none
Analysis: One old clock from a friend this evening is within one per evening, so the owner gladly does it.
Instruction: The claims, the situation and the Context line settle it, as the Analysis line concludes. Answer as that analysis concludes.
Output: {"answer": "Так, приноси, охоче гляну. Старі механічні годинники я люблю лагодити.", "language": "uk",
"z_commits": true}
"""

INSTRUCTIONS = {
    "open": (
        "Instruction: The answer depends on the condition above, which nobody has checked. "
        "Do not accept, agree, promise or decline; reply non-committally in the owner's "
        "voice. A condition about the owner is something the owner will check; ask the "
        "other person only about what they can know."
    ),
    "ruled_out": (
        "Instruction: Claim {label} rules the request out. Decline clearly in the owner's "
        "voice, without making it conditional; you may suggest or ask about what the claims "
        "allow instead, but never state that the owner is free or able to do it."
    ),
    "settled": (
        "Instruction: The claims, the situation and the Context line settle it, as the "
        "Analysis line concludes. Answer as that analysis concludes."
    ),
}

# Appended to the answer call's user message for its single re-ask (step 7).
COMMIT_RETRY_NOTE = (
    "\n\nNote: your previous answer accepted, agreed, promised or declined. Follow the "
    "Instruction line: stay non-committal."
)
RULED_OUT_RETRY_NOTE = (
    "\n\nNote: your previous answer did not decline. Follow the Instruction line: decline "
    "clearly, without making it conditional."
)
LANGUAGE_RETRY_NOTE = (
    "\n\nNote: your previous answer was not in the language of the Answer language line. "
    "Write it in that language unless a claim says which language the owner uses."
)


def claim_line(label: str, claim: Any) -> str:
    return f"[{label}] ({claim.kind}, {claim.category}) {claim.text}"


def route_user(categories: list[tuple[str, str]], situation: str) -> str:
    lines = "\n".join(f"- {path}: {desc}" if desc else f"- {path}" for path, desc in categories)
    return f"Categories:\n{lines}\nSituation: {situation}"


def _context(context: str | None) -> str:
    return (context or "").strip() or "none"


# Appended to the analyze call's user message only when the caller passed a context, so
# calls without one stay byte-identical (changing ANALYZE_SYSTEM itself shifted unrelated
# no-context scenarios). Static: the same text for every call with a context.
CONTEXT_NOTE = (
    "Note: The owner goes by the Context line even when no claim names it. When it says "
    "the request cannot happen as asked (that time is taken, the thing is gone ...), the "
    'owner declines, and suggests what it says is possible instead ("the kiln is booked '
    'until 16:00" for a 14:00 firing: not at 14:00, suggest 16:00). A time the Context '
    "line calls taken is never accepted. When the request itself names a span of two "
    "times and the Context line calls only a part of that span free, the request is "
    'possible as asked: the owner accepts that part ("only 15:00-16:00 is '
    'free", the rest is taken, for a firing between 1 and 5 pm, that is 13:00-17:00: '
    "yes, at 15:00-16:00). No claim excludes "
    'it then: "c_excluded_by" stays "", the verdict is "settled", and "a_check" '
    "ends with that decision and its reason from the Context line."
)


def analyze_user(
    *, claims: list[str], context: str | None, situation: str, now: str, when: str = ""
) -> str:
    claim_block = "\n".join(claims) if claims else "(none)"
    when_part = f"When: {when}\n" if when else ""
    note = f"\n{CONTEXT_NOTE}" if (context or "").strip() else ""
    return (
        f"Claims:\n{claim_block}\nNow: {now}\n{when_part}"
        f"Situation: {situation}\nContext: {_context(context)}{note}"
    )


def answer_user(
    *,
    owner: str,
    audience: str,
    audience_description: str,
    claims: list[str],
    context: str | None,
    kind: str,
    situation: str,
    now: str,
    instruction: str,
    when: str = "",
    depends_on: str | None = None,
    analysis: str | None = None,
    case: str | None = None,
    style: str | None = None,
    language: str | None = None,
) -> str:
    claim_block = "\n".join(claims) if claims else "(none)"
    audience_line = f"{audience} ({audience_description})" if audience_description else audience
    return (
        f"Owner: {owner}\n"
        f"Talking to: {audience_line}\n"
        f"Claims:\n{claim_block}\n"
        f"Kind: {kind}\n"
        + (f"{style}\n" if style else "")
        + f"Now: {now}\n"
        + (f"{language}\n" if language else "")
        + f"Situation: {situation}\n"
        + (f"When: {when}\n" if when else "")
        + f"Context: {_context(context)}\n"
        + (f"Depends on: {depends_on}\n" if depends_on else "")
        + (f"{case}\n" if case else "")
        + (f"Analysis: {analysis}\n" if analysis else "")
        + instruction
    )
