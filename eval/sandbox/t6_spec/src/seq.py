def dedupe(items):
    """去掉重复项：保留每个值第一次出现的位置，后面再次出现的全部去掉。"""
    out = []
    seen = set()
    for x in items:
        try:
            if x in seen:
                continue
            seen.add(x)
        except TypeError:  # 不可哈希的元素退化为线性查找
            if x in out:
                continue
        out.append(x)
    return out
