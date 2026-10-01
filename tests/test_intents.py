from app.intents import is_photo_question, looks_like_question


def test_photo_question_is_not_treated_as_ingredients():
    text = "а я могу фото отправить?"
    assert is_photo_question(text)
    assert looks_like_question(text)


def test_regular_ingredient_list_is_not_a_question():
    assert not looks_like_question("яйца сыр макароны помидоры курица")


def test_regular_question_is_guarded():
    assert looks_like_question("как мне лучше это отправить?")
