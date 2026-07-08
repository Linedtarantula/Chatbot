"""
Geographic zone helpers for the province of Huelva.

Localities in the same zone are geographically close, so grouping jobs by zone
lets the installer minimise travel. This module is shared by the database
service and the REST API.
"""

import unicodedata

# Known zones/localities in the province of Huelva. Each entry maps a canonical
# zone name to the localities that belong to it.
ZONE_GROUPS = {
    'Costa Occidental': ['islantilla', 'lepe', 'la antilla', 'antilla', 'ayamonte',
                         'isla cristina', 'cartaya', 'punta umbria', 'el rompido'],
    'Huelva Capital': ['huelva', 'aljaraque', 'gibraleon', 'san juan del puerto'],
    'Condado': ['moguer', 'palos', 'bollullos', 'la palma del condado', 'almonte',
               'bonares', 'rociana', 'lucena'],
    'Sierra': ['aracena', 'jabugo', 'cortegana', 'valverde'],
}


def normalize(text):
    """Lowercase and strip accents for robust locality matching."""
    if not text:
        return ''
    text = text.strip().lower()
    text = ''.join(
        c for c in unicodedata.normalize('NFKD', text)
        if not unicodedata.combining(c)
    )
    return text


def get_zone_for_location(location):
    """Return the canonical zone name for a locality (or the locality itself)."""
    norm = normalize(location)
    if not norm:
        return None
    for zone, localities in ZONE_GROUPS.items():
        for loc in localities:
            if loc in norm or norm in loc:
                return zone
    # Unknown locality: treat the locality itself as its own zone.
    return location.strip().title() if location else None


def locations_same_zone(loc_a, loc_b):
    """True if two localities belong to the same working zone."""
    if not loc_a or not loc_b:
        return False
    if normalize(loc_a) == normalize(loc_b):
        return True
    za, zb = get_zone_for_location(loc_a), get_zone_for_location(loc_b)
    return bool(za and zb and za == zb)
