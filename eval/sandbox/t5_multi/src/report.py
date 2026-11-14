from helpers import fmt

def build(values):
    """把一串数字拼成一行。"""
    return ", ".join(fmt(v) for v in values)