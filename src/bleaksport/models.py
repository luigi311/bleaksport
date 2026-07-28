from pydantic import BaseModel
from pyftms import MachineType


class BaseSample(BaseModel):
    """Base class for all samples."""

    timestamp_ms: int
    distance_m: float | None = None
    power_watts: int | None = None
    altitude_m: float | None = None

    @property
    def distance_km(self) -> float | None:
        """Distance in kilometers."""
        if self.distance_m is None:
            return None
        return self.distance_m / 1000

    @property
    def distance_miles(self) -> float | None:
        """Distance in miles."""
        if self.distance_m is None:
            return None
        return self.distance_m * 0.000621371


class RunningSample(BaseSample):
    """A fused sample from RSCS and CPS."""

    speed_mps: float | None = None
    cadence_spm: int | None = None
    stride_length_m: float | None = None
    is_running: bool | None = None

    # --- Non-canonical pace targets for UI purposes ---
    @property
    def speed_kph(self) -> float | None:
        """Speed in kilometers per hour."""
        if self.speed_mps is None:
            return None
        return self.speed_mps * 3.6

    @property
    def speed_mph(self) -> float | None:
        """Speed in miles per hour."""
        if self.speed_mps is None:
            return None
        return self.speed_mps * 2.23694


class CyclingSample(BaseSample):
    """A fused sample from CSCS and CPS."""

    speed_mps: float | None = None
    cum_wheel_revs: int | None = None
    last_wheel_event_time_s: float | None = None
    cum_crank_revs: int | None = None
    last_crank_event_time_s: float | None = None
    wheel_rpm: float | None = None
    cadence_rpm: float | None = None

    # --- Non-canonical pace targets for UI purposes ---
    @property
    def speed_kph(self) -> float | None:
        """Speed in kilometers per hour."""
        if self.speed_mps is None:
            return None
        return self.speed_mps * 3.6

    @property
    def speed_mph(self) -> float | None:
        """Speed in miles per hour."""
        if self.speed_mps is None:
            return None
        return self.speed_mps * 2.23694

class TrainerSample(BaseSample):
    """Normalized indoor-training telemetry (FTMS update events).

    Fields are best-effort: many machines report only a subset.
    """

    # Common across indoor bikes / trainers
    speed_kmh: float | None = None
    cadence_spm: int | None = None
    cadence_rpm: float | None = None
    resistance_level: float | None = None
    heart_rate_bpm: float | None = None
    elapsed_s: float | None = None
    inclination: float | None = None

    target_inclination: float | None = None
    target_power: int | None = None
    target_resistance: float | None = None
    target_speed: float | None = None
    target_heart_rate: int | None = None

    # Machine meta / raw passthrough
    machine_type: MachineType | None = None

    # --- Non-canonical pace targets for UI purposes ---
    @property
    def speed_mps(self) -> float | None:
        """Speed in meters per second."""
        if self.speed_kmh is None:
            return None
        return self.speed_kmh / 3.6

    @property
    def speed_mph(self) -> float | None:
        """Speed in miles per hour."""
        if self.speed_kmh is None:
            return None
        return self.speed_kmh * 0.621371

class HeartRateSample(BaseModel):
    """Heart rate sample, typically from a chest strap."""

    timestamp_ms: int
    heart_rate_bpm: int | None = None
    rr_interval_ms: float | None = None
    energy_expended_kcal: float | None = None
