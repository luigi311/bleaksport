from typing import Any

from bleaksport.models import CyclingSample, RunningSample, TrainerSample


def merged_value(
    new: RunningSample | CyclingSample | TrainerSample,
    last: RunningSample | CyclingSample | TrainerSample,
    field: str,
) -> Any:
    """Helper to merge a single field from new and last samples, preferring new if not None."""
    v_new = getattr(new, field)
    if v_new is not None:
        return v_new

    return getattr(last, field)
