# -*- coding: utf-8 -*-
"""V2.1 Evidence Builder 组装单测：关联、溯源、预算、高命中收益。"""

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
    """构造 BM25 命中 unit：实际只用 message_index 和 start/end。"""
    content = mem["messages"][index]["content"]
    return (0, index, 0, start, len(content) if end is None else end,
            mem["messages"][index]["role"])


def _kinds(text):
    return re.findall(r"^\[(\w+) \|", text, re.M)


PROBLEM = ("TimeSeries required column check is misleading in "
           "astropy/timeseries/core.py: expected ['time', 'a'] but found "
           "['time', 'b']. Hidden test_required_columns fails.")
READ = ('[tool_use Read]\n'
        '{"file_path": "/testbed/astropy/timeseries/core.py"}')
SOURCE = ("[tool_result]\n"
          "     1\tdef _check_required_columns(timeseries, required_columns):\n"
          "     2\t    names = timeseries.colnames\n"
          "     3\t    raise ValueError(\"TimeSeries object is invalid - expected\")")
TEST_CMD = ('[tool_use Bash]\n'
            '{"command": "pytest astropy/timeseries/tests/test_sampled.py '
            '-k test_required_columns"}')
ERROR = ("[tool_result]\n"
         "Traceback (most recent call last):\n"
         "  File \"astropy/timeseries/core.py\", line 75, in "
         "_check_required_columns\n"
         "ValueError: TimeSeries object is invalid - expected ['time', 'a'] "
         "as the first columns but found ['time', 'b']")
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


def _full_mem():
    return _mem(
        ("user", PROBLEM),
        ("assistant", READ),
        ("user", SOURCE),
        ("assistant", TEST_CMD),
        ("user", ERROR),
        ("assistant", PATCH),
        ("user", TEST_OK),
        ("assistant", SUMMARY),
    )


def test_full_assembly_all_sections_in_fixed_order():
    mem = _full_mem()
    text = EvidenceBuilder().render(mem, _unit(0, mem), QUERY, 20000)

    assert _kinds(text) == [
        "problem", "source", "error", "patch", "test", "test", "summary"]
    assert "def _check_required_columns" in text
    assert "Traceback (most recent call last)" in text
    assert "--- a/astropy/timeseries/core.py" in text
    assert "+    raise ValueError(\"required_columns are missing\")" in text
    assert "$ pytest astropy/timeseries/tests/test_sampled.py" in text
    assert "1 passed in 0.12s" in text
    assert "Summary: _check_required_columns" in text


def test_negative_control_excludes_other_task_patch_and_summary():
    """required-columns 查询不能把 to_pandas 任务的补丁/总结拼进来。"""
    other_patch = ('[tool_use Edit]\n' + json.dumps({
        "file_path": "/testbed/astropy/table/table.py",
        "old_string": "        new_col = col.to_pandas()",
        "new_string": "        new_col = col.to_pandas()\n"
                      "        remove_indices(col, tbl)",
    }))
    other_summary = ("Summary: fixed BinnedTimeSeries.to_pandas KeyError by "
                     "calling remove_indices before replacing the column in "
                     "astropy/table/table.py. " + "context " * 40)
    mem = _mem(
        ("user", PROBLEM),
        ("user", SOURCE),
        ("assistant", other_patch),
        ("assistant", other_summary),
    )
    text = EvidenceBuilder().render(mem, _unit(0, mem), QUERY, 20000)

    assert "_check_required_columns" in text
    assert "remove_indices" not in text
    assert "to_pandas" not in text
    assert "astropy/table/table.py" not in text
    assert _kinds(text) == ["problem", "source"]


def test_source_span_traces_back_to_original_message():
    mem = _full_mem()
    spans = EvidenceBuilder()._spans(mem)
    source = next(s for s in spans if s.kind == "source")

    original = mem["messages"][source.message_index]["content"]
    assert source.message_index == 2
    assert source.start > 0
    assert original[source.start:source.end] == source.body


def test_budget_is_a_hard_cap_and_tiny_budget_returns_empty():
    mem = _full_mem()
    builder = EvidenceBuilder()
    assert len(builder.render(mem, _unit(0, mem), QUERY, 600)) <= 600
    assert builder.render(mem, _unit(0, mem), QUERY, 30) == ""


def test_thinking_anchor_is_kept_as_note_without_prefix():
    thinking = "[thinking]" + "check _check_required_columns call chain " * 20
    mem = _mem(("user", PROBLEM), ("assistant", thinking))
    text = EvidenceBuilder().render(mem, _unit(1, mem), QUERY, 4000)

    assert "[note | m=1" in text
    assert "[thinking]" not in text
    assert "check _check_required_columns call chain" in text


def test_only_the_last_assistant_text_is_used_as_summary():
    first = "First summary about _check_required_columns. " + "early " * 60
    last = "Last summary about _check_required_columns. " + "final " * 60
    mem = _mem(("user", PROBLEM), ("assistant", first), ("assistant", last))
    text = EvidenceBuilder().render(mem, _unit(0, mem), QUERY, 20000)

    assert "Last summary" in text
    assert "First summary" not in text


def test_high_hit_summary_pulls_in_source_patch_and_test():
    """高命中：query 直接命中总结时，应带出同任务的源码、补丁和测试。"""
    summary = ("Summary: BinnedTimeSeries.to_pandas raised KeyError. The fix "
               "calls remove_indices before replacing columns in "
               "astropy/table/table.py. " + "detail " * 20)
    source = ("[tool_result]\n"
              "    10\tdef to_pandas(self):\n"
              "    11\t    remove_indices(self, index)\n"
              "    12\t    return self.as_array()")
    patch = ('[tool_use Edit]\n' + json.dumps({
        "file_path": "/testbed/astropy/table/table.py",
        "old_string": "    return self.as_array()",
        "new_string": "    remove_indices(self, self.indices)\n"
                      "    return self.as_array()",
    }))
    test_ok = "[tool_result]\n1 passed in 0.20s"
    mem = _mem(
        ("assistant", summary),
        ("user", source),
        ("assistant", patch),
        ("user", test_ok),
    )
    text = EvidenceBuilder().render(
        mem, _unit(0, mem),
        "BinnedTimeSeries to_pandas KeyError remove_indices", 20000)

    assert _kinds(text) == ["source", "patch", "test", "summary"]
    assert "remove_indices" in text
    assert "--- a/astropy/table/table.py" in text
    assert len(text) > 2 * len(summary)


# ---- 双格式兼容：带方括号与不带方括号的消息必须解析一致 ----

def _tool_message(name, data, bracketed=True):
    marker = "[tool_use {}]".format(name) if bracketed else "tool_use {}".format(name)
    return marker + "\n" + json.dumps(data)


def _result_message(body, bracketed=True):
    marker = "[tool_result]" if bracketed else "tool_result"
    return marker + "\n" + body


def _only_span(role, content):
    spans = EvidenceBuilder()._spans(_mem((role, content)))
    assert len(spans) == 1, spans
    return spans[0]


def _assert_same(span_a, span_b, fields):
    for field in fields:
        assert getattr(span_a, field) == getattr(span_b, field), field


@pytest.mark.parametrize("name,data", [
    ("Edit", {
        "file_path": "/testbed/main.py",
        "old_string": "a = 1",
        "new_string": "a = 2",
    }),
    ("Write", {
        "file_path": "/testbed/main.py",
        "content": "hello\nworld\n",
    }),
    ("MultiEdit", {
        "file_path": "/testbed/main.py",
        "edits": [
            {"old_string": "a = 1", "new_string": "a = 2"},
            {"old_string": "b = 3", "new_string": "b = 4"},
        ],
    }),
])
def test_bracket_and_plain_tool_use_produce_same_patch(name, data):
    fields = ("kind", "body", "file_paths", "strong", "test_names",
              "primary_path")
    for bracketed in (True, False):
        content = _tool_message(name, data, bracketed)
        span = _only_span("assistant", content)
        assert span.kind == "patch"
        assert span.start == 0 and span.end == len(content)
        if bracketed:
            bracketed_span = span
        else:
            _assert_same(bracketed_span, span, fields)


def test_bracket_and_plain_bash_produce_same_test_evidence():
    command = ("pytest astropy/timeseries/tests/sampled.py "
               "-k test_required_columns")
    fields = ("kind", "body", "file_paths", "strong", "test_names",
              "primary_path")
    spans = []
    for bracketed in (True, False):
        content = _tool_message("Bash", {"command": command}, bracketed)
        span = _only_span("assistant", content)
        assert span.kind == "test"
        assert span.body == "$ " + command
        assert span.test_names == frozenset({"test_required_columns"})
        assert content[span.start:span.end] == content
        spans.append(span)
    _assert_same(spans[0], spans[1], fields)


@pytest.mark.parametrize("body,kind", [
    ("     1\tdef _check_required_columns(timeseries):\n"
     "     2\t    return timeseries.colnames", "source"),
    ("Traceback (most recent call last):\n"
     "  File \"astropy/timeseries/core.py\", line 75\n"
     "ValueError: bad columns", "error"),
    ("1 passed in 0.12s", "test"),
])
def test_bracket_and_plain_tool_result_produce_same_evidence(body, kind):
    fields = ("kind", "body", "file_paths", "strong", "test_names",
              "primary_path")
    spans = []
    for bracketed in (True, False):
        content = _result_message(body, bracketed)
        span = _only_span("user", content)
        assert span.kind == kind
        assert content[span.start:span.end] == span.body
        spans.append(span)
    _assert_same(spans[0], spans[1], fields)


def test_tool_result_colon_variant_is_supported():
    body = "1 passed in 0.12s"
    span = _only_span("user", "tool_result: " + body)
    assert span.kind == "test"
    assert span.body == body


def test_bracket_and_plain_thinking_render_the_same_note():
    body = "check _check_required_columns call chain " * 20
    renders = []
    for bracketed in (True, False):
        content = ("[thinking] " if bracketed else "thinking ") + body
        mem = _mem(("user", PROBLEM), ("assistant", content))
        spans = EvidenceBuilder()._spans(mem)
        assert [s.message_index for s in spans] == [0]
        text = EvidenceBuilder().render(mem, _unit(1, mem), QUERY, 4000)
        assert "[note | m=1" in text
        assert "[thinking]" not in text
        assert body.strip() in text
        renders.append(text)
    assert renders[0] == renders[1]


@pytest.mark.parametrize("tool_name", ["Bash", "Edit", "Read"])
def test_tool_result_inherits_entities_from_both_tool_use_formats(tool_name):
    data = {"file_path": "/testbed/astropy/timeseries/core.py"}
    if tool_name == "Bash":
        data["command"] = "pytest astropy/timeseries/tests/test_sampled.py"
    else:
        data.update({"old_string": "a", "new_string": "b"})
    inherited = []
    for bracketed in (True, False):
        mem = _mem(
            ("assistant", _tool_message(tool_name, data, bracketed)),
            ("user", _result_message("1 passed in 0.12s", bracketed)),
        )
        span = next(s for s in EvidenceBuilder()._spans(mem)
                    if s.message_index == 1)
        assert span.kind == "test"
        assert "astropy/timeseries/core.py" in span.inherited
        assert "core.py" in span.inherited
        assert span.primary_path == "astropy/timeseries/core.py"
        inherited.append(span.inherited)
    assert inherited[0] == inherited[1]


def test_mixed_formats_keep_tool_call_and_result_related():
    data = {"file_path": "/testbed/astropy/timeseries/core.py",
            "old_string": "a", "new_string": "b"}
    for call_bracketed, result_bracketed in ((True, False), (False, True)):
        mem = _mem(
            ("assistant", _tool_message("Edit", data, call_bracketed)),
            ("user", _result_message("1 passed in 0.12s", result_bracketed)),
        )
        result = next(s for s in EvidenceBuilder()._spans(mem)
                      if s.message_index == 1)
        assert result.kind == "test"
        assert result.primary_path == "astropy/timeseries/core.py"
        assert "astropy/timeseries/core.py" in result.inherited


@pytest.mark.parametrize("role,content,kind", [
    ("assistant", _tool_message("Bash", {
        "command": "pytest " + " ".join(
            "tests/test_case_{:02d}.py".format(i) for i in range(20)),
    }), "test"),
    ("assistant", _tool_message("Bash", {
        "command": "pytest " + " ".join(
            "tests/test_case_{:02d}.py".format(i) for i in range(20)),
    }, bracketed=False), "test"),
    ("assistant", _tool_message("Edit", {
        "file_path": "/testbed/main.py",
        "old_string": "a = 1",
        "new_string": "a = 2\n" + "detail = 1\n" * 30,
    }), "patch"),
    ("assistant", _tool_message("Edit", {
        "file_path": "/testbed/main.py",
        "old_string": "a = 1",
        "new_string": "a = 2\n" + "detail = 1\n" * 30,
    }, bracketed=False), "patch"),
    ("assistant", "[thinking] " + "reason " * 60, None),
    ("assistant", "thinking " + "reason " * 60, None),
    ("user", _result_message("1 passed in 0.12s " + "detail " * 40), "test"),
    ("user", _result_message("1 passed in 0.12s " + "detail " * 40,
                             bracketed=False), "test"),
])
def test_one_message_is_parsed_at_most_once(role, content, kind):
    spans = EvidenceBuilder()._spans(_mem((role, content)))
    assert len(spans) <= 1
    assert [s.kind for s in spans] == ([] if kind is None else [kind])


def test_identifier_like_prefixes_are_not_treated_as_markers():
    for content in ("thinking_about this task " + "detail " * 40,
                    "tool_usage notes for core.py " + "detail " * 40):
        span = _only_span("assistant", content)
        assert span.kind == "summary"
        assert span.body == content

    for content in ("tool_result_summary: 1 passed in 0.12s " + "detail " * 40,
                    "[tool_result_summary] 1 passed in 0.12s " + "detail " * 40):
        span = _only_span("user", content)
        assert span.kind == "problem"
        assert span.body == content


@pytest.mark.parametrize("content", [
    "[tool_use Edit]\n{not json",
    "tool_use Edit\n{not json",
    "[tool_use Edit]\n{not json}",
    "tool_use Edit\n{not json}",
    "[tool_use Bash]",
    "tool_use Bash",
    "[tool_use Edit]",
    "tool_use Edit",
])
def test_invalid_or_incomplete_tool_messages_are_ignored(content):
    mem = _mem(("user", PROBLEM), ("assistant", content))
    spans = EvidenceBuilder()._spans(mem)
    assert [s for s in spans if s.message_index == 1] == []


@pytest.mark.parametrize("content", [
    "[tool_result]", "tool_result", "[tool_result]\n", "tool_result\n",
    "tool_result: ",
])
def test_empty_tool_results_are_ignored(content):
    mem = _mem(("user", content))
    assert EvidenceBuilder()._spans(mem) == []
