"""Code-owned task catalog and pinned execution policies; never model-extensible."""

from types import MappingProxyType

from teaching_assistant.agent.context import manifest as legacy_manifest
from teaching_assistant.domain import ModelError, digest
from teaching_assistant.harness import budgets, hooks
from teaching_assistant.harness.assisted_lesson import AssistedLessonTask
from teaching_assistant.harness.background_lesson import BackgroundLessonTask
from teaching_assistant.harness.class_practice import ClassPracticeTask
from teaching_assistant.harness.compaction import COMPACTION_VERSION
from teaching_assistant.harness.grading import GradingTask
from teaching_assistant.harness.lesson import LessonTask
from teaching_assistant.harness.parallel_lesson import (
    LessonReviewTask,
    ParallelLessonTask,
    PracticeDesignerTask,
)
from teaching_assistant.harness.practice import PracticeTask
from teaching_assistant.harness.research_lesson import ResearchLessonTask
from teaching_assistant.harness.rubric import RubricTask
from teaching_assistant.harness.semantic_compaction import SEMANTIC_TOOL_VERSION, SEMANTIC_VERSION
from teaching_assistant.harness.team_lesson import TeamLessonTask

TASKS = MappingProxyType(
    {
        task.key: task
        for task in (
            RubricTask(),
            LessonTask(),
            TeamLessonTask(),
            PracticeTask(),
            ParallelLessonTask(),
            PracticeDesignerTask(),
            LessonReviewTask(),
            AssistedLessonTask(),
            ResearchLessonTask(),
            BackgroundLessonTask(),
            ClassPracticeTask(),
            GradingTask(),
        )
    }
)
V2_TASKS = ("rubric", "lesson-plan", "lesson-plan-team", "lesson-practice")
V3_TASKS = (*V2_TASKS, "lesson-plan-parallel", "lesson-practice-designer", "lesson-review")
V4_TASKS = (*V3_TASKS, "lesson-plan-assisted")
ALLOWED_CHILDREN = MappingProxyType(
    {
        "lesson-plan-team": ("lesson-practice",),
        "lesson-plan-parallel": ("lesson-practice-designer", "lesson-review"),
        "lesson-practice-designer": ("lesson-practice",),
        "lesson-plan-background": ("lesson-review",),
    }
)
ALIASES = MappingProxyType(
    {
        "homework-grading": ("fake-grading-agent-v1", "deepseek-grading-agent-v1"),
        "class-practice": ("fake-class-practice-v1", "deepseek-class-practice-v1"),
        "rubric": ("fake-rubric-agent-v1", "deepseek-rubric-agent-v1"),
        "lesson-plan": ("fake-lesson-agent-v1", "deepseek-lesson-agent-v1"),
        "lesson-plan-team": ("fake-lesson-team-agent-v1", "deepseek-lesson-team-agent-v1"),
        "lesson-practice": ("fake-practice-agent-v1", "deepseek-practice-agent-v1"),
        "lesson-plan-parallel": (
            "fake-lesson-parallel-agent-v1",
            "deepseek-lesson-parallel-agent-v1",
        ),
        "lesson-practice-designer": (
            "fake-practice-designer-agent-v1",
            "deepseek-practice-designer-agent-v1",
        ),
        "lesson-review": ("fake-lesson-review-agent-v1", "deepseek-lesson-review-agent-v1"),
        "lesson-plan-assisted": (
            "fake-assisted-lesson-agent-v1",
            "deepseek-assisted-lesson-agent-v1",
        ),
        "lesson-plan-research": (
            "fake-research-lesson-agent-v1",
            "deepseek-research-lesson-agent-v1",
        ),
        "lesson-plan-background": (
            "fake-background-lesson-agent-v1",
            "deepseek-background-lesson-agent-v1",
        ),
    }
)


def resolve(task_type):
    try:
        return TASKS[task_type]
    except KeyError:
        raise ModelError("unknown_task_type", retryable=False) from None


def v1_manifest(settings, task_type, alias):
    task = resolve(task_type)
    if alias not in ALIASES[task_type]:
        raise ModelError("unknown_agent_profile", retryable=False)
    provider = "deepseek" if alias.startswith("deepseek-") else "fake"
    result = legacy_manifest(settings, f"{provider}-rubric-agent-v1")
    result.update(
        runtime_version="harness-runtime-v1",
        context_version="exact-context-v1",
        hook_version="deny-only-hooks-v1",
        stop_version="persisted-artifact-stop-v1",
        max_stop_repairs=1,
        task_type=task.key,
        task_version=task.version,
        tool_version="typed-tools-v1",
        tool_registry_hash=digest(task.registry.manifest()),
        tools_hash=digest(task.registry.schemas()),
        system_hash=digest(task.system),
        alias=alias,
    )
    if provider == "fake":
        result["model"] = alias
    return result


def v2_manifest(settings, task_type, alias):
    result = v1_manifest(settings, task_type, alias)
    result.update(
        runtime_version="harness-runtime-v2",
        context_version=COMPACTION_VERSION,
        context_keep_recent_groups=2,
        scheduler_version="durable-children-v1",
        completion_status=getattr(resolve(task_type), "completion_status", "waiting_teacher"),
        allowed_children=list(ALLOWED_CHILDREN.get(task_type, ())),
        scheduler={
            "max_depth": 1,
            "max_children": 2,
            "max_parallel_children": 1,
            "child_max_model_calls": 3,
            "child_max_tool_calls": 3,
        },
    )
    return result


def v3_manifest(settings, task_type, alias):
    result = v2_manifest(settings, task_type, alias)
    result.update(
        runtime_version="harness-runtime-v3",
        context_version=SEMANTIC_VERSION,
        context_keep_recent_messages=8,
        scheduler_version="durable-children-v2",
        waiting_version="durable-waits-v1",
        scheduler={
            "max_depth": 2,
            "max_children": 8,
            "max_parallel_children": 2,
            "child_max_model_calls": 3,
            "child_max_tool_calls": 4,
        },
    )
    return result


def v4_manifest(settings, task_type, alias):
    result = v3_manifest(settings, task_type, alias)
    result.update(runtime_version="harness-runtime-v4", context_version=SEMANTIC_TOOL_VERSION)
    if task_type == "lesson-plan-assisted":
        result.update(
            interactive_version="teacher-questions-v1", memory_version="confirmed-course-memory-v1"
        )
    return result


def v5_manifest(settings, task_type, alias):
    result = v4_manifest(settings, task_type, alias)
    result.update(
        runtime_version="harness-runtime-v5",
        budget=budgets.policy_for(settings, result),
        hook_snapshot=hooks.snapshot(settings),
    )
    if task_type == "lesson-plan-background":
        result["background_version"] = "durable-background-children-v1"
    if task_type == "lesson-plan-research":
        result.update(
            interactive_version="teacher-questions-v1",
            memory_version="confirmed-course-memory-v1",
            tool_result_storage={
                "version": "tool-result-references-v1",
                "allowed_tools": ["search_teaching_memory"],
                "inline_threshold_characters": 4000,
                "preview_characters": 256,
                "read_page_characters": 4000,
            },
        )
    return result


def manifest(settings, task_type, alias):
    result = v5_manifest(settings, task_type, alias)
    # A nested designer needs three successful calls. Reserve room for the two
    # recoveries allowed by the existing attempt cap, within the root's fixed
    # budget. Unknown dispatches still count; persisted profiles stay frozen.
    result["scheduler"]["child_max_model_calls"] = 5
    return result


def profiles(settings, *, task_type=None, include_legacy=False):
    result = []
    for key in TASKS if task_type is None else (task_type,):
        resolve(key)
        for alias in ALIASES[key]:
            try:
                policy = manifest(settings, key, alias)
            except ModelError:
                result.append({"alias": alias, "id": None, "ready": False})
                continue
            ready = policy["provider"] == "fake" or bool(
                settings.deepseek_api_key and settings.deepseek_api_key.get_secret_value().strip()
            )
            result.append(
                {
                    "alias": alias,
                    "id": "agent-" + digest(policy),
                    "manifest": policy,
                    "ready": ready,
                }
            )
            if include_legacy:
                old_policies = [v5_manifest(settings, key, alias)]
                if key in V4_TASKS:
                    old_policies.append(v4_manifest(settings, key, alias))
                if key in V3_TASKS:
                    old_policies.append(v3_manifest(settings, key, alias))
                if key in V2_TASKS:
                    old_policies.append(v2_manifest(settings, key, alias))
                if key in {"rubric", "lesson-plan"}:
                    old_policies.append(v1_manifest(settings, key, alias))
                if key == "rubric":
                    old_policies.append(legacy_manifest(settings, alias))
                result.extend(
                    {"alias": alias, "id": "agent-" + digest(old), "manifest": old, "ready": ready}
                    for old in old_policies
                )
    return result


def describe_tasks():
    return [
        {
            "task_type": task.key,
            "version": task.version,
            "waiting_for": task.waiting_for,
            "child_only": task.key
            in {"lesson-practice", "lesson-practice-designer", "lesson-review"},
            "allowed_children": list(ALLOWED_CHILDREN.get(task.key, ())),
            "profiles": list(ALIASES[task.key]),
            "tools": task.registry.manifest(),
        }
        for task in TASKS.values()
    ]
