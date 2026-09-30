from app.calculations import calculate_targets, estimate_expenditure


def test_lose_target_is_below_maintenance():
    t = calculate_targets("male", 30, 182, 84, "medium", "lose")
    assert t.calories < t.maintenance
    assert t.protein_g > 0


def test_expenditure_grows_with_steps():
    low = estimate_expenditure(1800, 84, 2000, None, 0)
    high = estimate_expenditure(1800, 84, 10000, None, 0)
    assert high > low
