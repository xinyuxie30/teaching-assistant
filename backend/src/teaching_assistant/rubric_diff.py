"""Structural rubric differences, never a semantic-equivalence or regrading decision."""

from collections import Counter

from teaching_assistant.domain import Rubric


def normalized_rubric(body: dict) -> dict:
    rubric = Rubric.model_validate(body)
    result = rubric.model_dump(mode="json")
    for value, criterion in zip(result["criteria"], rubric.criteria, strict=True):
        value["maximum_points"] = f"{criterion.maximum_points:.2f}"
        value["levels"] = [
            {**level.model_dump(mode="json"), "points": f"{level.points:.2f}"}
            for level in sorted(criterion.levels, key=lambda level: level.ordinal)
        ]
    return result


def row_change(before, after, fields):
    if before is None:
        return {"id": after["id"], "status": "added", "changed_fields": []}
    if after is None:
        return {"id": before["id"], "status": "removed", "changed_fields": []}
    changed = [field for field in fields if before[field] != after[field]]
    return {
        "id": after["id"],
        "status": "modified" if changed else "unchanged",
        "changed_fields": changed,
    }


def paired_rows(before, after):
    old, new = {row["id"]: row for row in before}, {row["id"]: row for row in after}
    # Follow the new document's order, then list removals in their original order.
    return [(old.get(key), new.get(key)) for key in [*new, *(key for key in old if key not in new)]]


def compare_rubrics(before: dict, after: dict) -> dict:
    before, after = normalized_rubric(before), normalized_rubric(after)
    criteria = []
    for old, new in paired_rows(before["criteria"], after["criteria"]):
        change = row_change(
            old, new, ("description", "semantic_version", "maximum_points", "levels")
        )
        change["levels"] = [
            row_change(old_level, new_level, ("description", "points", "ordinal"))
            for old_level, new_level in paired_rows(
                old["levels"] if old else [], new["levels"] if new else []
            )
        ]
        criteria.append(change)
    counts = Counter(change["status"] for change in criteria)
    fields = [
        key
        for key in ("global_instructions", "selection_policy", "rounding_policy")
        if before[key] != after[key]
    ]
    old_ids = [item["id"] for item in before["criteria"]]
    new_ids = [item["id"] for item in after["criteria"]]
    common = set(old_ids) & set(new_ids)
    order_changed = [key for key in old_ids if key in common] != [
        key for key in new_ids if key in common
    ]
    return {
        "has_changes": bool(
            fields or order_changed or any(row["status"] != "unchanged" for row in criteria)
        ),
        "summary": {key: counts[key] for key in ("added", "removed", "modified", "unchanged")},
        "global_fields": fields,
        "criterion_order_changed": order_changed,
        "criteria": criteria,
    }
