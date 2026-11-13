def dedupe(items):
    """保留每个值第一次出现的位置，去掉后面再次出现的元素。"""
    out = []
    seen = set()
    for x in items:
        try:
            if x in seen:
                continue
            seen.add(x)
        except TypeError:
            # 不可哈希的元素回退到线性比较
            if x in out:
                continue
        out.append(x)
    return out