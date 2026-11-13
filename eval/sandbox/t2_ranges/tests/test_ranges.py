from ranges import parse_range

def test_single_numbers():
    assert parse_range("1,2") == [1, 2]
