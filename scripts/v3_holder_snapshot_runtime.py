"""Atomic renewal of the independent holder source; no Dune routes or spending."""
from copy import deepcopy
from datetime import timedelta
import hashlib

from v3_credit_budget import parse_instant
from v3_holder_snapshot import (PATH, AUTHORIZATION_REF, acquire, validate_baseline,
                               SnapshotRejected, ACQUISITION_VERSION)
from v3_live_state_store import MACHINE_OWNED, StateError


def authorized(state, config):
    policy = config.get("holder_reconciliation", {})
    return (policy.get("enabled") is True and policy.get("approval_ref") == AUTHORIZATION_REF
            and state.get("holder_reconciliation_authorization", {}).get("approval_ref") == AUTHORIZATION_REF)


def activate(runtime, state):
    policy = runtime.CONFIG.get("holder_reconciliation", {})
    if policy.get("enabled") is not True or policy.get("approval_ref") != AUTHORIZATION_REF:
        raise StateError("Finalized holder source requires the approved fixed-mint policy")
    previous = state.get("holder_reconciliation_authorization")
    if previous and previous.get("approval_ref") != AUTHORIZATION_REF:
        raise StateError("Cannot replace an unrelated durable holder-source authority")
    if not previous:
        state["holder_reconciliation_authorization"] = {
            "approval_ref":AUTHORIZATION_REF,"authorized_by":"user",
            "activated_at_utc":runtime.stamp(),"dune_spending_authority":False,
            "basis":"User requested end-to-end autonomous recovery; complete same-finalized-bank account inventories, original seed retained"}
        state.setdefault("holder_reconciliation",{"schema_version":1,"baseline_history":[]})["acquisition_version"] = ACQUISITION_VERSION
        runtime.checkpoint(state)


def refresh(runtime, state, *, acquire_snapshot=None):
    if not authorized(state, runtime.CONFIG):
        return False
    lane = state.setdefault("holder_reconciliation", {"schema_version":1,"baseline_history":[]})
    now = runtime.now()
    retry = lane.get("next_retry_at_utc")
    previous_version = lane.get("acquisition_version",1)
    if retry and now < parse_instant(retry,"holder snapshot retry") and previous_version == ACQUISITION_VERSION:
        return False
    if previous_version != ACQUISITION_VERSION:
        lane.setdefault("acquisition_policy_history",[]).append({"previous_version":previous_version,
            "version":ACQUISITION_VERSION,"changed_at_utc":runtime.stamp(),
            "previous_retry_at_utc":retry,"previous_error":lane.get("last_error"),
            "basis":"Bounded exact-bank alignment retry upgrade; no relaxation of snapshot acceptance"})
        lane["acquisition_version"] = ACQUISITION_VERSION
    last = lane.get("last_success_at_utc")
    cadence = int(runtime.CONFIG["holder_reconciliation"].get("cadence_minutes",120))
    quality = runtime.load(runtime.ROOT/"data/current/xnet_holder_integrity.json", {})
    if (last and lane.get("active_baseline_id") and quality.get("status") == "HEALTHY"
            and now < parse_instant(last,"holder snapshot success")+timedelta(minutes=cadence)):
        return False
    try:
        baseline = (acquire_snapshot or acquire)(now=runtime.now)
        owners, receipt = validate_baseline(baseline)
        if lane.get("active_slot") is not None and receipt["slot"] < lane["active_slot"]:
            raise SnapshotRejected("Finalized holder source regressed its accepted slot")
        if (receipt["slot"] == lane.get("active_slot")
                and baseline["baseline_id"] != lane.get("active_baseline_id")):
            # Acquisition timestamps can differ at the same immutable bank, but
            # owner state and supply may not. Reuse the accepted generation.
            old = runtime.load(runtime.ROOT/PATH,{})
            old_owners, old_receipt = validate_baseline(old)
            if owners != old_owners or receipt["mint_supply_raw"] != old_receipt["mint_supply_raw"]:
                raise SnapshotRejected("Conflicting holder state at an already accepted finalized slot")
            baseline, receipt = old, old_receipt
    except (SnapshotRejected, OSError) as error:
        lane.update(last_attempt_at_utc=runtime.stamp(),last_error=str(error),
                    next_retry_at_utc=(now+timedelta(hours=1)).isoformat().replace("+00:00","Z"))
        state.setdefault("source_errors",{})["holder_snapshot"] = {
            "error":str(error),"automatic_recheck":True,"next_retry_at_utc":lane["next_retry_at_utc"]}
        runtime.checkpoint(state)
        return False

    root = runtime.ROOT
    before = deepcopy(state)
    originals = {path:(root/path).read_bytes() if (root/path).exists() else None for path in MACHINE_OWNED}
    immutable_seed = (root/"data/xnet_owner_balances_seed.csv").read_bytes()
    immutable_metadata = (root/"data/xnet_owner_balances_seed_metadata.json").read_bytes()
    canonical = (root/"data/canonical/xnet_transfers.csv").read_bytes()
    seed_hash = hashlib.sha256(immutable_seed).hexdigest()
    expected_seed = (lane.get("original_seed_sha256")
                     or state.get("holder_seed_review",{}).get("seed_sha256") or seed_hash)
    if expected_seed != seed_hash:
        raise StateError("Original holder seed changed; source migration cannot overwrite its provenance")
    publishing = False
    try:
        runtime.save(root/PATH,baseline)
        lane.update(active_baseline_id=baseline["baseline_id"],active_slot=receipt["slot"],
                    original_seed_sha256=seed_hash,
                    original_seed_metadata_sha256=hashlib.sha256(immutable_metadata).hexdigest(),
                    last_success_at_utc=runtime.stamp())
        lane.pop("next_retry_at_utc",None);lane.pop("last_error",None)
        if not any(row.get("baseline_id")==baseline["baseline_id"] for row in lane["baseline_history"]):
            lane["baseline_history"].append({"baseline_id":baseline["baseline_id"],**receipt})
        review = state.get("holder_seed_review",{})
        if review.get("status") == "requires_review":
            review.update(status="resolved_by_verified_finalized_snapshot",resolution={
                "resolved_at_utc":runtime.stamp(),"baseline_id":baseline["baseline_id"],
                "original_seed_retained":True,"historical_seed_not_claimed_repaired":True,
                "basis":"Complete current holder inventory independently conserves mint supply at the same finalized slot"})
        state.setdefault("source_errors",{}).pop("holder_snapshot",None)
        runtime.save(runtime.STATE,state)  # Local only; reducer binds to this candidate identity.
        runtime.run("v3_reduce_chain.py","--rebuild-canonical","--allow-stale-holders")
        quality = runtime.load(root/"data/current/xnet_holder_integrity.json",{})
        holder = runtime.load(root/"data/current/xnet_holder_state.json",{})
        if (quality.get("status")!="HEALTHY" or quality.get("baseline_id")!=baseline["baseline_id"]
                or holder.get("baseline_id")!=baseline["baseline_id"]
                or holder.get("positive_holder_count")!=receipt["positive_owner_count"]
                or holder.get("mint_conservation_residual_raw")!="0"):
            raise StateError("Independent holder snapshot did not produce matching healthy derived evidence")
        if ((root/"data/xnet_owner_balances_seed.csv").read_bytes()!=immutable_seed
                or (root/"data/xnet_owner_balances_seed_metadata.json").read_bytes()!=immutable_metadata
                or (root/"data/canonical/xnet_transfers.csv").read_bytes()!=canonical):
            raise StateError("Holder source renewal changed immutable seed/canonical financial evidence")
        runtime.sync_holder_integrity_error(state)
        state["forced_presentations"] = sorted(set(state.get("forced_presentations",[]))|{"current","holders"})
        health = runtime.load(runtime.HEALTH,{})
        health.update(status="DEGRADED" if state.get("source_errors") else "HEALTHY",
                      degraded_sources=deepcopy(state.get("source_errors",{})),
                      holder_snapshot_completed_at_utc=runtime.stamp())
        runtime.save(runtime.HEALTH,health)
        for script in ("v3_build_chain_snapshot.py","v3_build_presentation_history.py","v3_build_live_bundle.py"):
            runtime.run(script)
        runtime.write_operational_health(state)
        runtime.save(runtime.STATE,state)
        if (state.get("financial_journal",{}).get("obligations")!=before.get("financial_journal",{}).get("obligations")
                or state.get("standing_credit_budget")!=before.get("standing_credit_budget")
                or state.get("queries")!=before.get("queries")):
            raise StateError("Free holder-source renewal changed spending/accepted-query facts")
        publishing = True
        runtime.publish()  # Proof, owners, quality, journal and bundle in ONE exact-base generation.
        return True
    except Exception:
        if not publishing:
            for path,raw in originals.items():
                target = root/path
                if raw is None:
                    target.unlink(missing_ok=True)
                else:
                    target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(raw)
            state.clear();state.update(before)
        raise  # An ambiguous publication must never be compensated by an old state.
