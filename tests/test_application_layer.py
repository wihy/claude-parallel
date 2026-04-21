import unittest


class ApplicationLayerTest(unittest.TestCase):

    def test_orchestrator_importable_from_application(self):
        from src.application.orchestration import Orchestrator, BudgetExceeded
        self.assertTrue(callable(Orchestrator))

    def test_orchestrator_shim_still_works(self):
        # 过渡期兼容
        from src.orchestrator import Orchestrator
        self.assertTrue(callable(Orchestrator))

    def test_worker_importable_from_application(self):
        from src.application.worker import Worker, WorkerResult, retry_worker
        self.assertTrue(callable(Worker))

    def test_worker_shim_still_works(self):
        from src.worker import Worker
        self.assertTrue(callable(Worker))


if __name__ == "__main__":
    unittest.main()
