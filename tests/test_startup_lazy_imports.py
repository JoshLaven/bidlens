import unittest
from pathlib import Path


class StartupLazyImportTests(unittest.TestCase):
    def test_api_module_does_not_import_research_stack_at_module_load(self):
        api_source = Path("src/bidlens/routes/api.py").read_text()
        module_header = api_source.split("router = APIRouter", 1)[0]

        self.assertNotIn("services.research.brief_generator", module_header)
        self.assertNotIn("services.research.document_fetcher", module_header)

    def test_openai_sdk_is_imported_only_inside_llm_generation(self):
        brief_source = Path("src/bidlens/services/research/brief_generator.py").read_text()
        before_llm_function = brief_source.split("def generate_llm_brief", 1)[0]
        llm_function = brief_source.split("def generate_llm_brief", 1)[1]

        self.assertNotIn("from openai import OpenAI", before_llm_function)
        self.assertIn("from openai import OpenAI", llm_function)


if __name__ == "__main__":
    unittest.main()
