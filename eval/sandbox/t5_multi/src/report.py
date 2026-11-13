from helpers import fmt


def build(values):
    """鎶婁竴涓叉暟瀛楁嫾鎴愪竴琛屻€?""
    return ", ".join("-" if v is None else fmt(v) for v in values)
