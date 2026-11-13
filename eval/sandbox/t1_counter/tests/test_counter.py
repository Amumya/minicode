from counter import count_words

def test_three_words():
    assert count_words("a b c") == 3

def test_one_word():
    assert count_words("hello") == 1
