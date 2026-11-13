from counter import count_words

def test_empty():
    assert count_words("") == 0

def test_two():
    assert count_words("a b") == 2

def test_five():
    assert count_words("a b c d e") == 5
