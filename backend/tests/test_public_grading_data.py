"""Keep public labels outside grading inputs and preserve source-data defects."""

import importlib.util
import json
import zipfile
from pathlib import Path
from uuid import uuid4

import pytest
from teaching_assistant.domain import AssessmentInput, Rubric, calculate_score
from teaching_assistant.jev import JevEvaluator
from teaching_assistant.profiles import ProfileRegistry
from teaching_assistant.settings import Settings

SPEC = importlib.util.spec_from_file_location(
    "public_grading_data", Path(__file__).parents[1] / "tools/public_grading_data.py"
)
data = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(data)


@pytest.mark.parametrize("maximum", range(1, 37))
def test_integer_total_encoding_is_lossless_for_every_score(maximum):
    row = {"dataset": "sas-bench", "max_score": maximum, "guideline": "Fixture rubric"}
    rubric = Rubric.model_validate(data.rubric_for(row))
    assert sum(c.maximum_points for c in rubric.criteria) == maximum
    assert all(len(c.levels) <= 10 for c in rubric.criteria)
    for score in range(maximum + 1):
        labels = data.gold_levels_for({**row, "gold_score": score})
        assert calculate_score(rubric, labels) == score


def source_fixture(root, scores=(4, 11)):
    directory = root / "sas-bench/datasets"
    directory.mkdir(parents=True)
    for filename in data.SAS_FILES:
        if filename[0].isdigit():
            (directory / filename).write_text("", encoding="utf-8")
    (directory / "error_type.jsonl").write_text(
        json.dumps({"q_id": 0, "guideline": "Use reference answer, allow partial credit."}),
        encoding="utf-8",
    )
    item = {
        "id": "original",
        "question": "Explain the result.",
        "reference": "The explanation.",
        "analysis": "Independent question analysis.",
        "total": 10,
        "steps": [
            {
                "response": "An incomplete explanation.",
                "label": 4,
                "errors": ["GOLD_ERROR_SENTINEL"],
            }
        ],
    }
    path = directory / "0_Physics_ShortAns.jsonl"
    path.write_text(
        "\n".join(json.dumps({**item, "manual_label": score}) for score in scores), encoding="utf-8"
    )
    (root / "semeval").mkdir()
    with zipfile.ZipFile(root / "semeval/semeval-5way.zip", "w"):
        pass
    return path


def payloads(suite):
    settings = Settings(_env_file=None, jev_model="jev-1.13.0", jev_api_key="fixture-key")
    profile = ProfileRegistry(settings).resolve("jev-v1")
    evaluator = JevEvaluator(settings, profile)
    result = []
    for entry in suite:
        dataset = entry["dataset"]
        for sample in dataset.samples:
            for criterion in dataset.rubric.criteria:
                result.append(
                    evaluator.prepare(
                        AssessmentInput(
                            question_version_id=uuid4(),
                            answer_version_id=uuid4(),
                            rubric_version_id=uuid4(),
                            question=dataset.question,
                            reference=dataset.reference,
                            answer=sample.answer_text,
                            global_instructions=dataset.rubric.global_instructions,
                            criterion=criterion,
                            profile=profile.id,
                        )
                    )
                )
    return result


def test_source_label_change_never_changes_provider_payload(tmp_path):
    path = source_fixture(tmp_path, (4,))
    first = data.build_suite(tmp_path)
    before = payloads(first)
    original = json.loads(path.read_text(encoding="utf-8"))
    original["manual_label"] = 8
    original["steps"][0]["label"] = 8
    original["steps"][0]["errors"] = ["DIFFERENT_GOLD_ERROR"]
    path.write_text(json.dumps(original), encoding="utf-8")
    second = data.build_suite(tmp_path)
    assert first[0]["dataset"].samples[0].gold_levels != second[0]["dataset"].samples[0].gold_levels
    assert before == payloads(second)
    assert "GOLD_ERROR_SENTINEL" not in json.dumps(before)
    assert "DIFFERENT_GOLD_ERROR" not in json.dumps(payloads(second))


def test_invalid_source_score_retained_unlabeled_and_metadata_stays_aligned(tmp_path):
    source_fixture(tmp_path)
    suite = data.build_suite(tmp_path)
    assert len(suite) == 2
    rows = []
    for entry in suite:
        dataset = entry["dataset"]
        for sample, metadata in zip(dataset.samples, entry["rows"], strict=True):
            assert sample.sample_id == metadata["sample_id"]
            rows.append(metadata)
            if metadata["original_label"] == 11:
                assert dataset.label_status == "unlabeled"
                assert sample.gold_levels is None
                assert metadata["gold_score"] is None
                assert metadata["label_valid"] is False
            else:
                assert dataset.label_status == "teacher_confirmed"
                assert calculate_score(dataset.rubric, sample.gold_levels) == metadata["gold_score"]
    assert len({row["question_answer_hash"] for row in rows}) == 1
    assert len({row["sample_id"] for row in rows}) == 2
    assert len(payloads(suite)) == 4


def test_semeval_core_only_preserves_test_splits_and_binary_label_semantics(tmp_path):
    source_fixture(tmp_path, ())
    xml = """<question id="Q1"><questionText>Question?</questionText>
    <referenceAnswers><referenceAnswer>Reference.</referenceAnswer></referenceAnswers>
    <studentAnswers><studentAnswer id="a" accuracy="correct">Correct.</studentAnswer>
    <studentAnswer id="b" accuracy="partially_correct_incomplete">Partial.</studentAnswer>
    <studentAnswer id="c" accuracy="contradictory">Contradiction.</studentAnswer>
    <studentAnswer id="d" accuracy="irrelevant">Irrelevant.</studentAnswer>
    <studentAnswer id="e" accuracy="non_domain">No idea.</studentAnswer></studentAnswers></question>"""
    with zipfile.ZipFile(tmp_path / "semeval/semeval-5way.zip", "w") as archive:
        for path in (
            "beetle/train/Core/q.xml",
            "beetle/test-unseen-questions/Core/q.xml",
            "beetle/test-unseen-questions/Extra/q.xml",
        ):
            archive.writestr(path, xml)
    records = list(data.semeval_records(tmp_path))
    assert len(records) == 10
    suite = data.build_suite(tmp_path)
    assert len(suite) == 1
    assert [row["gold_score"] for row in suite[0]["rows"]] == [1, 0, 0, 0, 0]
    assert {row["split"] for row in suite[0]["rows"]} == {"test-unseen-questions"}
    assert {row["task_type"] for row in suite[0]["rows"]} == {"binary"}


def test_fractional_source_score_is_rejected_instead_of_rounded():
    with pytest.raises(ValueError, match="Non-integer"):
        data._integer("4.5", field="manual_label")


@pytest.mark.parametrize(
    ("changed_field", "new_value"),
    [
        ("question_type", "gapfilling"),
        ("guideline", "Different source scoring instructions."),
    ],
)
def test_same_question_with_distinct_scoring_scope_has_separate_shards(
    tmp_path, monkeypatch, changed_field, new_value
):
    source_fixture(tmp_path, (4,))
    first = next(data.sas_records(tmp_path))
    second = {**first, "sample_id": "second_sample", changed_field: new_value}
    monkeypatch.setattr(data, "sas_records", lambda _: iter([first, second]))
    monkeypatch.setattr(data, "semeval_records", lambda _: iter(()))
    suite = data.build_suite(tmp_path)
    assert len(suite) == 2
    assert len({entry["dataset"].dataset_id for entry in suite}) == 2
    assert {row["question_id"] for entry in suite for row in entry["rows"]} == {
        first["question_id"]
    }
    sources = {row["sample_id"]: row for row in (first, second)}
    for entry in suite:
        assert len(entry["rows"]) == len(entry["dataset"].samples) == 1
        source = sources[entry["rows"][0]["sample_id"]]
        expected = Rubric.model_validate(data.rubric_for(source))
        assert entry["dataset"].rubric == expected
