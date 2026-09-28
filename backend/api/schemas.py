"""
api/schemas.py
=================
Pydantic request models for api/legacy.py and api/routes_policy.py.
Constant defaults (DEFAULT_N_WORKERS, DAY_START_HOUR, DAY_LENGTH_S,
DRAIN_DAYS, PUSH_CARD_SIZE) are sourced from domain.constants, the
single canonical home for these values.
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field

from domain.constants import DEFAULT_N_WORKERS, DAY_START_HOUR, DAY_LENGTH_S, DRAIN_DAYS, PUSH_CARD_SIZE


class PushPolicyPatchRequest(BaseModel):
    """All fields optional — unset ones keep the current policy's value.
    Mirrors PushPolicyConfig's own fields 1:1 (see that class for what
    each knob does and which are live-immediately vs. need this call vs.
    apply only to new orders — same caveats as apply_push_policy())."""
    frozen_zone_cards: Optional[int] = Field(default=None, ge=0)
    card_production_time_min: Optional[float] = Field(default=None, gt=0)
    push_visibility_days: Optional[int] = Field(default=None, ge=0)
    ideal_lead_time_h: Optional[float] = Field(default=None, ge=0)
    max_lead_time_h: Optional[float] = Field(default=None, ge=0)
    rush_threshold_h: Optional[float] = Field(default=None, ge=0)
    retry_interval_h: Optional[float] = Field(default=None, gt=0)


class SimulateMixedRequest(BaseModel):
    n_workers: int = Field(default=DEFAULT_N_WORKERS, ge=1, le=2)
    n_crews: int = Field(
        default=2, ge=1,
        description="How many crew_process() workers (sim.drain.crew) "
                     "share the plant's lines. A crew holds at most one "
                     "line at a time, so more crews than lines just "
                     "leaves some crews idle; see /api/parameters' "
                     "crew_options for a sane upper bound.",
    )
    seed: int = 42
    day_start_hour: int = Field(default=DAY_START_HOUR, ge=0, le=23)
    day_length_s: float = DAY_LENGTH_S
    drain_days: int = Field(default=DRAIN_DAYS, ge=0, le=7)
    push_card_size: int = Field(
        default=PUSH_CARD_SIZE, ge=1,
        description="Max pieces per push OrderRecord slice before class-1 "
                     "gets another chance to jump the gate queue.",
    )
    horizon_s: Optional[float] = Field(
        default=None,
        description="Override the simulation horizon. Default: "
                     "run_mixed()'s own estimate (max of kanban's "
                     "estimate_horizon_s() and last push date + "
                     "drain_days).",
    )
    time_unit: str = "h"  # for gantt / card_flow / line_units_series: "h" | "min" | "s"
