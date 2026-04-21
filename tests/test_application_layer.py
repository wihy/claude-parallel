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

    def test_merger_importable_from_application(self):
        from src.application.merge import WorktreeMerger, MergeReport
        self.assertTrue(callable(WorktreeMerger))

    def test_merger_shim_still_works(self):
        from src.merger import WorktreeMerger
        self.assertTrue(callable(WorktreeMerger))

    def test_reviewer_importable_from_application(self):
        from src.application.review import CodeReviewer
        self.assertTrue(callable(CodeReviewer))

    def test_reviewer_shim_still_works(self):
        from src.reviewer import CodeReviewer
        self.assertTrue(callable(CodeReviewer))

    def test_validator_importable_from_application(self):
        from src.application.validation import TaskValidator
        self.assertTrue(callable(TaskValidator))

    def test_validator_shim_still_works(self):
        from src.validator import TaskValidator
        self.assertTrue(callable(TaskValidator))

    def test_context_extractor_importable_from_application(self):
        from src.application.context_extraction import extract_context_for_downstream
        self.assertTrue(callable(extract_context_for_downstream))

    def test_context_extractor_shim_still_works(self):
        from src.context_extractor import extract_context_for_downstream
        self.assertTrue(callable(extract_context_for_downstream))

    def test_claude_client_from_infrastructure(self):
        from src.infrastructure.claude.client import strip_code_fences
        self.assertTrue(callable(strip_code_fences))

    def test_claude_client_shim_still_works(self):
        from src.claude_client import strip_code_fences
        self.assertTrue(callable(strip_code_fences))

    def test_chat_input_from_infrastructure(self):
        from src.infrastructure.input.chat_input import ChatInputSession
        self.assertTrue(callable(ChatInputSession))

    def test_chat_input_shim_still_works(self):
        from src.chat_input import ChatInputSession
        self.assertTrue(callable(ChatInputSession))


if __name__ == "__main__":
    unittest.main()
