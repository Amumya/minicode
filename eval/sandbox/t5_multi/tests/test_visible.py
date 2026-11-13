from report import build
from summary import total

def test_build_numbers():
    assert build([1.5, 2.25]) == '1.50, 2.25'

def test_total_numbers():
    assert total([1, 2, 3]) == 6
