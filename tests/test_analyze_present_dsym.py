"""A-W4 Task 15 — 验证 analyze/ + present/ + locate/dsym 迁移."""

import unittest


class AnalyzePresentDsymTest(unittest.TestCase):

    # analyze/ 层 — new paths
    def test_power_attribution_from_new_path(self):
        from src.perf.analyze.power_attribution import (
            ProcessPower,
            attribute_power,
            parse_system_power,
        )
        self.assertTrue(callable(ProcessPower))
        self.assertTrue(callable(attribute_power))
        self.assertTrue(callable(parse_system_power))

    def test_ai_diagnosis_from_new_path(self):
        from src.perf.analyze.ai_diagnosis import (
            DiagnosisContext,
            DiagnosisResult,
            run_diagnosis,
        )
        self.assertTrue(callable(DiagnosisContext))
        self.assertTrue(callable(DiagnosisResult))
        self.assertTrue(callable(run_diagnosis))

    # present/ 层 — new path
    def test_report_html_from_new_path(self):
        from src.perf.present.report_html import generate_html_report
        self.assertTrue(callable(generate_html_report))

    # locate/ 层 — 新 dsym 模块
    def test_dsym_module_exists(self):
        from src.perf.locate.dsym import find_dsym_by_uuid, auto_symbolicate
        self.assertTrue(callable(find_dsym_by_uuid))
        self.assertTrue(callable(auto_symbolicate))

if __name__ == "__main__":
    unittest.main()
