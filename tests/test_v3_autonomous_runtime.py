from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT / "scripts"))
spec = importlib.util.spec_from_file_location("autonomous_refresh_test",ROOT / "scripts/v3_refresh_dashboard.py")
refresh = importlib.util.module_from_spec(spec)
spec.loader.exec_module(refresh)
NOW = datetime(2026,10,20,12,tzinfo=timezone.utc)
USAGE = {"credits_used":1803.249,"credits_included":4000,"period_start_utc":"2026-10-04T16:35:49Z",
         "period_end_utc":"2026-11-04T00:00:00Z","exact_end":True}


def authorized_state():
    return {"schema_version":1,"queries":{},"execution_history":[],"pending_executions":{},
        "standing_authorization":{"approval_ref":refresh.CONFIG["autonomous_operation"]["approval_ref"],
            "policy_sha256":refresh.standing_authority_hash(refresh.CONFIG["autonomous_operation"])}}


class AutonomousRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.now = patch.object(refresh,"now",return_value=NOW)
        self.now.start(); self.addCleanup(self.now.stop)
        self.state = authorized_state()
        self.metadata = patch.object(refresh,"DuneMetadataClient")
        client = self.metadata.start();self.addCleanup(self.metadata.stop)
        self.client=client.return_value
        self.client.get_usage.return_value=deepcopy(USAGE)
        self.forecast = patch.object(refresh,"standing_forecast",return_value={"expected_credits":200,"entries":[]})
        self.forecast.start();self.addCleanup(self.forecast.stop)

    def quote(self,cost=2):
        return {"status":"estimated","cost_estimate_credits":cost,"expected_credits":0.3,
                "evidence":{"sample_execution_ids":["observed"],"method":"sample_backed"}}

    def current_sql(self):
        return (ROOT / "dune/v3/sql/60_live_current_snapshot.sql").read_text()

    def test_config_alone_is_not_standing_authority(self):
        self.assertFalse(refresh.autonomous_enabled({}))
        self.assertTrue(refresh.autonomous_enabled(self.state))
        self.state["standing_authorization"]["policy_sha256"]="0"*64
        with self.assertRaises(refresh.BudgetBlocked):refresh.autonomous_enabled(self.state)

    def test_exact_mcp_metadata_replaces_date_only_usage_endpoint(self):
        with patch.object(refresh,"api") as api:
            billing=refresh.usage_guard(self.state)
        api.assert_not_called()
        self.assertEqual(billing["period_end"],USAGE["period_end_utc"])
        self.assertEqual(self.state["budget_decision"]["status"],"admitted")

    def test_billing_outage_is_an_automatic_recheck_not_a_permanent_service_pause(self):
        self.client.get_usage.side_effect=refresh.DuneMetadataError("temporary provider outage")
        with self.assertRaises(refresh.BudgetBlocked):refresh.usage_guard(self.state)
        self.assertTrue(self.state["budget_decision"]["automatic_recheck"])
        self.assertFalse(self.state.get("paused",False))

    def test_main_preserves_free_service_during_billing_outage(self):
        self.client.get_usage.side_effect=refresh.DuneMetadataError("temporary provider outage")
        config={**refresh.CONFIG,"sources":[],"presentation":[],"reconciliation":None}
        with tempfile.TemporaryDirectory() as directory, patch.object(refresh,"CONFIG",config), \
             patch.object(refresh,"load_authoritative_state",return_value=self.state), \
             patch.object(refresh,"HEALTH",Path(directory)/"missing.json"),patch.object(refresh,"save"), \
             patch.object(refresh,"sync_independent_network") as independent, \
             patch.object(refresh,"publish_data_generation"),patch.object(refresh,"checkpoint"), \
             patch("sys.argv",["refresh"]):
            self.assertEqual(refresh.main(),0)
        self.assertFalse(self.state["paused"])
        independent.assert_called_once_with(self.state)

    def test_quote_and_exact_authority_are_durable_before_execute(self):
        posted=[];checkpoints=[]
        def api(path,payload=None):
            if path == "query/8894185":
                return {"query_id":8894185,"query_sql":self.current_sql(),"version":18}
            if path.endswith("/execute"):
                pending=self.state["pending_executions"]["current"]
                self.assertTrue(checkpoints)
                self.assertEqual(pending["operation_approval"]["quote_credits"],2)
                self.assertEqual(pending["operation_approval"]["sql_sha256"],hashlib.sha256(self.current_sql().encode()).hexdigest())
                self.assertIn(pending["financial_attempt_id"],next(iter(self.state["standing_credit_budget"]["periods"].values()))["operations"])
                posted.append(path)
                return {"execution_id":"one_exact_execution"}
            return {"state":"QUERY_STATE_COMPLETED","execution_cost_credits":0.4,
                "execution_ended_at":"2026-10-20T12:00:00Z","result_metadata":{"total_row_count":5}}
        with patch.object(refresh,"api",side_effect=api),patch.object(refresh,"checkpoint",side_effect=lambda state:checkpoints.append(deepcopy(state))), \
             patch.object(refresh,"autonomous_estimate",return_value=self.quote()):
            result=refresh.execute({"key":"current","query_id":8894185,"max_run_credits":0.1},state=self.state)
        self.assertEqual(result["execution_id"],"one_exact_execution")
        self.assertEqual(len(posted),1)
        self.assertIn("one_exact_execution",self.state["cost_outliers"])

    def test_sql_drift_blocks_before_any_paid_post(self):
        with patch.object(refresh,"api",return_value={"query_id":8894185,"query_sql":"SELECT 1","version":19}) as api:
            with self.assertRaises(refresh.BudgetBlocked):
                refresh.execute({"key":"current","query_id":8894185,"max_run_credits":1.5},state=self.state)
        self.assertEqual([c.args[0] for c in api.call_args_list],["query/8894185"])
        self.assertFalse(self.state["pending_executions"])

    def test_operation_estimate_above_authorized_scope_never_submits(self):
        with patch.object(refresh,"api",return_value={"query_id":8894185,"query_sql":self.current_sql(),"version":18}) as api, \
             patch.object(refresh,"autonomous_estimate",return_value=self.quote(30)):
            with self.assertRaises(refresh.BudgetBlocked):
                refresh.execute({"key":"current","query_id":8894185,"max_run_credits":1.5},state=self.state)
        self.assertFalse(any(c.args[0].endswith("/execute") for c in api.call_args_list))

    def test_known_result_recovers_without_new_quote_or_submission(self):
        self.state["pending_executions"]["current"]={"key":"current","query_id":8894185,
            "query_parameters":{},"execution_id":"already_paid","submitted_at_utc":"2026-10-20T11:00:00Z",
            "timeout_seconds":180,"financial_attempt_id":"old_attempt","post_run_threshold_credits":1.5,"billing":{},
            "terminal_status":{"state":"QUERY_STATE_COMPLETED","execution_cost_credits":2,
                               "execution_ended_at":"2026-10-20T11:01:00Z","result_metadata":{}}}
        with patch.object(refresh,"api") as api, patch.object(refresh,"checkpoint"), \
             patch.object(refresh,"autonomous_estimate") as quote:
            result=refresh.execute({"key":"current","query_id":8894185,"max_run_credits":1.5},state=self.state)
        api.assert_not_called();quote.assert_not_called()
        self.assertEqual(result["execution_id"],"already_paid")

    def test_budget_pacing_does_not_defer_daily_independent_domains(self):
        self.state["standing_credit_budget"]={"last_decision":{"cadence_multiplier":3}}
        self.assertEqual(refresh.cadence_multiplier(self.state,"current"),3)
        self.assertEqual(refresh.cadence_multiplier(self.state,"xnet_transfers"),3)
        self.assertEqual(refresh.cadence_multiplier(self.state,"bbb_dex"),1)
        self.assertEqual(refresh.cadence_multiplier(self.state,"network"),1)

    def test_cached_only_entry_never_uses_standing_metadata_or_paid_routes(self):
        with patch.object(refresh,"load_authoritative_state",return_value=self.state), \
             patch.object(refresh,"refresh_independent_data",return_value=0) as independent, \
             patch.object(refresh,"usage_guard") as guard,patch("sys.argv",["refresh","--cached-only"]):
            self.assertEqual(refresh.main(),0)
        guard.assert_not_called();independent.assert_called_once_with(self.state)

    def test_transfer_catchup_retains_four_hours_of_indexing_overlap(self):
        source=deepcopy(refresh.CONFIG["sources"][0])
        config={**refresh.CONFIG,"sources":[source],"presentation":[],"reconciliation":None}
        self.state.update(reconciliation={"generation":1},chain_repaired_at_utc="2026-10-20T12:00:00Z",
                          chain_completed_at_utc="2026-10-20T08:00:00Z")
        self.state["queries"]["xnet_transfers"]={"completed_at_utc":"2026-10-20T08:00:00Z","query_parameters":{"lookback_hours":6}}
        record={"query_id":8894037,"query_parameters":{"lookback_hours":8},"execution_id":"fresh-source"}
        with tempfile.TemporaryDirectory() as directory,patch.object(refresh,"CONFIG",config), \
             patch.object(refresh,"load_authoritative_state",return_value=self.state), \
             patch.object(refresh,"HEALTH",Path(directory)/"missing.json"),patch.object(refresh,"save"), \
             patch.object(refresh,"sync_independent_network"),patch.object(refresh,"usage_guard",return_value={}), \
             patch.object(refresh,"publish_data_generation"),patch.object(refresh,"checkpoint"), \
             patch.object(refresh,"execute",return_value=record) as execute, \
             patch.object(refresh,"export_source",return_value=1000),patch("sys.argv",["refresh"]):
            self.assertEqual(refresh.main(),0)
        self.assertEqual(execute.call_args.args[1],{"lookback_hours":8})

    def test_daily_bbb_does_not_drift_three_hours_later_every_day(self):
        source=deepcopy(refresh.CONFIG["sources"][1])
        config={**refresh.CONFIG,"sources":[source],"presentation":[],"reconciliation":None}
        for hour,should_execute in ((0,False),(1,True)):
            state=authorized_state()
            state.update(reconciliation={"generation":1},chain_completed_at_utc="2026-10-20T09:35:00Z")
            state["queries"]["bbb_dex"]={"completed_at_utc":"2026-10-20T09:35:00Z","query_parameters":{"lookback_hours":26}}
            record={"query_id":8894132,"query_parameters":{"lookback_hours":26},"execution_id":"daily-bbb",
                    "completed_at_utc":f"2026-10-21T{hour:02d}:07:00Z"}
            with self.subTest(hour=hour),tempfile.TemporaryDirectory() as directory, \
                 patch.object(refresh,"CONFIG",config),patch.object(refresh,"now",return_value=datetime(2026,10,21,hour,7,tzinfo=timezone.utc)), \
                 patch.object(refresh,"load_authoritative_state",return_value=state), \
                 patch.object(refresh,"HEALTH",Path(directory)/"missing.json"),patch.object(refresh,"save"), \
                 patch.object(refresh,"sync_independent_network"),patch.object(refresh,"usage_guard",return_value={}), \
                 patch.object(refresh,"publish_data_generation"),patch.object(refresh,"checkpoint"), \
                 patch.object(refresh,"execute",return_value=record) as execute, \
                 patch.object(refresh,"export_source",return_value=1000),patch("sys.argv",["refresh"]):
                self.assertEqual(refresh.main(),0)
            self.assertEqual(execute.called,should_execute)


if __name__ == "__main__":unittest.main()
