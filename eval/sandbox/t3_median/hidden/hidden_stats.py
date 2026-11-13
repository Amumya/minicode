from stats import median, mean

def test_odd():
    assert median([3, 1, 2]) == 2

def test_even():
    assert median([1, 2, 3, 4]) == 2.5

def test_empty():
    assert median([]) is None

def test_mean_still_works():
    assert mean([2, 4]) == 3
