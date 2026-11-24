import json

BOM_TAG = "utf8-sig"


def encode_tag() -> str:
    return json.dumps(BOM_TAG)
