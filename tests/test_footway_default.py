"""Default matrice: footway jako zpevněný chodník."""

from app.settings import default_footway_as_sidewalk


def test_default_footway_matrix():
    assert default_footway_as_sidewalk(4000) is True
    assert default_footway_as_sidewalk(4000, "forest_10000") is True
    assert default_footway_as_sidewalk(None, "sprint_2m") is True
    assert default_footway_as_sidewalk(None, "sprint_2_5m") is True
    assert default_footway_as_sidewalk(7500) is False
    assert default_footway_as_sidewalk(10000) is False
    assert default_footway_as_sidewalk(15000) is False
    assert default_footway_as_sidewalk(10000, "forest_10000") is False
    assert default_footway_as_sidewalk(15000, "mtbo_15000") is False
    assert default_footway_as_sidewalk(None, None) is False
    assert default_footway_as_sidewalk(None, "") is False
