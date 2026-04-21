import unittest


class ApplicationLayerTest(unittest.TestCase):

    def test_orchestrator_importable_from_application(self):
        from src.application.orchestration import Orchestrator, BudgetExceeded
        self.assertTrue(callable(Orchestrator))

    def test_orchestrator_shim_still_works(self):
        # 过渡期兼容
        from src.orchestrator import Orchestrator
        self.assertTrue(callable(Orchestrator))


if __name__ == "__main__":
    unittest.main()
