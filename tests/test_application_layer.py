import unittest


class ApplicationLayerTest(unittest.TestCase):

    def test_orchestrator_importable_from_application(self):
        from src.application.orchestration import Orchestrator, BudgetExceeded
        self.assertTrue(callable(Orchestrator))

    def test_worker_importable_from_application(self):
        from src.application.worker import Worker, WorkerResult, retry_worker
        self.assertTrue(callable(Worker))

    def test_merger_importable_from_application(self):
        from src.application.merge import WorktreeMerger, MergeReport
        self.assertTrue(callable(WorktreeMerger))

    def test_reviewer_importable_from_application(self):
        from src.application.review import CodeReviewer
        self.assertTrue(callable(CodeReviewer))

    def test_validator_importable_from_application(self):
        from src.application.validation import TaskValidator
        self.assertTrue(callable(TaskValidator))

    def test_context_extractor_importable_from_application(self):
        from src.application.context_extraction import extract_context_for_downstream
        self.assertTrue(callable(extract_context_for_downstream))

    def test_claude_client_from_infrastructure(self):
        from src.infrastructure.claude.client import strip_code_fences
        self.assertTrue(callable(strip_code_fences))

    def test_chat_input_from_infrastructure(self):
        from src.infrastructure.input.chat_input import ChatInputSession
        self.assertTrue(callable(ChatInputSession))

    def test_worker_result_in_domain(self):
        from src.domain.worker_result import WorkerResult
        r = WorkerResult(task_id="t1", success=True)
        self.assertEqual(r.task_id, "t1")
        self.assertTrue(r.success)

    def test_worker_result_still_reachable_via_worker(self):
        # application.worker 应 re-export WorkerResult (避免一次性改所有消费方)
        from src.application.worker import WorkerResult
        self.assertTrue(WorkerResult.__module__.startswith("src.domain"))


if __name__ == "__main__":
    unittest.main()
