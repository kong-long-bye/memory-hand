# -*- coding: utf-8 -*-
"""V3.0-lite 单测：solution_status 判定、答案优先渲染、False Solution 门禁。"""

import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from evidence_builder import EvidenceBuilder  # noqa: E402


def _mem(*messages, mem_id="mem_test"):
    return {"id": mem_id,
            "messages": [{"role": role, "content": content}
                         for role, content in messages]}


def _unit(index, mem, start=0, end=None):
    content = mem["messages"][index]["content"]
    return (0, index, 0, start, len(content) if end is None else end,
            mem["messages"][index]["role"])


def _status(text):
    match = re.match(r"\[solution_status=(\w+)", text)
    return match.group(1) if match else None


PROBLEM = ("TimeSeries required column check is misleading in "
           "astropy/timeseries/core.py: expected ['time', 'a'] but found "
           "['time', 'b']. Hidden test_required_columns fails.")
SOURCE = ("[tool_result]\n"
          "     1\tdef _check_required_columns(timeseries, required_columns):\n"
          "     2\t    names = timeseries.colnames\n"
          "     3\t    raise ValueError(\"TimeSeries object is invalid - expected\")")
TEST_CMD = ('[tool_use Bash]\n'
            '{"command": "pytest astropy/timeseries/tests/test_sampled.py '
            '-k test_required_columns"}')
PATCH = ('[tool_use Edit]\n' + json.dumps({
    "file_path": "/testbed/astropy/timeseries/core.py",
    "old_string": '    raise ValueError("TimeSeries object is invalid - expected")',
    "new_string": '    raise ValueError("required_columns are missing")',
}))
TEST_OK = "[tool_result]\n1 passed in 0.12s"
SUMMARY = ("Summary: _check_required_columns in astropy/timeseries/core.py "
           "now reports the missing names, and test_required_columns covers "
           "the regression. " + "detail " * 40)
QUERY = ("required columns error message _check_required_columns "
         "test_required_columns astropy/timeseries/core.py")

# 另一条子任务（to_pandas）的补丁与总结：T2 负例的污染源
OTHER_PATCH = ('[tool_use Edit]\n' + json.dumps({
    "file_path": "/testbed/astropy/table/table.py",
    "old_string": "        new_col = col.to_pandas()",
    "new_string": "        new_col = col.to_pandas()\n"
                  "        remove_indices(col, tbl)",
}))
OTHER_SUMMARY = ("Summary: fixed BinnedTimeSeries.to_pandas KeyError by "
                 "calling remove_indices before replacing the column in "
                 "astropy/table/table.py. " + "context " * 40)
OTHER_SOURCE = ("[tool_result]\n"
                "   210\tdef to_pandas(self):\n"
                "   211\t    return self.as_array()")


def _v3():
    return EvidenceBuilder(mode="v3_0")


# ---- 状态判定 ----

def test_verified_solution_when_bound_patch_and_passed_test():
    mem = _mem(
        ("user", PROBLEM),
        ("user", SOURCE),
        ("assistant", TEST_CMD),
        ("assistant", PATCH),
        ("user", TEST_OK),
        ("assistant", SUMMARY),
    )
    text = _v3().render(mem, _unit(0, mem), QUERY, 20000)

    assert _status(text) == "verified_solution"
    assert "1 passed in 0.12s" in text
    assert "Re-verify before applying" in text


def test_patch_available_when_no_passed_test():
    mem = _mem(
        ("user", PROBLEM),
        ("user", SOURCE),
        ("assistant", PATCH),
        ("user", "[tool_result]\n2 failed, 1 passed in 0.30s"),
    )
    text = _v3().render(mem, _unit(0, mem), QUERY, 20000)

    assert _status(text) == "patch_available"
    assert "no trusted passing-test record" in text


def test_summary_only_when_summary_but_no_patch():
    mem = _mem(
        ("user", PROBLEM),
        ("user", SOURCE),
        ("assistant", SUMMARY),
    )
    text = _v3().render(mem, _unit(0, mem), QUERY, 20000)

    assert _status(text) == "summary_only"
    assert "Summary: _check_required_columns" in text
    assert "no directly recoverable patch" in text


def test_source_only_declares_no_answer():
    mem = _mem(
        ("user", PROBLEM),
        ("user", SOURCE),
    )
    text = _v3().render(mem, _unit(0, mem), QUERY, 20000)

    assert _status(text) == "source_only"
    assert "def _check_required_columns" in text
    assert "NO verified patch" in text


def test_related_only_for_thinking_anchor():
    thinking = "[thinking]" + "wondering about the timeseries package " * 10
    mem = _mem(("user", PROBLEM), ("assistant", thinking))
    text = _v3().render(mem, _unit(1, mem), "timeseries package design", 4000)

    assert _status(text) == "related_only"
    assert len(text) <= 1000
    assert "not a solution" in text


# ---- False Solution 门禁（T2 负例） ----

def test_other_task_patch_never_becomes_the_solution():
    """required-columns 查询：to_pandas 的补丁/总结不得进入 v3 输出。"""
    mem = _mem(
        ("user", PROBLEM),
        ("user", SOURCE),
        ("assistant", OTHER_PATCH),
        ("assistant", OTHER_SUMMARY),
    )
    text = _v3().render(mem, _unit(0, mem), QUERY, 20000)

    assert _status(text) == "source_only"
    assert "remove_indices" not in text
    assert "to_pandas" not in text
    assert "_check_required_columns" in text


def test_secondary_anchor_cannot_smuggle_other_task_patch():
    """次锚点落在 to_pandas 消息上时，补丁仍不得绑为本题答案。"""
    mem = _mem(
        ("user", PROBLEM),
        ("user", SOURCE),
        ("user", OTHER_SOURCE),
        ("assistant", OTHER_PATCH),
    )
    text = _v3().render(mem, [_unit(0, mem), _unit(3, mem)], QUERY, 20000)

    assert _status(text) == "source_only"
    assert "remove_indices" not in text
    assert "[patch |" not in text


# ---- 答案优先渲染 ----

def test_answer_first_patch_before_source():
    mem = _mem(
        ("user", PROBLEM),
        ("user", SOURCE),
        ("assistant", TEST_CMD),
        ("assistant", PATCH),
        ("user", TEST_OK),
        ("assistant", SUMMARY),
    )
    text = _v3().render(mem, _unit(0, mem), QUERY, 20000)

    assert text.index("[patch |") < text.index("[source |")
    assert text.index("[patch |") < 2048  # 前 2KB 内必须出现补丁
    assert "--- a/astropy/timeseries/core.py" in text


def test_chronological_patch_sequence_keeps_final_state():
    first = ('[tool_use Edit]\n' + json.dumps({
        "file_path": "/testbed/astropy/timeseries/core.py",
        "old_string": "    names = timeseries.colnames",
        "new_string": "    names = list(timeseries.colnames)  # intermediate",
    }))
    second = ('[tool_use Edit]\n' + json.dumps({
        "file_path": "/testbed/astropy/timeseries/core.py",
        "old_string": '    raise ValueError("TimeSeries object is invalid - expected")',
        "new_string": '    raise ValueError("required_columns are missing")',
    }))
    mem = _mem(
        ("user", PROBLEM),
        ("assistant", first),
        ("assistant", second),
    )
    text = _v3().render(mem, _unit(0, mem), QUERY, 20000)

    assert _status(text) == "patch_available"
    assert text.index("intermediate") < text.index("required_columns are missing")


# ---- 预算与去重 ----

def test_source_only_budget_cap():
    huge_source = ("[tool_result]\n"
                   + "".join("   {:03d}\tx = _check_required_columns + {}\n"
                             .format(i, i) for i in range(400)))
    mem = _mem(("user", PROBLEM), ("user", huge_source))
    text = _v3().render(mem, _unit(0, mem), QUERY, 20000)

    assert _status(text) == "source_only"
    assert len(text) <= 4000


def test_duplicate_source_rendered_once():
    mem = _mem(
        ("user", PROBLEM),
        ("user", SOURCE),
        ("user", SOURCE),
    )
    v3_text = _v3().render(mem, _unit(0, mem), QUERY, 20000)
    v2_text = EvidenceBuilder(mode="v2_1").render(
        mem, _unit(0, mem), QUERY, 20000)

    assert v3_text.count("def _check_required_columns(timeseries") == 1
    assert v2_text.count("def _check_required_columns(timeseries") == 2


# ---- 模式开关与回滚 ----

def test_default_mode_is_v2_1_and_output_unchanged(monkeypatch):
    monkeypatch.delenv("RETRIEVAL_MODE", raising=False)
    mem = _mem(
        ("user", PROBLEM),
        ("user", SOURCE),
        ("assistant", PATCH),
        ("user", TEST_OK),
    )
    default = EvidenceBuilder()
    explicit = EvidenceBuilder(mode="v2_1")

    assert default.mode == "v2_1"
    assert (default.render(mem, _unit(0, mem), QUERY, 20000)
            == explicit.render(mem, _unit(0, mem), QUERY, 20000))


@pytest.mark.parametrize("raw,expected", [
    ("v3_0", "v3_0"), ("v3", "v3_0"), ("V3_0", "v3_0"),
    ("v2_1", "v2_1"), ("garbage", "v2_1"), ("", "v2_1"),
])
def test_env_mode_normalization(monkeypatch, raw, expected):
    monkeypatch.setenv("RETRIEVAL_MODE", raw)
    assert EvidenceBuilder().mode == expected


def test_v2_1_output_has_no_status_header(monkeypatch):
    monkeypatch.delenv("RETRIEVAL_MODE", raising=False)
    mem = _mem(("user", PROBLEM), ("user", SOURCE), ("assistant", PATCH))
    text = EvidenceBuilder().render(mem, _unit(0, mem), QUERY, 20000)

    assert "solution_status" not in text
    assert text.startswith("[problem |")


# ---- MemoryStore 集成 ----

def test_search_content_follows_retrieval_mode(tmp_path, monkeypatch):
    pytest.importorskip("rank_bm25")
    pytest.importorskip("jieba")
    from memory_engine import MemoryStore

    messages = [
        {"role": "user", "content": PROBLEM},
        {"role": "user", "content": SOURCE},
        {"role": "assistant", "content": PATCH},
        {"role": "user", "content": TEST_OK},
        {"role": "assistant", "content": SUMMARY},
    ]

    monkeypatch.setenv("RETRIEVAL_MODE", "v3_0")
    v3_store = MemoryStore(tmp_path / "v3.jsonl")
    v3_store.add("req-1", messages, "u1", "s1")
    v3_hits = v3_store.search("u1", QUERY, top_k=5)
    assert v3_hits and v3_hits[0]["content"].startswith(
        "[solution_status=verified_solution")
    assert v3_store.stats()["retrieval_mode"] == "v3_0"

    monkeypatch.setenv("RETRIEVAL_MODE", "v2_1")
    v2_store = MemoryStore(tmp_path / "v2.jsonl")
    v2_store.add("req-1", messages, "u1", "s1")
    v2_hits = v2_store.search("u1", QUERY, top_k=5)
    assert v2_hits and "solution_status" not in v2_hits[0]["content"]
    # 召回与排序不受模式影响
    assert [h["id"] for h in v3_hits] == [h["id"] for h in v2_hits]
    assert [h["score"] for h in v3_hits] == [h["score"] for h in v2_hits]
