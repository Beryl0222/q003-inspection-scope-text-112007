"""核对随档资料可以读取，以及原始记录引用完整。"""

from pathlib import Path
import tempfile
import unittest

from inspect_records import load_records


class RecordsTest(unittest.TestCase):
    def setUp(self):
        self.data = load_records(Path(__file__).resolve().parents[1] / "data" / "records.json")

    def test_record_references(self):
        for source, field, target in [('inspection', 'handover', 'logistics'), ('correction', 'note', 'inspection'), ('receipt', 'site', 'site')]:
            group, _, key = target.partition(".")
            keys = {row[key or "id"] for row in self.data[group]}
            for row in self.data[source]:
                refs = row.get(field)
                if refs is None:
                    continue
                refs = refs if isinstance(refs, list) else [refs]
                with self.subTest(kind=source, record=row["id"], field=field):
                    self.assertLessEqual(set(refs), keys)

    def test_duplicate_id_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "duplicate.json"
            path.write_text('{"组":[{"id":"1"},{"id":"1"}]}', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "编号重复"):
                load_records(path)

    def test_source_packet(self):
        self.assertEqual(self.data["logistics"][0]["box"], self.data["logistics"][1]["box"])
        assembly = {row["id"] for row in self.data["assembly"]}
        self.assertTrue(all(set(row["serial_candidates"]) <= assembly for row in self.data["logistics"]))
        self.assertGreater(len({row["conditions"] for row in self.data["inspection"]}), 1)
        self.assertEqual({r["type"] for r in self.data["receipt"]}, {"实物签收", "待落实意向"})


if __name__ == "__main__":
    unittest.main()
