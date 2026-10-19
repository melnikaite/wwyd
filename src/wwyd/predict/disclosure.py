# SPDX-License-Identifier: AGPL-3.0-or-later
"""Server-built disclosure for every predicted answer (SPEC P5, §8.2).

The templates below are product strings shown to third parties, one per supported language.
"""

from __future__ import annotations

from wwyd.store.personas import DEFAULT_DISCLOSURE

TEMPLATES: dict[str, str] = {
    "en": "This is {owner}'s auto-responder; most likely {owner} would answer:",
    "ru": "Это автоответчик {owner}; скорее всего, {owner} ответил(а) бы так:",
    "de": "Dies ist der automatische Assistent von {owner}; {owner} würde wahrscheinlich so antworten:",
    "uk": "Це автовідповідач {owner}; найімовірніше, {owner} відповів(ла) б так:",
    "es": "Este es el contestador automático de {owner}; lo más probable es que {owner} respondiera:",
    "fr": "Ceci est le répondeur automatique de {owner} ; {owner} répondrait probablement :",
}


def _lang(language: str | None) -> str:
    return (language or "").strip().lower().replace("_", "-").split("-")[0][:3]


def build_disclosure(owner: str, language: str | None, template: str | None = None) -> str:
    """``template`` is the owner's override; the stored default means "not overridden"."""
    if template and template != DEFAULT_DISCLOSURE and "{owner}" in template:
        chosen = template
    else:
        chosen = TEMPLATES.get(_lang(language), TEMPLATES["en"])
    return chosen.replace("{owner}", owner)
