from workflow_engine.utils.string_utils import split_pascal_case


# ---------------------------------------------------------------------------
# split_pascal_case
# ---------------------------------------------------------------------------


def test_split_pascal_case_simple():
    """Should split a simple PascalCase word into its parts."""
    result = split_pascal_case("ConvexHull")
    assert result == ["Convex", "Hull"]


def test_split_pascal_case_multiple_words():
    """Should split a sentence containing PascalCase tokens."""
    result = split_pascal_case("ConvexHull WorkflowEngine")
    assert "Convex" in result
    assert "Hull" in result
    assert "Workflow" in result
    assert "Engine" in result


def test_split_pascal_case_acronym():
    """Should handle consecutive uppercase letters (acronym prefix)."""
    result = split_pascal_case("MSDDatabase")
    assert len(result) >= 2


def test_split_pascal_case_plain_text():
    """Plain lowercase text should be returned as a single token."""
    result = split_pascal_case("traceability")
    assert result == ["traceability"]
