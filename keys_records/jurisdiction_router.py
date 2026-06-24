"""
Maps jurisdiction strings to permit adapter module names.
"""

JURISDICTION_MAP = {
    "KEY WEST": ["key_west"],
    "CITY OF KEY WEST": ["key_west"],
    "MARATHON": ["marathon"],
    "CITY OF MARATHON": ["marathon"],
    "ISLAMORADA": ["islamorada"],
    "VILLAGE OF ISLAMORADA": ["islamorada"],
    "KEY COLONY BEACH": ["key_colony"],
    "CITY OF KEY COLONY BEACH": ["key_colony"],
    "LAYTON": ["layton"],
    "CITY OF LAYTON": ["layton"],
}


def route(jurisdiction: str) -> list:
    """Return list of permit adapter names for the given jurisdiction string."""
    j = jurisdiction.upper().strip()
    for key, adapters in JURISDICTION_MAP.items():
        if key in j:
            return adapters
    return ["mcesearch", "opal"]  # unincorporated Monroe County default
