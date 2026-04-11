"""LANDFIRE lookup data loaded from JSON files."""

import json
from pathlib import Path

_DATA_DIR = Path(__file__).parent


def _load_json(filename: str) -> dict:
    with open(_DATA_DIR / filename) as f:
        return json.load(f)


# Large lookup tables loaded from JSON files
# Keys are string pixel values, values are names/labels
EVT_CODES: dict[str, str] = _load_json("evt_codes.json")
BPS_CODES: dict[str, str] = _load_json("bps_codes.json")
FDIST_CODES: dict[str, dict[str, str]] = _load_json("fdist_codes.json")
