def parse_range(spec: str) -> list[int]:
    return [int(x) for x in spec.split(",") if x.strip()]
