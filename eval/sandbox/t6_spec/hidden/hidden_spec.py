from seq import dedupe

def test_non_adjacent():
    assert dedupe([1, 1, 2, 1]) == [1, 2]

def test_alternating():
    assert dedupe([3, 4, 3, 4, 3]) == [3, 4]

def test_strings():
    assert dedupe(['a', 'b', 'a']) == ['a', 'b']

def test_order_kept():
    assert dedupe([5, 9, 5, 1]) == [5, 9, 1]

def test_adjacent_still_works():
    assert dedupe([1, 1, 2]) == [1, 2]
