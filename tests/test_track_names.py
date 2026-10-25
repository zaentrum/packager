"""A rendition's NAME keeps the script and the region its language's tag or
its source title names, in the words of the clients' spec (docs/chino,
subtitles: "Portuguese (Brazil)", "Chinese (Simplified)"), as chino-web's
lib/languages.ts reads a title; a code that only repeats the language says
nothing. The cases are the clients' own, from languages.test.ts."""

from __future__ import annotations

import pytest

from packager import packager as pk
from packager import regions


def _name(language: str, title: str) -> str:
    return pk._audio_display_name({"language": language, "title": title})


@pytest.mark.parametrize(("language", "title", "name"), [
    # A title that is a code of the track's language: its region or script
    # stays, in the word list's form.
    ("por", "PT-BR", "Portuguese (Brazil)"),
    ("por", "pt-BR", "Portuguese (Brazil)"),
    ("por", "pt_BR", "Portuguese (Brazil)"),
    ("por", "PT-PT", "Portuguese (Portugal)"),
    ("chi", "zh-Hans", "Chinese (Simplified)"),
    ("chi", "zh-Hant", "Chinese (Traditional)"),
    ("chi", "zh-Hant-TW", "Chinese (Traditional, Taiwan)"),
    ("spa", "ES-419", "Spanish (Latin America)"),
    ("spa", "es-419", "Spanish (Latin America)"),
    ("spa", "es-ES", "Spanish (Spain)"),
    ("eng", "en-US", "English (United States)"),
    ("eng", "en-GB", "English (United Kingdom)"),
    ("eng", "en-US-x-custom", "English (United States)"),
    # In brackets after the language's name, and before what else it says.
    ("por", "Português (PT-BR)", "Portuguese (Brazil)"),
    ("por", "PT-BR (Commentary)", "Portuguese (Brazil) · Commentary"),
    # A region's code in brackets after the language's name, where the
    # region is one of the language's own.
    ("por", "Português (BR)", "Portuguese (Brazil)"),
    ("por", "Português (PT)", "Portuguese (Portugal)"),
    ("spa", "Español (419)", "Spanish (Latin America)"),
    ("spa", "Español [MX]", "Spanish (Mexico)"),
    ("fre", "Français (CA)", "French (Canada)"),
    ("ger", "Deutsch (CH)", "German (Switzerland)"),
    ("chi", "中文\uff08TW\uff09", "Chinese (Taiwan)"),
    ("por", "Português (BR) - Comentário", "Portuguese (Brazil) · Comentário"),
    # The language's own code, where it is none of its regions, and a code
    # alone: only the language again, dropped as before.
    ("swe", "Svenska (SV)", "Swedish"),
    ("cat", "Català (CA)", "Catalan"),
    ("ara", "Arabic (AR)", "Arabic"),
    ("por", "Português (pt)", "Portuguese"),
    ("por", "pt", "Portuguese"),
    ("eng", "eng", "English"),
    ("eng", "EN", "English"),
    ("eng", "Commentary (eng)", "English · Commentary"),
    # Capitals that are no region of the language are a word; another
    # language's code is something else; another region or script than the
    # tag's stays as the title says it.
    ("eng", "English (BD)", "English · BD"),
    ("eng", "de", "English · de"),
    ("pt-pt", "PT-BR", "Portuguese (Portugal) · PT-BR"),
    ("zh-hant", "zh-Hans", "Chinese (Traditional) · zh-Hans"),
    # The tag's own region or script: said once.
    ("pt-br", "", "Portuguese (Brazil)"),
    ("pt-br", "PT-BR", "Portuguese (Brazil)"),
    ("pt-br", "Portuguese (Brazil)", "Portuguese (Brazil)"),
    ("zh-hans", "", "Chinese (Simplified)"),
    ("zh-hant-tw", "", "Chinese (Traditional, Taiwan)"),
    ("sr-latn", "", "Serbian (Latin)"),
    ("en-gu", "", "English (Guam)"),
    ("de-ch", "Deutsch", "German (Switzerland)"),
    # A region or a script in words: the standard form, never the dialect's.
    ("por", "Brazilian Portuguese", "Portuguese (Brazil)"),
    ("por", "Portuguese (Brazilian)", "Portuguese (Brazil)"),
    ("por", "Portuguese - Brazilian", "Portuguese (Brazil)"),
    ("por", "Português - Brasil", "Portuguese (Brazil)"),
    ("eng", "British English", "English (United Kingdom)"),
    ("eng", "English (UK)", "English (United Kingdom)"),
    ("eng", "English [American]", "English (United States)"),
    ("dut", "Flemish", "Dutch (Belgium)"),
    ("ger", "German (Austrian)", "German (Austria)"),
    ("spa", "Spanish (Latin America)", "Spanish (Latin America)"),
    ("spa", "Español (Latinoamérica)", "Spanish (Latin America)"),
    ("spa", "Español Latino", "Spanish (Latin America)"),
    ("fre", "Français canadien", "French (Canada)"),
    ("chi", "简体中文", "Chinese (Simplified)"),
    ("chi", "繁體中文", "Chinese (Traditional)"),
    ("chi", "Chinese (Simplified)", "Chinese (Simplified)"),
    ("chi", "Simplified Chinese", "Chinese (Simplified)"),
    ("chi", "繁體中文\uff08台灣\uff09", "Chinese (Traditional, Taiwan)"),
    ("chi", "中文", "Chinese"),
    # Another region than the tag's stays a word; a region among other
    # words is a word.
    ("es-es", "Latinoamérica", "Spanish (Spain) · Latinoamérica"),
    ("pt-pt", "Portuguese (Brazilian)", "Portuguese (Portugal) · Brazilian"),
    ("por", "Brazilian Commentary", "Portuguese · Brazilian Commentary"),
    # A track of no known language is called what its title says.
    ("und", "PT-BR", "PT-BR"),
    ("und", "", "Unknown"),
    ("qaa-br", "", "qaa-br"),
    # Nothing to read: as before.
    ("eng", "Commentary", "English · Commentary"),
    ("eng", "English (SDH)", "English · SDH"),
    ("ger", "Deutsch (Kommentar)", "German · Kommentar"),
    ("eng", "Signs/Songs", "English · Signs/Songs"),
    ("eng", "AC3 5.1 @ 640 Kbps", "English"),
])
def test_a_name_keeps_the_region_or_script(language: str, title: str, name: str) -> None:
    assert _name(language, title) == name


def test_never_the_dialect_form_or_a_code() -> None:
    names = [_name("por", t) for t in ("PT-BR", "pt-BR", "Brazilian Portuguese", "Português (BR)")]
    assert set(names) == {"Portuguese (Brazil)"}
    assert _name("pt-br", "") != "Brazilian Portuguese"
    assert _name("spa", "ES-419") != "Latin American Spanish"


def test_the_51_group_and_a_forced_subtitle() -> None:
    assert pk._audio_display_name({"language": "por", "title": "PT-BR"},
                                  surround=True) == "Portuguese (Brazil) 5.1"
    assert pk._audio_display_name({"language": "spa", "title": "ES-419 Commentary"},
                                  surround=True) == "Spanish 5.1 · ES-419 Commentary"
    sub = pk._subtitle_display_name
    assert sub({"language": "por", "title": "PT-BR", "forced": True}) == \
        "Portuguese (Brazil) (forced)"
    assert sub({"language": "por", "title": "PT-BR (Forced)", "forced": True}) == \
        "Portuguese (Brazil) · Forced"
    assert sub({"language": "chi", "title": "zh-Hans", "forced": False}) == \
        "Chinese (Simplified)"


@pytest.mark.parametrize(("tag", "script", "region", "name"), [
    # The spec's examples (docs/chino, subtitles: for the builders).
    ("por", "", "BR", "Portuguese (Brazil)"),
    ("eng", "", "GU", "English (Guam)"),
    ("chi", "Hans", "", "Chinese (Simplified)"),
    ("zho", "Hant", "TW", "Chinese (Traditional, Taiwan)"),
    ("spa", "", "419", "Spanish (Latin America)"),
    ("eng", "", "", "English"),
    ("zxx", "", "BR", "No dialogue"),
    ("und", "", "BR", "Unknown"),
])
def test_language_display_name(tag: str, script: str, region: str, name: str) -> None:
    assert pk._language_display_name(tag, script, region) == name


@pytest.mark.parametrize(("code", "parts"), [
    ("PT-BR", ("pt", "", "BR")),
    ("por_br", ("pt", "", "BR")),
    ("zh-hant-tw", ("zh", "Hant", "TW")),
    ("ES-419", ("es", "", "419")),
    ("ger", ("de", "", "")),
    ("en-US-x-custom", ("en", "", "US")),
    ("fil", ("fil", "", "")),
    ("de-1901", None),     # a variant: no code read
    ("eng-sdh", None),
    ("pt-XY", None),       # a region CLDR doesn't name
])
def test_subtags_of(code: str, parts: tuple[str, str, str] | None) -> None:
    assert pk._subtags_of(code) == parts


def test_the_cldr_tables_name_what_the_clients_name() -> None:
    # A spot check of regions.py against what Intl.DisplayNames gives the
    # clients (scripts/regions.mjs writes it).
    assert regions.REGION_NAMES["BR"] == "Brazil"
    assert regions.REGION_NAMES["419"] == "Latin America"
    assert regions.SCRIPT_NAMES["Hans"] == "Simplified"
    assert regions.SCRIPT_NAMES["Hant"] == "Traditional"
    assert {"BR", "PT"} <= regions.LANGUAGE_REGIONS["pt"]
    assert "CA" in regions.LANGUAGE_REGIONS["fr"]           # Canadian French
    assert "SV" not in regions.LANGUAGE_REGIONS.get("sv", frozenset())
    assert "CA" not in regions.LANGUAGE_REGIONS.get("ca", frozenset())
    assert regions.DIALECTS["pt"]["brazilian portuguese"] == "BR"
