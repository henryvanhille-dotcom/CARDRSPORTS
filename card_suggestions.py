"""Conservative, text-only card detail suggestions for CARDR.

This module deliberately does *not* inspect an image, call an OCR service, or
look up a card online.  It accepts short, collector-supplied OCR/transcribed
text and extracts only readily visible, rule-based hints.  The result is
structured for a confirmation screen: callers must never save a suggestion as
confirmed card metadata without the collector reviewing it.

The small input limit, plain-text cleanup, and lack of dynamic evaluation make
this safe to call from an API endpoint.  UI callers should still render every
returned string with a text-only API (for example, ``textContent``) rather
than treating it as HTML.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


MAX_INPUT_CHARS = 4_000
MAX_INPUT_LINES = 60
MAX_FIELD_LENGTH = 80
MAX_CANDIDATES_PER_FIELD = 4
MAX_UNMATCHED_LINES = 8
MAX_UNMATCHED_LINE_CHARS = 160


TEXT_ONLY_NOTICE = (
    "These are text-only suggestions from collector-supplied OCR or transcription. "
    "CARDR did not inspect a photo or verify the card. Confirm every field before saving."
)


# These are common product names, not a product database.  An explicit Set:
# label can still suggest an unfamiliar set; unlabelled text only receives a
# set suggestion when it contains one of the deliberately small, recognizable
# phrases below.
KNOWN_SET_NAMES: Tuple[str, ...] = (
    "Topps Chrome Update",
    "Topps Cosmic Chrome",
    "Topps Stadium Club",
    "Topps Allen & Ginter",
    "Bowman Chrome",
    "Bowman Draft",
    "Bowman Sterling",
    "Bowman Sapphire",
    "Bowman Heritage",
    "Bowman Best",
    "Topps Finest",
    "Topps Heritage",
    "Topps Gallery",
    "Panini Prizm",
    "Panini Select",
    "Panini Mosaic",
    "Donruss Optic",
    "National Treasures",
    "Contenders Optic",
    "Upper Deck Young Guns",
    "Upper Deck",
    "Leaf Metal",
    "Fleer Ultra",
    "Immaculate",
    "Flawless",
    "Contenders",
    "Topps Chrome",
    "Topps",
    "Bowman",
    "Score",
)


PRODUCT_WORDS = {
    "allen",
    "and",
    "auto",
    "autograph",
    "baseball",
    "beckett",
    "best",
    "box",
    "bowman",
    "card",
    "chrome",
    "color",
    "contenders",
    "csg",
    "donruss",
    "draft",
    "fleer",
    "flawless",
    "gem",
    "grade",
    "heritage",
    "immaculate",
    "insert",
    "leaf",
    "metal",
    "mint",
    "mosaic",
    "mystery",
    "national",
    "nice",
    "numbered",
    "panini",
    "prizm",
    "psa",
    "pull",
    "refractor",
    "rookie",
    "score",
    "select",
    "set",
    "sgc",
    "silver",
    "sapphire",
    "stadium",
    "topps",
    "treasures",
    "ultra",
    "upper",
    "young",
}


PARALLEL_PATTERNS: Tuple[Tuple[str, str], ...] = (
    ("Superfractor", r"\bsuperfractor\b"),
    ("Silver Prizm", r"\bsilver\s+prizm\b"),
    ("Gold Refractor", r"\bgold\s+refractor\b"),
    ("Orange Refractor", r"\borange\s+refractor\b"),
    ("Red Refractor", r"\bred\s+refractor\b"),
    ("Blue Refractor", r"\bblue\s+refractor\b"),
    ("Cracked Ice", r"\bcracked\s+ice\b"),
    ("X-Fractor", r"\bx[\s-]?fractor\b"),
    ("Atomic", r"\batomic\b"),
    ("Sapphire", r"\bsapphire\b"),
    ("Refractor", r"\brefractor\b"),
    ("Shimmer", r"\bshimmer\b"),
    ("Mojo", r"\bmojo\b"),
    ("Holo", r"\bholo\b"),
    ("Sepia", r"\bsepia\b"),
    ("Gold", r"\bgold\b"),
    ("Orange", r"\borange\b"),
    ("Red", r"\bred\b"),
    ("Green", r"\bgreen\b"),
    ("Blue", r"\bblue\b"),
    ("Purple", r"\bpurple\b"),
    ("Aqua", r"\baqua\b"),
    ("Black", r"\bblack\b"),
    ("Pink", r"\bpink\b"),
)


FIELD_NAMES = (
    "player",
    "year",
    "set_name",
    "card_number",
    "card_type",
    "parallel",
    "grade",
)


def _confidence_label(percent: int) -> str:
    if percent >= 80:
        return "high"
    if percent >= 55:
        return "medium"
    if percent > 0:
        return "low"
    return "none"


def _plain_text(value: object, limit: int = MAX_FIELD_LENGTH) -> str:
    """Return bounded display text with control characters and tags removed.

    This is defense in depth only.  A browser must still render API values as
    text, never insert them with ``innerHTML``.
    """
    if not isinstance(value, str):
        return ""
    text = unicodedata.normalize("NFKC", value)
    text = "".join(
        character if character.isprintable() and character not in "<>" else " "
        for character in text
    )
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit].strip()


def _prepare_text(raw_text: object) -> Tuple[Optional[List[str]], Optional[Dict[str, str]], bool]:
    """Validate and sanitize user-supplied text without silently truncating it."""
    if not isinstance(raw_text, str):
        return None, {"code": "invalid_input", "message": "Card text must be a string."}, False
    if len(raw_text) > MAX_INPUT_CHARS:
        return (
            None,
            {
                "code": "input_too_long",
                "message": "Card text is limited to {} characters.".format(MAX_INPUT_CHARS),
            },
            False,
        )

    normalized = unicodedata.normalize("NFKC", raw_text).replace("\r\n", "\n").replace("\r", "\n")
    raw_lines = normalized.split("\n")
    if len(raw_lines) > MAX_INPUT_LINES:
        return (
            None,
            {
                "code": "too_many_lines",
                "message": "Card text is limited to {} lines.".format(MAX_INPUT_LINES),
            },
            False,
        )

    markup_removed = False
    lines: List[str] = []
    for raw_line in raw_lines:
        # Markup is never executed or interpreted.  Removing tag-shaped text
        # also keeps a future consumer from accidentally treating it as HTML.
        without_active_content, active_substitutions = re.subn(
            r"<\s*(?:script|style)[^>\n]{0,256}>.*?</\s*(?:script|style)\s*>",
            " ",
            raw_line,
            flags=re.IGNORECASE,
        )
        without_tags, tag_substitutions = re.subn(r"<[^>\n]{0,256}>", " ", without_active_content)
        markup_removed = markup_removed or bool(active_substitutions) or bool(tag_substitutions)
        cleaned = _plain_text(without_tags, limit=MAX_UNMATCHED_LINE_CHARS)
        if cleaned:
            lines.append(cleaned)

    if not lines:
        return None, {"code": "empty_input", "message": "Add readable card text before requesting suggestions."}, markup_removed
    return lines, None, markup_removed


def _empty_field() -> Dict[str, object]:
    return {"value": "", "confidence": "none", "confidence_percent": 0, "evidence": [], "candidates": []}


def _candidate_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.casefold())


def _new_fields() -> Dict[str, Dict[str, object]]:
    return {field: _empty_field() for field in FIELD_NAMES}


def _add_candidate(
    fields: Dict[str, Dict[str, object]],
    field: str,
    value: object,
    confidence_percent: int,
    evidence: str,
) -> bool:
    """Merge a bounded candidate and retain the strongest evidence only."""
    clean_value = _plain_text(value)
    if field not in fields or not clean_value:
        return False
    percent = max(1, min(100, int(confidence_percent)))
    entry = fields[field]
    candidates = entry["candidates"]
    assert isinstance(candidates, list)
    key = _candidate_key(clean_value)
    if not key:
        return False

    for candidate in candidates:
        if _candidate_key(str(candidate.get("value", ""))) != key:
            continue
        candidate["confidence_percent"] = max(int(candidate["confidence_percent"]), percent)
        candidate["confidence"] = _confidence_label(int(candidate["confidence_percent"]))
        candidate_evidence = candidate.get("evidence", [])
        if evidence and evidence not in candidate_evidence:
            candidate_evidence.append(evidence)
        break
    else:
        candidates.append(
            {
                "value": clean_value,
                "confidence": _confidence_label(percent),
                "confidence_percent": percent,
                "evidence": [evidence] if evidence else [],
            }
        )

    candidates.sort(key=lambda item: (-int(item["confidence_percent"]), str(item["value"]).casefold()))
    del candidates[MAX_CANDIDATES_PER_FIELD:]
    top = candidates[0] if candidates else None
    if top:
        entry["value"] = top["value"]
        entry["confidence"] = top["confidence"]
        entry["confidence_percent"] = top["confidence_percent"]
        entry["evidence"] = list(top["evidence"])
    return True


def _line_for_match(lines: Sequence[str], match_start: int, text: str) -> int:
    """Map a match position in joined text back to a bounded line index."""
    total = 0
    for index, line in enumerate(lines):
        next_total = total + len(line)
        if match_start <= next_total:
            return index
        total = next_total + 1
    return max(0, len(lines) - 1)


def _value_after_label(line: str, labels: Iterable[str]) -> str:
    label_expression = "|".join(re.escape(label) for label in labels)
    match = re.search(r"^\s*(?:{})\s*[:#\-]\s*(.+)$".format(label_expression), line, flags=re.IGNORECASE)
    if not match:
        return ""
    value = match.group(1)
    # Stop at clear second-field delimiters without trying to parse free-form
    # prose into a card identity.
    value = re.split(r"\s+(?:card\s*(?:number|no\.?|#)|grade|parallel|type|year|set|player)\s*[:#\-]", value, maxsplit=1, flags=re.IGNORECASE)[0]
    return _plain_text(value)


def _normalize_year(value: object) -> str:
    match = re.search(r"\b(19[5-9]\d|20(?:0\d|1\d|2\d|3[0-2]))\b", _plain_text(value))
    return match.group(1) if match else ""


def _known_set_in(value: str) -> str:
    for set_name in KNOWN_SET_NAMES:
        if re.search(r"\b{}\b".format(re.escape(set_name)), value, flags=re.IGNORECASE):
            return set_name
    return ""


def _trim_set_value(value: str) -> str:
    value = re.sub(r"^\s*(?:19[5-9]\d|20(?:0\d|1\d|2\d|3[0-2]))\s+", "", value)
    value = re.split(r"\s+(?:#|card\s*(?:number|no\.?|#)|psa|bgs|sgc|cgc|csg)\b", value, maxsplit=1, flags=re.IGNORECASE)[0]
    return _plain_text(value)


def _is_human_name(value: str) -> bool:
    words = value.split()
    if not 2 <= len(words) <= 4:
        return False
    for word in words:
        bare_word = word.replace("'", "").replace("-", "").replace(".", "")
        if not bare_word or not bare_word.isalpha() or bare_word.casefold() in PRODUCT_WORDS:
            return False
    return True


def _display_name(value: str) -> str:
    words = []
    for word in value.split():
        if word.casefold() in {"ii", "iii", "iv", "jr", "sr"}:
            words.append(word.upper() if word.casefold() != "jr" else "Jr.")
        elif "'" in word:
            words.append("'".join(part.capitalize() for part in word.split("'")))
        elif "-" in word:
            words.append("-".join(part.capitalize() for part in word.split("-")))
        else:
            words.append(word.capitalize())
    return " ".join(words)


def _trim_player_value(value: str) -> str:
    value = re.split(
        r"\s+(?:19[5-9]\d|20(?:0\d|1\d|2\d|3[0-2])|#|card\b|rookie\b|rc\b|auto(?:graph)?\b|psa\b|bgs\b|sgc\b|cgc\b)",
        value,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0]
    return _plain_text(value)


def _normalize_card_number(value: object) -> str:
    text = _plain_text(value)
    text = re.sub(r"\s*-\s*", "-", text).replace(" ", "").upper()
    if not re.fullmatch(r"[A-Z]{0,8}(?:-[A-Z0-9]{1,8})?\d{1,6}[A-Z]?|\d{1,6}[A-Z]?", text):
        # The prior expression is intentionally permissive for values such as
        # BCP-123 while rejecting long prose or serial-number fractions.
        if not re.fullmatch(r"[A-Z]{1,8}-\d{1,6}[A-Z]?", text):
            return ""
    return text


def _grade_from_text(text: str) -> Tuple[str, int, str]:
    grade_match = re.search(
        r"\b(PSA|BGS|BECKETT|SGC|CGC|CSG)\s*(?:GEM\s*MINT\s*)?(10|[1-9](?:\.5)?)\b",
        text,
        flags=re.IGNORECASE,
    )
    if grade_match:
        company = grade_match.group(1).upper()
        if company == "BECKETT":
            company = "BGS"
        return "{} {}".format(company, grade_match.group(2)), 90, "recognized grading-company label"
    if re.search(r"\b(?:raw|ungraded)\b", text, flags=re.IGNORECASE):
        return "Raw", 65, "explicit raw/ungraded text"
    return "", 0, ""


def _type_from_text(text: str) -> Tuple[str, int, str]:
    normalized = text.casefold()
    first_bowman = bool(re.search(r"\b(?:1st|first)\s+bowman\b", normalized))
    autograph = bool(re.search(r"\b(?:auto|autograph|signed|signature)\b", normalized))
    rookie = bool(re.search(r"\b(?:rookie\s+card|rookie|rc)\b", normalized))
    if first_bowman and autograph:
        return "1st Bowman Auto", 88, "1st Bowman and autograph terms"
    if rookie and autograph:
        return "Rookie Autograph", 82, "rookie and autograph terms"
    if first_bowman:
        return "1st Bowman", 82, "1st Bowman term"
    if autograph:
        return "Autograph", 78, "autograph term"
    if rookie:
        return "Rookie Card", 75, "rookie/RC term"
    if re.search(r"\b(?:relic|memorabilia|jersey\s+card)\b", normalized):
        return "Relic", 72, "relic/memorabilia term"
    if re.search(r"\bprospect\b", normalized):
        return "Prospect", 60, "prospect term"
    if re.search(r"\binsert\b", normalized):
        return "Insert", 60, "insert term"
    return "", 0, ""


def _parallel_from_text(text: str) -> Tuple[str, int, str]:
    for canonical, expression in PARALLEL_PATTERNS:
        if re.search(expression, text, flags=re.IGNORECASE):
            if canonical == "Superfractor" and re.search(r"\b1\s*/\s*1\b", text):
                return "Superfractor 1/1", 88, "Superfractor and 1/1 terms"
            return canonical, 76, "recognized parallel term"
    if re.search(r"\b1\s*/\s*1\b", text):
        return "1/1", 55, "1/1 scarcity marker"
    return "", 0, ""


def _unlabelled_name_candidates(line: str) -> List[str]:
    """Find low-confidence two/three-word name-shaped fragments only.

    The caller must keep these candidates visibly low confidence because no
    player database or image recognition has corroborated them.
    """
    # Card identifiers such as #CPA-1 are not names.  Remove them before the
    # simple name-shaped scan so a suffix like "CPA" cannot become a bogus
    # second or third name.
    line_without_numbers = re.sub(r"#\s*[A-Za-z0-9\- ]{1,20}\b", " ", line)
    line_without_numbers = re.sub(r"\b[A-Za-z]{2,8}\s*-\s*\d{1,6}[A-Za-z]?\b", " ", line_without_numbers)
    tokens = re.findall(r"[A-Za-z][A-Za-z'\-\.]*", line_without_numbers)
    candidates: List[str] = []
    for size in (3, 2):
        for start in range(0, len(tokens) - size + 1):
            candidate = " ".join(tokens[start : start + size])
            # OCR is commonly all caps.  Restricting unlabelled candidates to
            # that form avoids turning normal sentence fragments such as
            # "mystery box pull" into a fictional player.
            if candidate.upper() != candidate:
                continue
            if _is_human_name(candidate):
                display = _display_name(candidate)
                if _candidate_key(display) not in {_candidate_key(item) for item in candidates}:
                    candidates.append(display)
    return candidates[:MAX_CANDIDATES_PER_FIELD]


def suggest_card_details(raw_text: object) -> Dict[str, Any]:
    """Return confirm-before-save card metadata suggestions from supplied text.

    ``raw_text`` is intentionally the only input.  There is no image input,
    network request, OCR provider, or remote card lookup behind this helper.
    """
    lines, error, markup_removed = _prepare_text(raw_text)
    base: Dict[str, Any] = {
        "recognition_mode": "text_only",
        "photo_recognition": {"available": False, "reason": "No image recognition or OCR provider is used."},
        "non_authoritative": True,
        "requires_confirmation": True,
        "notice": TEXT_ONLY_NOTICE,
        "input_limits": {"max_characters": MAX_INPUT_CHARS, "max_lines": MAX_INPUT_LINES},
    }
    if error:
        return {
            **base,
            "status": "input_rejected",
            "error": error,
            "fields": _new_fields(),
            "suggestions": {},
            "unmatched_text": [],
            "warnings": ["No card details were inferred from rejected input."],
        }

    assert lines is not None
    text = "\n".join(lines)
    fields = _new_fields()
    recognized_lines = set()

    # Explicit labels receive the strongest confidence because the collector
    # (or their OCR) supplied the field boundary.
    for line_index, line in enumerate(lines):
        player_value = _trim_player_value(_value_after_label(line, ("player", "athlete", "name")))
        if player_value and _is_human_name(player_value):
            _add_candidate(fields, "player", _display_name(player_value), 90, "explicit player label")
            recognized_lines.add(line_index)

        year_value = _normalize_year(_value_after_label(line, ("year", "season")))
        if year_value:
            _add_candidate(fields, "year", year_value, 92, "explicit year label")
            recognized_lines.add(line_index)

        set_value = _value_after_label(line, ("set", "product", "brand"))
        if set_value:
            known_set = _known_set_in(set_value)
            candidate_set = known_set or _trim_set_value(set_value)
            if candidate_set:
                _add_candidate(
                    fields,
                    "set_name",
                    candidate_set,
                    90 if known_set else 72,
                    "explicit set label" if known_set else "explicit unfamiliar set label",
                )
                recognized_lines.add(line_index)

        number_value = _value_after_label(line, ("card number", "card no.", "card no", "number", "no."))
        number_value = _normalize_card_number(number_value)
        if number_value:
            _add_candidate(fields, "card_number", number_value, 90, "explicit card-number label")
            recognized_lines.add(line_index)

        type_value = _value_after_label(line, ("type", "card type"))
        normalized_type, type_confidence, type_evidence = _type_from_text(type_value)
        if normalized_type:
            _add_candidate(fields, "card_type", normalized_type, max(82, type_confidence), "explicit type label: " + type_evidence)
            recognized_lines.add(line_index)

        parallel_value = _value_after_label(line, ("parallel", "variation"))
        normalized_parallel, parallel_confidence, parallel_evidence = _parallel_from_text(parallel_value)
        if normalized_parallel:
            _add_candidate(fields, "parallel", normalized_parallel, max(82, parallel_confidence), "explicit parallel label: " + parallel_evidence)
            recognized_lines.add(line_index)

        grade_value = _value_after_label(line, ("grade", "grading"))
        normalized_grade, grade_confidence, grade_evidence = _grade_from_text(grade_value)
        if normalized_grade:
            _add_candidate(fields, "grade", normalized_grade, max(88, grade_confidence), "explicit grade label: " + grade_evidence)
            recognized_lines.add(line_index)

    # Generic evidence remains deliberately below an explicit label.
    for match in re.finditer(r"\b(19[5-9]\d|20(?:0\d|1\d|2\d|3[0-2]))\b", text):
        if _add_candidate(fields, "year", match.group(1), 68, "year-shaped text"):
            recognized_lines.add(_line_for_match(lines, match.start(), text))

    known_set_matches = []
    for set_name in KNOWN_SET_NAMES:
        match = re.search(r"\b{}\b".format(re.escape(set_name)), text, flags=re.IGNORECASE)
        if match:
            known_set_matches.append((set_name, match))
    # A short product word such as "Bowman" is contained in "Bowman Chrome".
    # Retain the most specific recognizable product rather than offering a
    # weaker substring as the primary suggestion.
    if known_set_matches:
        set_name, match = max(known_set_matches, key=lambda item: (len(item[0]), -item[1].start()))
        if _add_candidate(fields, "set_name", set_name, 74, "recognized set phrase"):
            recognized_lines.add(_line_for_match(lines, match.start(), text))

    number_patterns = (
        r"(?:\b(?:card\s*)?(?:number|no\.?)\s*[:#]?\s*|#\s*)([A-Za-z]{0,8}(?:\s*-\s*)?\d{1,6}[A-Za-z]?)\b",
        r"\b([A-Za-z]{2,8}\s*-\s*\d{1,6}[A-Za-z]?)\b",
    )
    for expression in number_patterns:
        for match in re.finditer(expression, text, flags=re.IGNORECASE):
            card_number = _normalize_card_number(match.group(1))
            if card_number and _add_candidate(fields, "card_number", card_number, 74 if "#" in match.group(0) or "no" in match.group(0).casefold() else 48, "card-number-shaped text"):
                recognized_lines.add(_line_for_match(lines, match.start(), text))

    card_type, type_confidence, type_evidence = _type_from_text(text)
    if card_type:
        _add_candidate(fields, "card_type", card_type, type_confidence, type_evidence)

    parallel, parallel_confidence, parallel_evidence = _parallel_from_text(text)
    if parallel:
        _add_candidate(fields, "parallel", parallel, parallel_confidence, parallel_evidence)

    grade, grade_confidence, grade_evidence = _grade_from_text(text)
    if grade:
        _add_candidate(fields, "grade", grade, grade_confidence, grade_evidence)

    # Name-shaped text is never promoted above low confidence without an
    # explicit Player/Athlete/Name field.
    for line_index, line in enumerate(lines):
        for player_candidate in _unlabelled_name_candidates(line):
            if _add_candidate(fields, "player", player_candidate, 40, "unlabelled name-shaped text"):
                recognized_lines.add(line_index)

    suggestions = {field: data["value"] for field, data in fields.items() if data["value"]}
    unmatched = [
        line[:MAX_UNMATCHED_LINE_CHARS]
        for index, line in enumerate(lines)
        if index not in recognized_lines
    ][:MAX_UNMATCHED_LINES]

    warnings = [TEXT_ONLY_NOTICE]
    if markup_removed:
        warnings.append("Tag-shaped markup was removed before parsing; no markup was executed.")
    if any(data["confidence"] == "low" for data in fields.values()):
        warnings.append("At least one suggestion is low confidence because it came from unlabeled text.")
    if re.search(r"\b1\s*/\s*1\b", text) and fields["parallel"]["value"] in {"", "1/1"}:
        warnings.append("A 1/1 marker may describe scarcity, not the named parallel. Confirm the exact parallel.")
    if not suggestions:
        warnings.append("No reliable card details were found. Add field labels or clearer transcription before trying again.")

    return {
        **base,
        "status": "suggestions_available" if suggestions else "insufficient_text",
        "input_summary": {"characters_used": len(text), "line_count": len(lines), "markup_removed": markup_removed},
        "fields": fields,
        "suggestions": suggestions,
        "unmatched_text": unmatched,
        "warnings": warnings,
    }


# Friendly alias for route code that reads more naturally at the integration
# point.  Keeping both names avoids coupling consumers to a UI label.
suggest_from_text = suggest_card_details
