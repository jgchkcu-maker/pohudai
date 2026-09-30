from app.calculations import calculate_targets, estimate_eer, estimate_expenditure


def test_adult_weight_loss_uses_moderate_capped_deficit():
    t = calculate_targets("male", 30, 182, 84, "medium", "lose", 76)
    assert t.calories < t.maintenance
    assert 250 <= t.deficit_kcal <= 500
    assert not t.is_youth
    assert t.method == "DRI_EER_2023"


def test_youth_does_not_get_automatic_deficit():
    t = calculate_targets("male", 16, 180, 80, "light", "lose", 72)
    assert t.is_youth
    assert t.calories == t.maintenance
    assert t.deficit_kcal == 0


def test_questionnaire_activity_changes_eer():
    inactive = estimate_eer("female", 25, 168, 65, "low")
    active = estimate_eer("female", 25, 168, 65, "medium")
    assert active > inactive


def test_target_weight_affects_protein_planning_for_weight_loss():
    current_weight = calculate_targets("male", 30, 182, 100, "light", "lose")
    target_weight = calculate_targets("male", 30, 182, 100, "light", "lose", 80)
    assert target_weight.protein_g < current_weight.protein_g


def test_expenditure_grows_with_steps():
    low = estimate_expenditure(2100, 84, 3000, None, 0)
    high = estimate_expenditure(2100, 84, 10000, None, 0)
    assert high > low
