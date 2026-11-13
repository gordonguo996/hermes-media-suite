"""同名重复行合并：发布页把同一条任务列两遍时不应判歧义。"""
import pytest

from agent.winport import xingtu_tasks as X

TASK = {"task_name": "沫檬油污-智星鸿图-星川任务",
        "task_name_full": "沫檬油污-智星鸿图-星川任务"}


def _row(name):
    return {"visible": True, "name": name, "titles": [name], "order_ids": []}


def test_identical_duplicate_rows_collapse_to_one():
    out = X.match_picker_rows([_row("沫檬油污-智星鸿图-星川任务"),
                               _row("沫檬油污-智星鸿图-星川任务")], TASK)
    assert out["status"] == X.RESOLVE_OK
    assert out["resolved_identity"]["kind"] == "full_name"


def test_same_name_with_different_order_ids_stays_ambiguous():
    a = _row("沫檬油污-智星鸿图-星川任务")
    a["order_ids"] = ["1234567890"]
    b = _row("沫檬油污-智星鸿图-星川任务")
    b["order_ids"] = ["9876543210"]
    out = X.match_picker_rows([a, b], TASK)
    assert out["status"] == X.RESOLVE_AMBIGUOUS


def test_distinct_full_names_still_resolve_exactly():
    out = X.match_picker_rows([_row("沫檬油污-智星鸿图-星川任务"),
                               _row("沫檬油污-智星鸿图-星川任务-B")], TASK)
    assert out["status"] == X.RESOLVE_OK
