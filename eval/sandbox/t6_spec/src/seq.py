def dedupe(items):
    """去掉相邻的重复项。"""
    out = []
    for x in items:
        if not out or out[-1] != x:
            out.append(x)
    return out