from copy import deepcopy
from datetime import datetime,timedelta,timezone
import hashlib,json
from pathlib import Path
import sys,unittest
import yaml

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"scripts"))
from v3_autonomy_watchdog import evaluate
NOW=datetime(2026,10,23,6,tzinfo=timezone.utc)
def at(hours=0):return (NOW-timedelta(hours=hours)).isoformat().replace("+00:00","Z")
def fixture():
    config={"sources":[{"key":"xnet_transfers","cadence_minutes":120},
                       {"key":"bbb_dex","cadence_minutes":1440}],
            "presentation":[{"key":"current","cadence_minutes":60}]}
    state={"schema_version":1,"queries":{key:{"completed_at_utc":at()} for key in ("xnet_transfers","bbb_dex","current")}}
    return state,config,{"status":"HEALTHY"}

class WatchdogTests(unittest.TestCase):
    def check(self,state=None,config=None,quality=None,**kwargs):
        s,c,q=fixture();return evaluate(state if state is not None else s,config or c,quality or q,NOW,**kwargs)
    def test_fresh_collector_is_healthy_when_last_chain_event_is_old(self):
        s,c,q=fixture();s["queries"]["xnet_transfers"]["source_asof"]="2026-10-21T00:00:00Z"
        self.assertEqual(self.check(s)["status"],"healthy")
    def test_stale_collection_cannot_be_hidden_by_fresh_generated_time(self):
        s,_,_=fixture();s["queries"]["xnet_transfers"].update(completed_at_utc=at(8),generated_at_utc=at())
        self.assertEqual(self.check(s)["status"],"unhealthy")
    def test_verified_frontier_not_retrieval_time_is_the_coverage_clock(self):
        s,_,_=fixture();s["queries"]["xnet_transfers"]["continuous_coverage_end_utc"]=at(8)
        self.assertEqual(self.check(s)["status"],"unhealthy")
    def test_pacing_is_respected_without_unbounded_threshold_extension(self):
        s,_,_=fixture();s["queries"]["xnet_transfers"]["completed_at_utc"]=at(8)
        s["standing_credit_budget"]={"last_decision":{"cadence_multiplier":2}}
        self.assertEqual(self.check(s)["status"],"healthy")
        s["standing_credit_budget"]["last_decision"]["cadence_multiplier"]=1000
        s["queries"]["xnet_transfers"]["completed_at_utc"]=at(100)
        self.assertEqual(self.check(s)["status"],"unhealthy")
    def test_transient_backoff_warns_but_persistent_hold_fails(self):
        s,_,_=fixture();s["source_errors"]={"xnet_transfers":{"error":"503","automatic_recheck":True}}
        self.assertEqual(self.check(s)["status"],"healthy");self.assertTrue(self.check(s)["warnings"])
        s["source_errors"]["xnet_transfers"]["manual_reconciliation_required"]=True
        self.assertEqual(self.check(s)["status"],"unhealthy")
    def test_holder_deficit_or_missing_integrity_is_not_a_green_service(self):
        for q in ({"status":"DEGRADED","error":"negative balance"},{"status":"unknown"}):
            self.assertEqual(self.check(quality=q)["status"],"unhealthy")
    def test_explicit_domain_hold_is_not_hidden_by_healthy_quality_file(self):
        s,_,_=fixture();s["domain_holds"]={"holders":{"error":"unresolved original hold"}}
        self.assertEqual(self.check(s)["status"],"unhealthy")
    def test_opening_review_is_not_hidden_by_a_later_positive_balance(self):
        s,_,_=fixture();s["holder_seed_review"]={"status":"requires_review","error":"Missing pre-seed opening requires review"}
        self.assertEqual(self.check(s)["status"],"unhealthy")
        self.assertTrue(any("pre-seed" in problem for problem in self.check(s)["problems"]))
    def test_complete_holder_source_needs_matching_proof_and_actual_snapshot_freshness(self):
        from v3_holder_snapshot import AUTHORIZATION_REF
        s,c,q=fixture();s["holder_reconciliation"]={"active_baseline_id":"bank-proof"}
        s["holder_reconciliation_authorization"]={"approval_ref":AUTHORIZATION_REF}
        q.update(baseline_id="bank-proof",holder_data_as_of_utc=at(),mint_conservation_residual_raw="0")
        self.assertEqual(self.check(s,c,q)["status"],"healthy")
        q["holder_data_as_of_utc"]=at(7)
        self.assertEqual(self.check(s,c,q)["status"],"unhealthy")
        q["holder_data_as_of_utc"]=at();q["baseline_id"]="other-proof"
        self.assertEqual(self.check(s,c,q)["status"],"unhealthy")
    def test_authorized_holder_bootstrap_is_not_satisfied_by_an_old_seed(self):
        from v3_holder_snapshot import AUTHORIZATION_REF
        s,c,q=fixture();s["holder_reconciliation_authorization"]={"approval_ref":AUTHORIZATION_REF}
        self.assertEqual(self.check(s,c,q)["status"],"unhealthy")
    def test_ambiguous_submission_fails_without_clearing_it(self):
        s,_,_=fixture();s["pending_executions"]={"current":{"execution_id":None}}
        old=deepcopy(s);self.assertEqual(self.check(s)["status"],"unhealthy");self.assertEqual(s,old)
    def test_independent_reader_does_not_false_alarm_on_fresh_pre_post_intent(self):
        s,_,_=fixture();s["pending_executions"]={"current":{"execution_id":None,"submitted_at_utc":at()}}
        self.assertEqual(self.check(s)["status"],"healthy")
        self.assertTrue(self.check(s)["warnings"])
        s["pending_executions"]["current"]["submission_error"]="lost response"
        self.assertEqual(self.check(s)["status"],"unhealthy")
        s["pending_executions"]["current"].pop("submission_error")
        s["pending_executions"]["current"]["submitted_at_utc"]=at(1)
        self.assertEqual(self.check(s)["status"],"unhealthy")
    def test_historical_terminal_failure_does_not_block_current_collector(self):
        s,_,_=fixture();s["pending_executions"]={"old":{"execution_id":"known-failed","terminal_status":{"state":"QUERY_STATE_FAILED"}}}
        self.assertEqual(self.check(s)["status"],"healthy")
    def test_budget_denial_warns_and_never_changes_authority(self):
        s,_,_=fixture();s["budget_decision"]={"status":"blocked"};old=deepcopy(s)
        self.assertEqual(self.check(s)["status"],"healthy");self.assertTrue(self.check(s)["warnings"])
        self.assertEqual(s,old)
    def test_policy_hash_mismatch_fails_read_only(self):
        s,c,_=fixture();c["autonomous_operation"]={"enabled":True,"approval_ref":"user"}
        self.assertEqual(self.check(s,c)["status"],"unhealthy")
        s["standing_authorization"]={"approval_ref":"user","policy_sha256":hashlib.sha256(json.dumps(c["autonomous_operation"],sort_keys=True,separators=(",", ":")).encode()).hexdigest()}
        self.assertEqual(self.check(s,c)["status"],"healthy")
    def test_daily_source_has_a_separate_grace_window(self):
        s,_,_=fixture();s["queries"]["bbb_dex"]["completed_at_utc"]=at(48)
        self.assertEqual(self.check(s)["status"],"healthy")
        s["queries"]["bbb_dex"]["completed_at_utc"]=at(73)
        self.assertEqual(self.check(s)["status"],"unhealthy")
    def test_independent_feeds_are_checked_without_invoking_them(self):
        ext={"market":{"fetched_at_utc":at()},"network":{"collected_at_utc":at(24)},"revenue":{"generated_at_utc":at(24)}}
        self.assertEqual(self.check(external=ext)["status"],"healthy")
        ext["market"]["fetched_at_utc"]=at(8)
        self.assertEqual(self.check(external=ext)["status"],"unhealthy")
    def test_future_or_missing_collector_clocks_fail(self):
        for value in (None,"not-a-time",at(-1)):
            s,_,_=fixture();s["queries"]["xnet_transfers"]["completed_at_utc"]=value
            self.assertEqual(self.check(s)["status"],"unhealthy")
    def test_lost_activated_book_is_not_just_a_budget_warning(self):
        s,c,q=fixture();c["autonomous_operation"]={"enabled":True,"approval_ref":"user"}
        s["standing_authority_activation"]={"approved":{"predecessor_revision":"known"}}
        report=self.check(s,c)
        self.assertTrue(any("risk book" in problem for problem in report["problems"]))
    def test_observer_is_independent_read_only_and_has_no_dune_secret(self):
        path=ROOT/".github/workflows/monitor-v3-health.yml"
        workflow=yaml.safe_load(path.read_text())
        self.assertEqual(workflow["permissions"],{"contents":"read"})
        self.assertNotIn("concurrency",workflow)
        self.assertNotIn("DUNE_API_KEY",path.read_text())
        self.assertNotIn("publish_live_state",path.read_text())
        self.assertIn("v3_autonomy_watchdog.py",path.read_text())
    def test_label_cli_install_is_version_checksum_pinned_without_dune_key(self):
        path=ROOT/".github/workflows/sync-dune-revenue.yml"
        workflow=yaml.safe_load(path.read_text())
        install=next(step for step in workflow["jobs"]["sync"]["steps"] if step.get("name")=="Install checksum-pinned Dune CLI")
        body=install["run"]
        self.assertIn("/download/v0.1.18/",body)
        self.assertIn("1b60f0df45e8d1827ba8ec9c85217ab2357ac2c3fc5a5b0e9738e7827c926dd9",body)
        self.assertLess(body.index("sha256sum --check"),body.index("tar -xzf"))
        self.assertNotIn("DUNE_API_KEY",install.get("env",{}))
        self.assertNotIn("raw/main/install.sh",path.read_text())
        self.assertNotIn("| bash",body)

if __name__=="__main__":unittest.main()
