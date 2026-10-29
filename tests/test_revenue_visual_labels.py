from copy import deepcopy
import io,json,os,sys,tempfile,unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"scripts"))
import v3_sync_dynamic_counter_labels as labels


class RevenueVisualTests(unittest.TestCase):
    def test_counter_rename_preserves_column_formatting_and_query(self):
        viz={"name":"old","description":"old","options":{"counterColName":"same_column", "stringPrefix":"$","stringDecimal":2}}
        with patch.object(labels,"run_json",side_effect=[viz,{}]) as run:
            labels.update_counter(12,"Oct Foundation ledger","new description","same_column",name="Foundation Booked Balance")
        args=run.call_args.args[0];options=json.loads(args[args.index("--options")+1])
        self.assertEqual(options["counterColName"],"same_column")
        self.assertEqual(options["stringDecimal"],2);self.assertEqual(options["stringPrefix"],"$")
        self.assertEqual(args[args.index("--name")+1],"Foundation Booked Balance")
        self.assertFalse(any("execute" in str(call) for call in run.call_args_list))
    def test_unrelated_counter_is_never_overwritten(self):
        with patch.object(labels,"run_json",return_value={"options":{"counterColName":"different"}}) as run:
            with self.assertRaises(RuntimeError):labels.update_counter(12,"label","detail","expected",name="name")
        self.assertEqual(run.call_count,1)
    def test_new_carrier_receipt_does_not_relabel_an_old_fiat_payout(self):
        revenue={"latest_service":{"month":"2026-09-01"},
                 "latest_payment":{"payment_date":"2026-10-26","amount_usd":"32643.19"}}
        defi={"settlements":[{"payment_received_date":"2026-10-26","payment_received_usd":32643.19,
                              "service_months":[{"service_month":"2026-08"}]}],
              "fiat_operator_transfers":[{"source_month":"2026-10","service_month":"2026-07","operator_payout_usd":1108.18}]}
        with tempfile.TemporaryDirectory() as directory:
            paths=[Path(directory)/name for name in ["revenue.json","defi.json","vis.json"]]
            paths[0].write_text(json.dumps(revenue));paths[1].write_text(json.dumps(defi))
            paths[2].write_text((ROOT/"config/v3_headline_visual_ids.json").read_text())
            with patch.object(labels,"REVENUE_STATE",paths[0]),patch.object(labels,"DEFI",paths[1]), \
                 patch.object(labels,"VIS_CONFIG",paths[2]),patch.dict(os.environ,{"DUNE_API_KEY":"offline"}), \
                 patch.object(labels,"update_counter") as update,redirect_stdout(io.StringIO()):
                labels.main()
        calls={call.args[0]:call.args for call in update.call_args_list}
        self.assertEqual(calls[12931311][1],"Aug 2026")
        self.assertEqual(calls[12940450][1],"Jul 2026 service")
        self.assertEqual(calls[12940451][1],"Jul 2026 service")
    def test_foundation_chart_only_changes_names_not_axes_or_columns(self):
        options={"columnMapping":{"projected_buy_burn_usd":"y","transferred_to_buy_burn_usd":"y","month":"x"},
                 "yAxis":[{"title":{"text":"USD"}}],"seriesOptions":{"projected_buy_burn_usd":{"type":"column"}}}
        with patch.object(labels,"run_json",side_effect=[{"query_id":8895089,"options":deepcopy(options)},{}]) as run:
            labels.sync_foundation_labels({"foundation_accounting":{"source_month":"2026-10","inc_payable_usd":"64829.61"}}, {})
        args=run.call_args.args[0];changed=json.loads(args[args.index("--options")+1])
        self.assertEqual(changed["columnMapping"],options["columnMapping"])
        self.assertEqual(changed["yAxis"],options["yAxis"])
        self.assertEqual(changed["seriesOptions"]["projected_buy_burn_usd"]["name"],"Foundation Receivables")


if __name__=="__main__":unittest.main()
