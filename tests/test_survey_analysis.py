"""督察线索分析的规则测试。

覆盖：描述拆解、独立/复看/不明归属、补录改判、扩围触发门槛、
阴性观察效力、流向与未检点、抽点优先级与名额、签收处置。
"""

from pathlib import Path
import copy
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from inspect_records import load_records
import survey_analysis as sa

DATA = ROOT / "data" / "records.json"


class AnalysisTest(unittest.TestCase):
    def setUp(self):
        self.records = load_records(DATA)
        self.result = sa.analyze(self.records)

    def find_note(self, note_id):
        return next(n for n in self.result["parsed_notes"] if n["id"] == note_id)

    def find_attr(self, note_id):
        return next(a for a in self.result["attributions"] if a["note_id"] == note_id)

    def find_family(self, part, defect):
        return next(
            f for f in self.result["families"]
            if f["part"] == part and f["defect"] == defect
        )

    # ---- 描述拆解
    def test_parse_splits_part_defect_polarity(self):
        n1 = self.find_note("N1")
        kinds = {(f["part"], f["defect"], f["polarity"]) for f in n1["findings"]}
        self.assertIn(("转轴", "裂纹", "positive"), kinds)
        self.assertIn(("压盖", "松动", "negative"), kinds)
        self.assertEqual(n1["serial_confirmed"], ["L-091"])

    def test_parse_ambiguous_serial_keeps_candidates(self):
        # 补录前 N3 样号抄写存疑，只能保留候选
        before = sa.analyze(self.records, use_corrections=False)
        n3 = next(n for n in before["parsed_notes"] if n["id"] == "N3")
        self.assertEqual(set(n3["serial_ambiguous"]), {"L-091", "L-097"})
        self.assertEqual(n3["serial_confirmed"], [])

    def test_parse_flags_repair_and_negative_conditions(self):
        n4 = self.find_note("N4")
        self.assertTrue(n4["repair_after"])
        neg = next(f for f in n4["findings"] if f["polarity"] == "negative")
        self.assertEqual((neg["part"], neg["defect"]), ("转轴", "裂纹"))
        # 同一条里的划伤是独立的阳性，不能被“未复现”连坐
        self.assertTrue(
            any(f["part"] == "壳面" and f["polarity"] == "positive" for f in n4["findings"])
        )

    def test_unretained_part_retest_is_not_repair_review(self):
        # N6 是“未换轴时的复测”，仍属原始状态，不能误判为维修后复看
        n6 = self.find_note("N6")
        self.assertFalse(n6["repair_after"])
        self.assertTrue(
            any(f["polarity"] == "positive" for f in n6["findings"])
        )

    # ---- 归属
    def test_review_of_same_sample_is_duplicate_not_new_item(self):
        n2 = self.find_attr("N2")
        self.assertEqual(n2["kind"], "duplicate")
        self.assertEqual(n2["serial"], "L-091")

    def test_distinct_serials_are_independent_samples(self):
        independent = {
            a["serial"] for a in self.result["attributions"] if a["kind"] == "independent"
        }
        # 三个序列号各有一条独立首证；复看笔录不产生新样品
        self.assertEqual(independent, {"L-091", "L-097", "L-111"})

    def test_duplicate_records_merge_into_one_sample(self):
        samples = {s["serial"]: s for s in self.result["samples"]}
        # N1/N2/N3 都归到 L-091，但样品只算一件
        self.assertEqual(set(samples), {"L-091", "L-097", "L-111"})
        self.assertEqual(
            set(samples["L-091"]["note_ids"]), {"N1", "N2", "N3"}
        )
        self.assertEqual(
            set(samples["L-097"]["note_ids"]), {"N4", "N6"}
        )

    def test_ambiguous_note_is_unidentified_without_correction(self):
        before = sa.analyze(self.records, use_corrections=False)
        n3 = next(a for a in before["attributions"] if a["note_id"] == "N3")
        self.assertEqual(n3["kind"], "unidentified")
        self.assertIsNone(n3["serial"])

    def test_correction_resolves_identity_and_invalidates_handover(self):
        correction = self.result["corrections"][0]
        self.assertEqual(correction["confirmed_serial"], "L-091")
        self.assertEqual(correction["invalidated_handover"], "H2")
        n3 = self.find_attr("N3")
        self.assertEqual(n3["serial"], "L-091")
        self.assertEqual(n3["kind"], "duplicate")

    # ---- 扩围触发
    def test_trigger_requires_two_independent_same_lot_same_condition(self):
        family = self.find_family("转轴", "裂纹")
        self.assertTrue(family["trigger_met"])
        self.assertEqual(set(family["independent_serials"]), {"L-091", "L-097"})
        samples = {s["serial"]: s for s in self.result["samples"]}
        lots = {samples[s]["component_lot"] for s in family["independent_serials"]}
        self.assertEqual(lots, {"K4"})

    def test_single_item_defect_does_not_trigger(self):
        for part, defect in [("垫板", "松动"), ("壳面", "划伤")]:
            family = self.find_family(part, defect)
            self.assertFalse(family["trigger_met"])
            self.assertEqual(len(family["independent_serials"]), 1)

    def test_negative_only_family_never_counts_as_evidence(self):
        family = self.find_family("压盖", "松动")
        self.assertFalse(family["trigger_met"])
        self.assertEqual(family["evidence_notes"], [])
        self.assertEqual(family["independent_serials"], [])

    def test_unidentified_record_cannot_satisfy_trigger(self):
        # 去掉补录后 N3 身份不明；触发仍由 L-091、L-097 两件独立样品满足，
        # 若把 N3 误当独立件，独立样号集合会被污染
        before = sa.analyze(self.records, use_corrections=False)
        family = next(
            f for f in before["families"]
            if f["part"] == "转轴" and f["defect"] == "裂纹"
        )
        self.assertTrue(family["trigger_met"])
        self.assertEqual(set(family["independent_serials"]), {"L-091", "L-097"})
        self.assertNotIn("N3", family["evidence_notes"])

    def test_repair_review_negative_does_not_clear_lead(self):
        family = self.find_family("转轴", "裂纹")
        n4_neg = next(n for n in family["negatives"] if n["note_id"] == "N4")
        self.assertFalse(n4_neg["counts"])
        self.assertTrue(n4_neg["repair_after"])

    def test_different_condition_negative_cannot_withdraw_control(self):
        # N4 工况（常温换轴后）与阳性工况（低温静置后）不同，阴性无效
        family = self.find_family("转轴", "裂纹")
        n4_neg = next(n for n in family["negatives"] if n["note_id"] == "N4")
        self.assertIn("工况", n4_neg["reason"])

    # ---- 流向
    def test_lot_flow_marks_uninspected_outflow_sites(self):
        k4 = next(f for f in self.result["lot_flow"] if f["component_lot"] == "K4")
        self.assertEqual(k4["uninspected_sites"], ["D"])
        self.assertEqual(set(k4["possible_sites"]), {"A", "C", "D"})

    def test_correction_shrinks_outflow_scope(self):
        before = sa.analyze(self.records, use_corrections=False)
        k4_before = next(f for f in before["lot_flow"] if f["component_lot"] == "K4")
        k4_after = next(f for f in self.result["lot_flow"] if f["component_lot"] == "K4")
        self.assertIn("B", k4_before["possible_sites"])
        self.assertNotIn("B", k4_after["possible_sites"])

    def test_box_number_reuse_does_not_merge_handovers(self):
        # H1、H2 都用 7 号箱，但属于不同交接、不同去向
        boxes = {h["id"]: h["box"] for h in self.records["logistics"]}
        self.assertEqual(boxes["H1"], boxes["H2"])
        sites = {
            h["id"]: h["to"] for h in self.records["logistics"]
        }
        self.assertNotEqual(sites["H1"], sites["H2"])

    # ---- 计划
    def test_plan_prioritizes_uninspected_same_source_site(self):
        plan = self.result["plan"]
        self.assertEqual([a["site_id"] for a in plan["assigned"]], ["D"])
        first = plan["assigned"][0]
        self.assertEqual(first["tier"], "same_source")
        self.assertIn("L-102", first["target_serials"])
        self.assertEqual(first["required_conditions"], "低温静置后")

    def test_plan_leaves_slot_unassigned_without_discriminative_target(self):
        plan = self.result["plan"]
        self.assertEqual(plan["remaining_total"], 2)
        self.assertEqual(plan["leftover_slots"], 1)
        self.assertIn("保留", plan["leftover_reason"])

    def test_plan_before_correction_uses_both_slots_on_uninspected_sites(self):
        before = sa.analyze(self.records, use_corrections=False)
        plan = before["plan"]
        self.assertEqual(
            sorted(a["site_id"] for a in plan["assigned"]), ["B", "D"]
        )
        self.assertEqual(plan["leftover_slots"], 0)

    def test_physical_receipt_consumes_slot_even_if_attribution_changes(self):
        plan = self.result["plan"]
        r1 = next(r for r in plan["receipts"] if r["receipt_id"] == "R1")
        self.assertTrue(r1["slot_used"])
        self.assertEqual(r1["decision"], "executed")

    def test_intent_receipt_is_withdrawable_and_uses_no_slot(self):
        plan = self.result["plan"]
        r2 = next(r for r in plan["receipts"] if r["receipt_id"] == "R2")
        self.assertFalse(r2["slot_used"])
        self.assertEqual(r2["decision"], "withdrawable")

    def test_zero_remaining_site_offers_no_action(self):
        # A 桥西店 remaining=0，不出现在候选现场
        plan = self.result["plan"]
        self.assertNotIn("A", [a["site_id"] for a in plan["candidates"]])


class ContrastControlTest(unittest.TestCase):
    """对照点识别：同厂异批次 / 异厂同批次。"""

    def setUp(self):
        self.base = load_records(DATA)

    def test_contrast_same_plant_other_lot(self):
        records = copy.deepcopy(self.base)
        # 在江台店放一台芦岸 K5（同厂异部件批次）设备
        records["logistics"].append(
            {"id": "H9", "box": "1", "from": "芦岸装配", "to": "江台店",
             "serial_candidates": ["L-092"], "date": "2026-10-08"}
        )
        result = sa.analyze(records)
        action_b = next(a for a in result["plan"]["candidates"] if a["site_id"] == "B")
        self.assertEqual(action_b["tier"], "contrast")
        self.assertIn("同厂异部件批次", action_b["reason"])

    def test_contrast_other_plant_same_lot(self):
        records = copy.deepcopy(self.base)
        # 新增一台“东湾”厂装配的 K4 设备放到江台店（异厂同部件批次），
        # 东湾不在触发样品的装配厂集合内，构成真正的异厂对照
        records["assembly"].append(
            {"id": "L-205", "component_lot": "K4", "plant": "东湾"}
        )
        records["logistics"].append(
            {"id": "H9", "box": "1", "from": "临水分拨", "to": "江台店",
             "serial_candidates": ["L-205"], "date": "2026-10-08"}
        )
        result = sa.analyze(records)
        action_b = next(a for a in result["plan"]["candidates"] if a["site_id"] == "B")
        # K4 件到场首先是同源扩围点，同时标注异厂对照价值
        self.assertEqual(action_b["tier"], "same_source")
        self.assertIn("异厂同部件批次", action_b["reason"])


class ThresholdTest(unittest.TestCase):
    """扩围门槛的边界情形。"""

    def setUp(self):
        self.base = load_records(DATA)

    def test_two_items_same_condition_but_different_lot_no_trigger(self):
        records = copy.deepcopy(self.base)
        # 把 L-097 改成另一供应批次，则 K4 只剩一件独立阳性
        for row in records["assembly"]:
            if row["id"] == "L-097":
                row["component_lot"] = "K9"
        result = sa.analyze(records)
        family = next(
            f for f in result["families"]
            if f["part"] == "转轴" and f["defect"] == "裂纹"
        )
        self.assertFalse(family["trigger_met"])

    def test_two_items_same_lot_but_different_condition_no_trigger(self):
        records = copy.deepcopy(self.base)
        # N6（L-097 的阳性复看）工况改成常温，则 L-097 无低温阳性
        for row in records["inspection"]:
            if row["id"] == "N6":
                row["conditions"] = "常温"
        result = sa.analyze(records)
        family = next(
            f for f in result["families"]
            if f["part"] == "转轴" and f["defect"] == "裂纹"
        )
        self.assertFalse(family["trigger_met"])


class DefectSeparationTest(unittest.TestCase):
    """共享供应件可同源，但各自不相关的外观问题必须分开。"""

    def setUp(self):
        self.records = load_records(DATA)

    def test_unrelated_appearance_defects_form_separate_families(self):
        result = sa.analyze(self.records)
        keys = {(f["part"], f["defect"]) for f in result["families"]}
        # 转轴裂纹（同源候选）与壳面划伤、垫板松动分成不同证据族
        self.assertIn(("转轴", "裂纹"), keys)
        self.assertIn(("壳面", "划伤"), keys)
        self.assertIn(("垫板", "松动"), keys)
        crack = next(f for f in result["families"]
                     if f["part"] == "转轴" and f["defect"] == "裂纹")
        scratch = next(f for f in result["families"]
                       if f["part"] == "壳面" and f["defect"] == "划伤")
        self.assertTrue(crack["trigger_met"])
        self.assertFalse(scratch["trigger_met"])

    def test_shared_component_lot_links_samples_but_keeps_identity(self):
        # L-091（芦岸）与 L-097（矶北）装配厂不同，但共享 K4 供应件
        result = sa.analyze(self.records)
        samples = {s["serial"]: s for s in result["samples"]}
        self.assertEqual(
            samples["L-091"]["component_lot"], samples["L-097"]["component_lot"]
        )
        self.assertNotEqual(samples["L-091"]["plant"], samples["L-097"]["plant"])
        # 仍是两件可确认独立样品，不合并
        self.assertNotEqual(
            samples["L-091"]["note_ids"], samples["L-097"]["note_ids"]
        )


if __name__ == "__main__":
    unittest.main()
