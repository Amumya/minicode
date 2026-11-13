from ranges import parse_range

def test_mixed():
    assert parse_range("1-3,5") == [1, 2, 3, 5]

def test_only_range():
    assert parse_range("2-4") == [2, 3, 4]

def test_empty():
    assert parse_range("") == []
