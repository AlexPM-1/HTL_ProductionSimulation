"""
api/routes_policy.py
=======================
GET/PATCH /api/mixed/push_policy. Reads/writes the active run's push
policy via api.runs.run_store.

PushPolicyConfig lives in domain.policy. apply_push_policy (imported from
sim.runner) is the sim-side wiring that re-syncs frozen_zone_cards onto
every live KanbanChuteResource whenever the policy changes.
"""

from __future__ import annotations

from fastapi import HTTPException

from domain.policy import PushPolicyConfig
from sim.runner import apply_push_policy

from api.app import app
from api.runs import run_store
from api.schemas import PushPolicyPatchRequest


@app.get("/api/mixed/push_policy")
def get_push_policy():
    """
    Current push policy. If a mixed run has already happened, this
    reflects the LIVE ctx.policy that push_dispatch_process/
    push_drain_process are actually reading from ("live": true) — the
    same object PATCH below edits. Otherwise it falls back to
    PushPolicyConfig()'s bare defaults ("live": false), purely so a
    settings panel has sensible starting values before the first run.
    """
    kenv = run_store.kenv
    if kenv is not None and getattr(kenv, "push_policy", None) is not None:
        policy = kenv.push_policy
        live = True
    else:
        policy = PushPolicyConfig()
        live = False
    d = policy.to_dict()
    d["frozen_zone_hours"] = policy.frozen_zone_hours
    d["live"] = live
    return d


@app.patch("/api/mixed/push_policy")
def patch_push_policy(req: PushPolicyPatchRequest):
    """
    Partial update of the ACTIVE run's push policy. Requires a prior
    POST /api/simulate_mixed — there is no ctx to edit before that (use
    GET /api/mixed/push_policy beforehand if the UI just wants to know
    what values to preset a form with).

    Builds a full PushPolicyConfig from (current values + provided
    overrides) so the cross-field validation in
    PushPolicyConfig.__post_init__ (max_lead_time_h >= ideal_lead_time_h,
    frozen_zone_cards >= 0) runs against the RESULTING policy, not just
    the fields touched by this request. Then calls apply_push_policy()
    so frozen_zone_cards actually re-syncs onto every chute — see that
    function's docstring for exactly what's live-immediately vs. applies
    only to new orders.
    """
    kenv, _cfg_unused = run_store.require()
    ctx = getattr(kenv, "push_ctx", None)
    if ctx is None:
        raise HTTPException(
            status_code=409,
            detail="Active run has no push_ctx — sim.runner.run_mixed() may be out of sync with this server.",
        )

    updates = req.model_dump(exclude_none=True) if hasattr(req, "model_dump") else req.dict(exclude_none=True)
    merged = ctx.policy.to_dict()
    merged.update(updates)
    try:
        new_policy = PushPolicyConfig.from_dict(merged)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    apply_push_policy(ctx, new_policy)
    kenv.push_policy = ctx.policy  # keep the kenv-level mirror in sync, same spot run_mixed() sets it

    d = ctx.policy.to_dict()
    d["frozen_zone_hours"] = ctx.policy.frozen_zone_hours
    d["live"] = True
    return d
