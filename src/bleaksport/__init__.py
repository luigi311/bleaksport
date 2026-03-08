"""BLEAKSport: Bluetooth LE support for fitness sensors."""

from pyftms import MachineType

from bleaksport.cycling import CyclingMux, CyclingSession
from bleaksport.discover import (
    discover_cycling_devices,
    discover_ftms_devices,
    discover_heart_rate_devices,
    discover_power_devices,
    discover_running_devices,
    discover_speed_cadence_devices,
)
from bleaksport.heart_rate import HeartRateMux, HeartRateSession
from bleaksport.models import CyclingSample, HeartRateSample, RunningSample, TrainerSample
from bleaksport.running import RunningMux, RunningSession
from bleaksport.trainer import TrainerMux
