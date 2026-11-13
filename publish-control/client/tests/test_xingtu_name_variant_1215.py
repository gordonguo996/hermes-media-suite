"""1.2.215: status suffixes and re-issued ``-N`` task names must still match."""
from agent.winport import xingtu_tasks as X


def _row(name, *, visible=True, order_ids=None):
    return {
        "visible": visible,
        "name": name,
        "titles": [name],
        "text": name,
        "order_ids": list(order_ids or []),
    }


def test_unavailable_words_cover_page_wording():
    assert "\u65e0\u6cd5\u6295\u7a3f" in X._UNAVAILABLE_WORDS
    assert "\u72b6\u6001\u5f02\u5e38" in X._UNAVAILABLE_WORDS


def test_strip_task_status():
    assert X.strip_task_status("\u690d\u7269\u5927\u5e08\u809a\u8110\u8d34-2\u65e0\u6cd5\u6295\u7a3f") == "\u690d\u7269\u5927\u5e08\u809a\u8110\u8d34-2"
    assert X.strip_task_status("\u690d\u7269\u5927\u5e08\u809a\u8110\u8d34\u72b6\u6001\u5f02\u5e38") == "\u690d\u7269\u5927\u5e08\u809a\u8110\u8d34"
    assert X.strip_task_status("\u690d\u7269\u5927\u5e08\u809a\u8110\u8d34") == "\u690d\u7269\u5927\u5e08\u809a\u8110\u8d34"


def test_same_task_variant_only_numeric_suffix():
    assert X.same_task_variant("\u690d\u7269\u5927\u5e08\u809a\u8110\u8d34", "\u690d\u7269\u5927\u5e08\u809a\u8110\u8d34-2\u65e0\u6cd5\u6295\u7a3f")
    assert X.same_task_variant("\u690d\u7269\u5927\u5e08\u809a\u8110\u8d34-2", "\u690d\u7269\u5927\u5e08\u809a\u8110\u8d34")
    assert not X.same_task_variant("\u690d\u7269\u5927\u5e08\u809a\u8110\u8d34", "\u51b7\u9178\u7075\u7259\u818f-2")
    assert not X.same_task_variant("\u690d\u7269\u5927\u5e08\u809a\u8110\u8d34", "\u690d\u7269\u5927\u5e08\u809a\u8110\u8d34-\u5176\u4ed6")
    assert not X.same_task_variant("\u690d\u7269\u5927\u5e08\u809a\u8110\u8d34", "\u690d\u7269\u5927\u5e08\u809a\u8110\u8d34A-2")


def test_full_name_matches_reissued_variant():
    task = {"task_name_full": "\u690d\u7269\u5927\u5e08\u809a\u8110\u8d34"}
    out = X.match_picker_rows([_row("\u690d\u7269\u5927\u5e08\u809a\u8110\u8d34-2")], task)
    assert out["status"] == X.RESOLVE_OK


def test_full_name_variant_is_unique_only():
    task = {"task_name_full": "\u690d\u7269\u5927\u5e08\u809a\u8110\u8d34"}
    rows = [_row("\u690d\u7269\u5927\u5e08\u809a\u8110\u8d34-2"), _row("\u690d\u7269\u5927\u5e08\u809a\u8110\u8d34-3")]
    out = X.match_picker_rows(rows, task)
    assert out["status"] == X.RESOLVE_AMBIGUOUS
    assert out["code"] == "XINGTU_TASK_AMBIGUOUS"


def test_full_name_variant_still_rejects_other_tasks():
    task = {"task_name_full": "\u690d\u7269\u5927\u5e08\u809a\u8110\u8d34"}
    out = X.match_picker_rows([_row("\u51b7\u9178\u7075\u7259\u818f-2")], task)
    assert out["status"] == X.RESOLVE_NOT_FOUND


def test_unavailable_suffix_marks_task_unusable():
    task = {"task_name_full": "\u690d\u7269\u5927\u5e08\u809a\u8110\u8d34"}
    out = X.match_picker_rows([_row("\u690d\u7269\u5927\u5e08\u809a\u8110\u8d34-2\u65e0\u6cd5\u6295\u7a3f")], task)
    assert out["status"] == X.RESOLVE_UNAVAILABLE
    assert out["code"] == "XINGTU_TASK_UNAVAILABLE"
