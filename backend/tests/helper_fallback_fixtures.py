"""Offline-only localized test copies for Platinum Helper.

Production never imports this fixture; live translations come from UiTranslation
(maintained by the existing AI Localizer).
"""
from localization.catalog import english_dictionary
from platinum_helper.fallback_locale import FALLBACK_KEYS


def english_templates():
    source = english_dictionary()
    return {key.value: source[key.value] for key in FALLBACK_KEYS}


def russian_templates():
    templates = english_templates()
    templates.update({
        "helper.fallback.confident.1": "Я знаю: «{{answer}}».",
        "helper.fallback.confident.2": "Ну, здесь точно «{{answer}}».",
        "helper.fallback.tentative.1": "Мне кажется, здесь «{{answer}}».",
        "helper.fallback.tentative.2": "Скорее «{{answer}}», но это не точно.",
        "helper.fallback.unsure": "Тут я помочь не могу.",
    })
    return templates
