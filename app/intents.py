from __future__ import annotations

import re


PHOTO_QUESTION_PATTERNS = (
    r"\b(?:можно|могу|можно\s+ли|могу\s+ли)\b.{0,35}\b(?:фото|фотку|фотографию|картинку)\b",
    r"\b(?:фото|фотку|фотографию|картинку)\b.{0,35}\b(?:можно|могу|принимаешь|понимаешь|отправить|скинуть|прислать)\b",
)


def is_photo_question(text: str) -> bool:
    normalized = " ".join(text.lower().strip().split())
    return any(re.search(pattern, normalized) for pattern in PHOTO_QUESTION_PATTERNS)


def looks_like_question(text: str) -> bool:
    normalized = " ".join(text.lower().strip().split())
    if not normalized:
        return False

    if is_photo_question(normalized):
        return True

    if "?" in normalized:
        return True

    prefixes = (
        "а можно ",
        "а я могу ",
        "можно ли ",
        "можно ",
        "могу ли ",
        "могу ",
        "как ",
        "куда ",
        "зачем ",
        "почему ",
        "нужно ли ",
        "надо ли ",
        "ты можешь ",
    )
    return normalized.startswith(prefixes)
