import pytest
from unittest.mock import MagicMock
from frappe_microservice.core import DocumentHooks

class TestDocumentHooks:
    @pytest.fixture
    def hooks(self):
        return DocumentHooks()

    def test_register_and_get(self, hooks):
        handler = lambda x: x
        hooks.register("Test Doc", "before_insert", handler)
        
        registered = hooks.get_hooks("Test Doc", "before_insert")
        assert handler in registered
        
        # Test global hooks
        global_handler = lambda x: x
        hooks.register("*", "before_save", global_handler)
        assert global_handler in hooks.get_hooks("Any Doc", "before_save")

    def test_run_hooks(self, hooks):
        doc = MagicMock()
        doc.doctype = "Test Doc"
        
        context = {"called": False}
        def handler(d):
            context["called"] = True
            
        hooks.register("Test Doc", "before_insert", handler)
        hooks.run_hooks(doc, "before_insert")
        
        assert context["called"] is True

    def test_hook_error_handling(self, hooks):
        doc = MagicMock()
        doc.doctype = "Test Doc"
        
        def failing_handler(d):
            raise ValueError("Hook failed")
            
        hooks.register("Test Doc", "before_insert", failing_handler)
        
        # Should raise error by default
        with pytest.raises(ValueError, match="Hook failed"):
            hooks.run_hooks(doc, "before_insert")
            
        # Should NOT raise error if raise_on_error is False
        hooks.run_hooks(doc, "before_insert", raise_on_error=False)
