from seq import dedupe

def test_adjacent():
    assert dedupe([1, 1, 2]) == [1, 2]

def test_empty():
    assert dedupe([]) == []
