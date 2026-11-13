from report import build
from summary import total

def test_build_none():
    assert build([None]) == '-'

def test_build_mixed():
    assert build([1.5, None, 2.25]) == '1.50, -, 2.25'

def test_total_none():
    assert total([1, None, 3]) == 4

def test_total_all_none():
    assert total([None, None]) == 0

def test_original_still_works():
    assert build([1.5, 2.25]) == '1.50, 2.25'
    assert total([1, 2, 3]) == 6
