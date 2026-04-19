"""Regression test for #6.3: gt_extractor distillation must use settings.model_router,
not settings.model_general (the slow cloud model).
"""
import pytest


@pytest.mark.smoke
def test_gt_distill_uses_model_router_as_default():
    """Source-level check: the distill call site must reference settings.model_router.

    We verify via AST rather than runtime mocking because the distillation path
    involves SearXNG + DB + many fake scaffolds. An AST check is robust, fast,
    and catches future regressions cleanly.
    """
    import ast
    with open("/code/app/modules/gt_extractor.py") as f:
        tree = ast.parse(f.read())

    # Find all Attribute nodes that reference settings.*
    settings_attrs = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "settings"
        ):
            settings_attrs.add(node.attr)

    assert "model_router" in settings_attrs, (
        f"#6.3 regression: gt_extractor should reference settings.model_router. "
        f"settings.* attrs found: {settings_attrs}"
    )
    assert "model_general" not in settings_attrs, (
        f"#6.3 regression: settings.model_general should NOT appear in gt_extractor. "
        f"settings.* attrs found: {settings_attrs}"
    )
