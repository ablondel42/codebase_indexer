import json

LEAF_CONST = "leaf"


def compute_leaf() -> str:
    return json.dumps(LEAF_CONST)
