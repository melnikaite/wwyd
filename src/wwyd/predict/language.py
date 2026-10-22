# SPDX-License-Identifier: AGPL-3.0-or-later
"""A small deterministic language detector (SPEC §8.1 steps 6-7).

It covers the languages the disclosure templates cover (en, ru, de, uk, es, fr) and uses
only the script, a few letters typical of one language and common function words. It
returns ``None`` whenever it is not sure (short text, ties, other languages): callers then
fall back to the model's own judgement.
"""

from __future__ import annotations

import re

LANGUAGES = ("en", "ru", "de", "uk", "es", "fr")

NAMES = {
    "en": "English",
    "ru": "Russian",
    "de": "German",
    "uk": "Ukrainian",
    "es": "Spanish",
    "fr": "French",
}


def _set(words: str) -> frozenset[str]:
    return frozenset(words.split())


_WORDS: dict[str, frozenset[str]] = {
    "en": _set(
        "the a an and or but is are was were be been am i you he she we they it it's its "
        "my your our their me him her us them this that these those what which who whom "
        "when where why how do does did don't can can't could would should will won't "
        "shall may might must have has had not no yes to of in on at for with by from "
        "about as if then than so there here up out just any some all tomorrow tonight "
        "today let's lets want like please thanks hey hi weekend"
    ),
    "de": _set(
        "der die das den dem des ein eine einen einem einer und oder aber ist sind war "
        "bin bist sein ich du er sie wir ihr es mein dein unser euer mir dir mich dich "
        "uns euch was wer wie wo wann warum nicht kein keine ja nein zu von mit bei für "
        "auf aus nach über unter vor um im am ins zum zur auch noch schon nur doch mal "
        "kannst kann können willst will möchtest hast habe hat haben gehst kommst lust "
        "gerne heute morgen dann wenn dass ob sich"
    ),
    "es": _set(
        "el la los las un una unos unas y o pero es son era soy eres está estás estoy yo "
        "tú tu él ella nosotros vosotros ellos me te se nos mi mis su sus qué que quién "
        "cómo como dónde donde cuándo cuando por para con sin de del al en a no sí muy "
        "también ya hoy mañana puedes puedo quieres quiero tienes tengo vamos hay este "
        "esta eso lo le"
    ),
    "fr": _set(
        "le la les un une des et ou mais est sont était suis es être je tu il elle nous "
        "vous ils elles me te se mon ma mes ton ta tes son sa ses notre votre quoi que "
        "qui comment où quand pourquoi ne pas non oui de du au aux en à avec pour sans "
        "sur dans chez très aussi déjà demain aujourd'hui peux peut veux veut as ai avez "
        "on ce cette ça c'est il y"
    ),
    "ru": _set(
        "и или но а не нет да это этот эта эти что кто как где когда почему зачем я ты "
        "он она мы вы они мне тебе меня тебя нас вас мой моя твой твоя наш ваш в во на с "
        "со к ко по за из от до у о об про для без над под при же ли бы уже ещё еще "
        "только очень тоже завтра сегодня можешь могу хочешь хочу пойдём пойдем давай "
        "есть будет был была привет"
    ),
    "uk": _set(
        "і й та або але а не ні так це цей ця ці що хто як де коли чому я ти він вона ми "
        "ви вони мені тобі мене тебе нас вас мій моя твій твоя наш ваш в у на з із зі до "
        "по за від для без над під при же чи би вже ще тільки дуже теж також завтра "
        "сьогодні можеш можу хочеш хочу підемо давай є буде був була привіт"
    ),
}

# Letters that occur in one of the covered languages and (nearly) never in the others of
# the same script. Each distinct one adds a point.
_LETTERS = {
    "uk": "іїєґ",
    "ru": "ыэъё",
    "de": "äöüß",
    "es": "ñ¿¡",
    "fr": "çœèêëîïôûà",
}

_TOKEN = re.compile(r"[^\W\d_]+(?:'[^\W\d_]+)?")
_CYRILLIC = ("ru", "uk")
_LATIN = ("en", "de", "es", "fr")
MIN_LETTERS = 8


def _script(text: str) -> str | None:
    cyr = lat = 0
    for ch in text:
        if not ch.isalpha():
            continue
        if "Ѐ" <= ch <= "ӿ":
            cyr += 1
        elif ch.isascii() or "À" <= ch <= "ɏ" or ch in "œ":
            lat += 1
    total = cyr + lat
    if total < MIN_LETTERS:
        return None
    if cyr >= 0.8 * total:
        return "cyrillic"
    if lat >= 0.8 * total:
        return "latin"
    return None


def detect(text: str) -> str | None:
    """ISO 639-1 code of ``text`` among :data:`LANGUAGES`, or ``None`` when unsure."""
    lowered = text.lower().replace("’", "'")
    script = _script(lowered)
    if script is None:
        return None
    candidates = _CYRILLIC if script == "cyrillic" else _LATIN
    tokens = _TOKEN.findall(lowered)
    scores = {}
    for lang in candidates:
        words = _WORDS[lang]
        score = sum(1 for t in tokens if t in words)
        score += sum(1 for ch in _LETTERS.get(lang, "") if ch in lowered)
        scores[lang] = score
    if script == "cyrillic":
        # Ukrainian-only and Russian-only letters settle Cyrillic text on their own.
        uk = any(ch in lowered for ch in _LETTERS["uk"])
        ru = any(ch in lowered for ch in _LETTERS["ru"])
        if uk != ru:
            return "uk" if uk else "ru"
        # Ukrainian sentences nearly always contain one of its own letters.
        if not uk and scores["ru"] >= 1:
            return "ru"
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    (best, top), (_, second) = ranked[0], ranked[1]
    if top >= 2 and top > second:
        return best
    return None


def normalise(code: object) -> str | None:
    """``"de-DE"`` -> ``"de"``; anything that is not a covered code -> ``None``."""
    if not isinstance(code, str):
        return None
    base = code.strip().lower().replace("_", "-").split("-")[0]
    return base if base in LANGUAGES else None


def answer_line(code: str) -> str:
    return f"Answer language: {code} ({NAMES[code]})"
