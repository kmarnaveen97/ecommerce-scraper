from ecommerce_scraper.sampling import deterministic_sample


def test_sample_is_deterministic_and_does_not_modify_input() -> None:
    values = list(range(100))
    original = values.copy()

    first = deterministic_sample(values, 10, "store/category")
    second = deterministic_sample(values, 10, "store/category")

    assert first == second
    assert len(first) == 10
    assert len(set(first)) == 10
    assert values == original


def test_sample_returns_all_unique_values_when_under_limit() -> None:
    assert deterministic_sample([1, 1, 2], 10, "seed") == [1, 2]

