"""The Overpass filter for the OSM road network each county is matched against.

osmnx uses a custom filter in place of its network_type presets, so this string is the whole
filter. It keeps every way with a highway tag, including service roads and private roads, which
INRIX's conflation lists, and leaves out roads drawn as areas and the values below.
Overpass's `!~` also matches ways without the key, so `["highway"]` must be required on its own.
The exclusion pattern is anchored, so it removes only the exact values listed.
"""
EXCLUDED_HIGHWAY = (
    # Ways for people on foot, bike or horse.
    "footway", "cycleway", "path", "steps", "pedestrian", "bridleway", "corridor", "elevator",
    "escalator", "ladder",
    # Not open roads.
    "track", "construction", "proposed", "planned", "abandoned", "razed", "no", "raceway",
    # Not carriageways.
    "platform", "bus_stop", "trailhead",
)

base_filter = '["highway"]["area"!~"yes"]["highway"!~"^(' + "|".join(EXCLUDED_HIGHWAY) + ')$"]'
