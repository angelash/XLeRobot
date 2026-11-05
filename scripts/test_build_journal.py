"""Regression coverage for recurring journal updates and historical cutoffs."""
from copy import deepcopy
import json
from pathlib import Path
import unittest

from build_journal import daily, prepare, render_coverage, render_readme, render_sources


class RecurringJournalTests(unittest.TestCase):
    def setUp(self):
        self.source = json.loads((Path(__file__).resolve().parents[1]/"docs/journal/data.json").read_text(encoding="utf-8"))

    def test_later_snapshot_counts_and_paths_replace_previous_batch(self):
        updated = deepcopy(self.source)
        updated["snapshot"].update(messages=7777,user_messages=432,turns=222,private_snapshot=".local-archive/generated-next-batch")
        updated["range_end"] = "2026-11-07"
        updated["cutoff"] = "2026-11-07 10:00"
        data = prepare(updated)
        self.assertIn("432条用户消息",render_readme(data))
        self.assertIn("7,777",render_coverage(data))
        self.assertIn("generated-next-batch/transcripts/",render_sources(data))
        self.assertIn("2026-11-07 10:00",render_readme(data))

    def test_prior_day_keeps_its_original_evidence_cutoff(self):
        data = prepare(self.source)
        previous = next(d for d in data["calendar"] if d["date"]=="2026-10-20")
        text = daily(previous,data)
        self.assertIn(previous["cutoff"],text)
        self.assertNotIn("本日整理依据截止 "+data["cutoff"],text)

    def test_unobserved_dates_stay_blank_when_range_extends(self):
        data = deepcopy(self.source)
        data["range_end"] = "2026-11-07"
        prepared = prepare(data)
        day = prepared["calendar"][-1]
        self.assertTrue(day["empty"])
        self.assertIn("未找到",day["summary"])
        self.assertEqual(prepared["display"]["recorded_days"],len(data["days"]))

    def test_render_does_not_mutate_reviewed_source(self):
        before = deepcopy(self.source)
        prepared = prepare(self.source)
        self.assertIn("calendar",prepared)
        self.assertEqual(self.source,before)

    def test_unknown_source_reference_cannot_be_published(self):
        self.source["days"][-1]["sources"].append("unavailable-generated-source")
        with self.assertRaises(AssertionError):
            prepare(self.source)


if __name__=="__main__":
    unittest.main()
