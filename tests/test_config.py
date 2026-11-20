import math

import numpy as np
import pytest

FLOAT_FIELDS = ["band_half_width_m", "min_overlap_m", "fallback_below_coverage", "hull_buffer_m"]


@pytest.fixture
def MatchConfig():
    from geo_mapper_api.config import MatchConfig
    return MatchConfig


def test_defaults(MatchConfig):
    config = MatchConfig()
    assert (config.band_half_width_m, config.min_overlap_m, config.fallback_below_coverage,
            config.hull_buffer_m, config.reserved_cores) == (15.0, 10.0, 0.5, 200.0, 2)


def test_exported_from_the_package(MatchConfig):
    import geo_mapper_api
    assert geo_mapper_api.MatchConfig is MatchConfig


@pytest.mark.parametrize("field", FLOAT_FIELDS)
@pytest.mark.parametrize("value", ["15", True, None])
def test_float_fields_reject_wrong_types(MatchConfig, field, value):
    with pytest.raises(TypeError, match=field):
        MatchConfig(**{field: value})


@pytest.mark.parametrize("value", [2.0, True, "2"])
def test_reserved_cores_rejects_wrong_types(MatchConfig, value):
    with pytest.raises(TypeError, match="reserved_cores"):
        MatchConfig(reserved_cores=value)


@pytest.mark.parametrize("field, value", [
    ("band_half_width_m", 0), ("band_half_width_m", -1), ("band_half_width_m", math.nan), ("band_half_width_m", math.inf),
    ("min_overlap_m", 0), ("min_overlap_m", math.nan),
    ("fallback_below_coverage", -0.1), ("fallback_below_coverage", 1.1), ("fallback_below_coverage", math.nan),
    ("hull_buffer_m", -1), ("hull_buffer_m", math.inf),
    ("reserved_cores", -1),
])
def test_bad_values_name_the_field(MatchConfig, field, value):
    with pytest.raises(ValueError, match=field):
        MatchConfig(**{field: value})


def test_ints_are_accepted_for_float_fields(MatchConfig):
    config = MatchConfig(band_half_width_m=20, hull_buffer_m=np.int64(0), reserved_cores=np.int64(3))
    assert config.band_half_width_m == 20.0 and isinstance(config.band_half_width_m, float)
    assert config.hull_buffer_m == 0.0 and isinstance(config.hull_buffer_m, float)
    assert config.reserved_cores == 3


def test_from_toml_reads_every_field(MatchConfig, tmp_path):
    path = tmp_path / "matching.toml"
    path.write_text("band_half_width_m = 20.5\nmin_overlap_m = 12\nfallback_below_coverage = 0.25\n"
                    "hull_buffer_m = 300\nreserved_cores = 4\n")
    config = MatchConfig.from_toml(path)
    assert (config.band_half_width_m, config.min_overlap_m, config.fallback_below_coverage,
            config.hull_buffer_m, config.reserved_cores) == (20.5, 12.0, 0.25, 300.0, 4)


def test_from_toml_partial_file_keeps_defaults(MatchConfig, tmp_path):
    path = tmp_path / "matching.toml"
    path.write_text("band_half_width_m = 15\n")
    assert MatchConfig.from_toml(path) == MatchConfig()


@pytest.mark.parametrize("text, message", [
    ("band_half_width_m = true\n", "band_half_width_m"),
    ("band_width = 5\n", "band_width"),
    ("band_half_width_m = \n", "TOML"),
])
def test_from_toml_errors_name_the_file(MatchConfig, tmp_path, text, message):
    path = tmp_path / "matching.toml"
    path.write_text(text)
    with pytest.raises(ValueError) as err:
        MatchConfig.from_toml(path)
    assert str(path) in str(err.value) and message in str(err.value)


def test_from_toml_missing_file(MatchConfig, tmp_path):
    path = tmp_path / "nowhere.toml"
    with pytest.raises(ValueError, match="nowhere.toml"):
        MatchConfig.from_toml(path)
