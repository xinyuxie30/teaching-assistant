from copy import deepcopy

import pytest
from teaching_assistant.rubric_diff import compare_rubrics


@pytest.fixture
def rubric():
    return {
        "global_instructions": "按独立评分点判断。",
        "criteria": [
            {
                "id": key,
                "description": f"解释{key}",
                "maximum_points": "5",
                "levels": [
                    {"id": "low", "ordinal": 0, "description": "未体现", "points": "0"},
                    {"id": "partial", "ordinal": 1, "description": "部分体现", "points": "2"},
                    {"id": "full", "ordinal": 2, "description": "完整体现", "points": "5"},
                ],
            }
            for key in ("a", "b")
        ],
    }


def test_numeric_format_defaults_and_level_array_order_are_not_rule_changes(rubric):
    updated = deepcopy(rubric)
    for criterion in updated["criteria"]:
        criterion["semantic_version"] = 1
        criterion["maximum_points"] = "5.00"
        for level in criterion["levels"]:
            level["points"] += ".00"
        criterion["levels"].reverse()
    result = compare_rubrics(rubric, updated)
    assert not result["has_changes"]
    assert result["summary"] == {"added": 0, "removed": 0, "modified": 0, "unchanged": 2}


def test_overall_instruction_change_is_visible_even_when_every_point_is_unchanged(rubric):
    updated = {**rubric, "global_instructions": "允许同义表达，但必须解释原因。"}
    result = compare_rubrics(rubric, updated)
    assert result["has_changes"]
    assert result["global_fields"] == ["global_instructions"]
    assert result["summary"]["unchanged"] == 2


def test_added_removed_and_modified_points_and_level_scores_are_distinguished(rubric):
    updated = deepcopy(rubric)
    criterion = updated["criteria"][0]
    criterion.update(description="补充因果解释", semantic_version=2, maximum_points="6")
    criterion["levels"][1].update(description="解释正确但不完整", points="3")
    criterion["levels"][2]["points"] = "6"
    updated["criteria"][1]["id"] = "replacement"
    original = deepcopy(rubric)
    result = compare_rubrics(rubric, updated)
    assert result["summary"] == {"added": 1, "removed": 1, "modified": 1, "unchanged": 0}
    assert [row["id"] for row in result["criteria"]] == ["a", "replacement", "b"]
    row = result["criteria"][0]
    assert row["changed_fields"] == ["description", "semantic_version", "maximum_points", "levels"]
    assert row["levels"][1]["changed_fields"] == ["description", "points"]
    assert row["levels"][0]["status"] == "unchanged"
    assert rubric == original


def test_new_level_identity_is_not_guessed_from_identical_text(rubric):
    updated = deepcopy(rubric)
    updated["criteria"][0]["levels"][1]["id"] = "replacement"
    rows = compare_rubrics(rubric, updated)["criteria"][0]["levels"]
    assert [(row["id"], row["status"]) for row in rows] == [
        ("low", "unchanged"),
        ("replacement", "added"),
        ("full", "unchanged"),
        ("partial", "removed"),
    ]


def test_changed_ordinals_are_detected_even_when_level_text_is_unchanged(rubric):
    updated = deepcopy(rubric)
    levels = updated["criteria"][0]["levels"]
    levels[0].update(ordinal=1, points="1")
    levels[1].update(ordinal=0, points="0")
    rows = compare_rubrics(rubric, updated)["criteria"][0]["levels"]
    assert rows[0]["id"] == "partial" and rows[0]["changed_fields"] == ["points", "ordinal"]
    assert rows[1]["id"] == "low" and rows[1]["changed_fields"] == ["points", "ordinal"]


def test_point_order_is_reported_without_mislabeling_insertions(rubric):
    reordered = {**rubric, "criteria": list(reversed(rubric["criteria"]))}
    result = compare_rubrics(rubric, reordered)
    assert result["has_changes"] and result["criterion_order_changed"]
    assert result["summary"]["unchanged"] == 2
    inserted = {**rubric, "criteria": [{**rubric["criteria"][0], "id": "new"}, *rubric["criteria"]]}
    result = compare_rubrics(rubric, inserted)
    assert result["has_changes"] and not result["criterion_order_changed"]
    assert result["summary"]["added"] == 1
