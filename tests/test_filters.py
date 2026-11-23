"""The OSM download filter, read as Overpass reads it (no network)."""
import re

import pytest

from geo_mapper_api.filters import EXCLUDED_HIGHWAY, base_filter


def _excluded_pattern():
    return re.search(r'\["highway"!~"(.*)"\]$', base_filter).group(1)


def test_filter_requires_a_highway_tag_and_skips_areas():
    # Overpass's !~ also matches ways without the key, so the filter must require highway itself.
    assert base_filter.startswith('["highway"]["area"!~"yes"]')


def test_excluded_list_is_the_agreed_one():
    assert len(EXCLUDED_HIGHWAY) == 21
    assert set(EXCLUDED_HIGHWAY) == {
        "footway", "cycleway", "path", "steps", "pedestrian", "bridleway", "corridor", "elevator",
        "escalator", "ladder", "track", "construction", "proposed", "planned", "abandoned", "razed",
        "no", "raceway", "platform", "bus_stop", "trailhead",
    }


@pytest.mark.parametrize("value", EXCLUDED_HIGHWAY)
def test_filter_excludes_each_listed_value(value):
    assert re.search(_excluded_pattern(), value)


@pytest.mark.parametrize("value", [
    "service", "services", "busway", "bus_guideway", "road", "rest_area", "escape", "emergency_bay",
    "living_street", "unclassified", "residential", "motorway_link", "footpath", "nothing",
])
def test_filter_keeps_road_values_and_near_misses(value):
    # Overpass searches the regex, so only the anchors stop "no" matching inside "nothing".
    assert not re.search(_excluded_pattern(), value)
