"""Regression test for #6.3: gt_extractor distillation must default to the
``model_router`` role (small/fast), not ``model_general`` (slow cloud).

After Sprint E.7 the call site references the role by name (``role="model_router"``)
rather than a settings attribute. We assert on that string at the AST level —
robust, fast, and catches future regressions cleanly without requiring runtime
SearXNG / DB / scaffolding.
"""
import ast

import pytest


@pytest.mark.smoke
def test_gt_distill_uses_model_router_role_as_default():
    with open("/code/app/modules/gt_extractor.py") as f:
        source = f.read()
    tree = ast.parse(source)

    # Find every model_router.generate(...) call and inspect its keyword args.
    # The default route_kwargs literal — {"model": model} if model else {"role": "model_router"}
    # — is what we want to see; if a future edit defaults to "model_general"
    # (or any other role), this fails.
    role_strings_used: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for k, v in zip(node.keys, node.values):
                if (
                    isinstance(k, ast.Constant)
                    and k.value == "role"
                    and isinstance(v, ast.Constant)
                    and isinstance(v.value, str)
                ):
                    role_strings_used.add(v.value)

    assert "model_router" in role_strings_used, (
        f"#6.3 regression: gt_extractor distill must default to "
        f'role="model_router". role strings found: {role_strings_used}'
    )
    assert "model_general" not in role_strings_used, (
        f"#6.3 regression: role=\"model_general\" must NOT appear in "
        f"gt_extractor (it's the slow cloud default). "
        f"role strings found: {role_strings_used}"
    )
