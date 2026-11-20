"""Tunable settings for matching XD segments to OSM edges."""
import dataclasses
import math
import numbers
import tomllib
from pathlib import Path


@dataclasses.dataclass(frozen=True)
class MatchConfig:
    """Matching settings. Every distance is in meters.

    band_half_width_m: half-width of the band drawn around each XD line, cut square at its ends.
    min_overlap_m: how far an edge must run along its XD inside the band (half the XD's length
        is enough for XDs shorter than twice this).
    fallback_below_coverage: XDs whose way-ID matches cover less than this share of their length
        are searched again by geometry. 0 turns the fallback off.
    hull_buffer_m: margin added around a county's XDs for the OSM download.
    reserved_cores: CPU cores parallel() leaves free.
    """

    band_half_width_m: float = 15.0
    min_overlap_m: float = 10.0
    fallback_below_coverage: float = 0.5
    hull_buffer_m: float = 200.0
    reserved_cores: int = 2

    def __post_init__(self):
        for name in ("band_half_width_m", "min_overlap_m", "fallback_below_coverage", "hull_buffer_m"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, numbers.Real):
                raise TypeError(f"MatchConfig.{name} must be a number, got {type(value).__name__} {value!r}")
            object.__setattr__(self, name, float(value))
        if isinstance(self.reserved_cores, bool) or not isinstance(self.reserved_cores, numbers.Integral):
            raise TypeError(f"MatchConfig.reserved_cores must be an integer, got "
                            f"{type(self.reserved_cores).__name__} {self.reserved_cores!r}")
        object.__setattr__(self, "reserved_cores", int(self.reserved_cores))

        checks = [
            ("band_half_width_m", self.band_half_width_m > 0, "a finite number above 0"),
            ("min_overlap_m", self.min_overlap_m > 0, "a finite number above 0"),
            ("fallback_below_coverage", 0 <= self.fallback_below_coverage <= 1, "between 0 and 1"),
            ("hull_buffer_m", self.hull_buffer_m >= 0, "a finite number of at least 0"),
            ("reserved_cores", self.reserved_cores >= 0, "at least 0"),
        ]
        for name, ok, rule in checks:
            value = getattr(self, name)
            if not ok or (isinstance(value, float) and not math.isfinite(value)):
                raise ValueError(f"MatchConfig.{name} must be {rule}, got {value!r}")

    @classmethod
    def from_toml(cls, path):
        """Read settings from a TOML file of top-level keys; missing keys keep their defaults."""
        try:
            values = tomllib.loads(Path(path).read_text())
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise ValueError(f"{path}: cannot read TOML settings: {exc}") from exc
        known = {field.name for field in dataclasses.fields(cls)}
        unknown = sorted(set(values) - known)
        if unknown:
            raise ValueError(f"{path}: unknown settings {unknown}; known settings are {sorted(known)}")
        try:
            return cls(**values)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{path}: {exc}") from exc
