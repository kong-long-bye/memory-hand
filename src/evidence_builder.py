# -*- coding: utf-8 -*-
"""V2.1 Evidence Builder：在同一个 Parent 内选择与查询相关的证据并渲染。

只改变返回内容的组织方式，不参与打分：BM25、Parent 聚合和全局预算都保持
V1.1 不变。安全规则：
- BM25 命中的 unit 所在消息永远是 anchor，保证命中不丢；
- 其他证据必须与 query 或 anchor 共享具体的文件路径 / snake_case 符号 /
  测试名；仅同 Parent、仅相邻、仅宽泛类名（如 TimeSeries）或单个通用异常名
  （如 ValueError）不算；
- 渲染顺序固定 problem → note → source → error → patch → test → summary，
  缺失的类型不输出空章节；note 只用于 BM25 命中的 thinking 片段，避免
  把推理伪装成源码或补丁。

补丁证据来自原始消息里的 tool_use JSON（Edit/Write/MultiEdit），渲染时
用 diff 形式还原；其余证据都按 message_index + start/end 回查原文。
"""

import difflib
import json
import re


MAX_PARENT_CONTENT_CHARS = 16000  # 单个 Parent 证据包的上限，仍受全局预算约束
MAX_SECTION_CHARS = {
    "problem": 1500,   # 问题描述最多 1500 字符
    "note": 1200,      # 笔记（thinking 片段）最多 1200 字符
    "source": 5000,    # 源码片段最多 5000 字符
    "error": 1500,     # 错误日志最多 1500 字符
    "patch": 5000,     # 补丁/diff 最多 5000 字符
    "test": 2500,      # 测试命令及输出最多 2500 字符
    "summary": 3000,   # 总结最多 3000 字符
}
MAX_SECTIONS = {
    "problem": 1,   # 最多放 1 条问题描述
    "note": 1,      # 最多放 1 条笔记
    "source": 2,    # 最多放 2 条源码片段
    "error": 1,     # 最多放 1 条错误日志
    "patch": 2,     # 最多放 2 条补丁
    "test": 2,      # 最多放 2 条测试记录
    "summary": 1,   # 最多放 1 条总结
}
SECTION_ORDER = ("problem", "note", "source", "error", "patch", "test", "summary")
# patch/test/summary/error 一旦拼错代价高：要求同主文件，或至少两个实体支持
HIGH_STAKES = ("error", "patch", "test", "summary")

_TOOL_RE = re.compile(r"^\[tool_use ([A-Za-z_]+)\]\s*(\{.*\})\s*$", re.S)
_TOOL_RESULT = "[tool_result]"
_TEST_CMD_RE = re.compile(
    r"\b(?:pytest|py\.test|unittest|tox|nox)\b|"
    r"(?:npm|yarn|pnpm)\s+test|\bgo\s+test\b|\bcargo\s+test\b", re.I)
_PATH_RE = re.compile(
    r"(?:/testbed/)?(?:[\w.-]+/)*[\w.-]+\.(?:py|pyx|txt|rst|toml|cfg|ini|json|ya?ml)")
_WORD_RE = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*\b")
_TEST_NAME_RE = re.compile(r"\btest_[A-Za-z0-9_]+\b")
_NUMBERED_LINE_RE = re.compile(r"^\s*\d+\t", re.M)
_DEF_RE = re.compile(r"^\s*(?:async\s+)?(?:def|class)\s+[A-Za-z_]", re.M)

# tool_use JSON 的协议字段不是任务实体，参与匹配会制造假关联
_PROTOCOL_WORDS = {
    "tool_use", "tool_result", "thinking", "file_path", "old_string",
    "new_string", "replace_all", "timeout_seconds", "timeout", "description",
    "command", "content", "path", "query", "top_k", "user_id", "session_id",
    "request_id", "created_at", "message_index", "parent_id", "role", "kind",
}


def _primary_path(text):
    """出现次数最多的文件路径，用来近似“这条证据属于哪个任务”。"""
    counts = {}
    for raw in _PATH_RE.findall(text or ""):
        path = raw.replace("/testbed/", "")
        counts[path] = counts.get(path, 0) + 1
    return max(counts, key=counts.get) if counts else None


class EvidenceSpan:
    """一条可回溯的证据：位置指向 Parent 中的原始消息。"""

    def __init__(self, message_index, start, end, role, kind, body,
                 file_paths=(), strong=(), test_names=(), inherited=(),
                 primary_path=None):
        self.message_index = message_index
        self.start = start
        self.end = end
        self.role = role
        self.kind = kind
        self.body = body
        self.file_paths = frozenset(file_paths)
        self.strong = frozenset(strong)
        self.test_names = frozenset(test_names)
        self.inherited = frozenset(inherited)
        self.primary_path = primary_path


def _entities(text):
    """返回 (文件路径, 强实体)；强实体只收具体到任务的符号。"""
    paths = set()
    for raw in _PATH_RE.findall(text or ""):
        path = raw.replace("/testbed/", "")
        paths.add(path)
        paths.add(path.rsplit("/", 1)[-1])
    strong = set()
    for word in _WORD_RE.findall(text or ""):
        key = word.strip("_").lower()
        if not key or key in _PROTOCOL_WORDS or word.startswith("__"):
            continue
        # 异常名（ValueError/KeyError）在无关任务里也常出现，只作证据类型，
        # 不作任务关联实体，否则会把别的任务的总结/patch 拼进来。
        if word.startswith("test_") and len(word) > 5:
            strong.add(word)
        elif "_" in word.strip("_") and len(word) >= 5:
            strong.add(word)
    return paths, strong


def _parse_patch(index, name, data, content_len):
    """把 tool_use Edit/Write/MultiEdit 还原成可读的补丁文本。"""
    path = (data.get("file_path") or "").replace("/testbed/", "")
    diffs, source_text = [], []
    if name == "Edit":
        old, new = data.get("old_string") or "", data.get("new_string") or ""
        diff = "\n".join(difflib.unified_diff(
            old.splitlines(), new.splitlines(), fromfile="a/" + path,
            tofile="b/" + path, n=2, lineterm=""))
        if diff:
            diffs.append(diff)
        source_text.extend([path, old, new])
    elif name == "MultiEdit":
        for edit in data.get("edits") or []:
            old = edit.get("old_string") or ""
            new = edit.get("new_string") or ""
            diff = "\n".join(difflib.unified_diff(
                old.splitlines(), new.splitlines(), fromfile="a/" + path,
                tofile="b/" + path, n=2, lineterm=""))
            if diff:
                diffs.append(diff)
            source_text.extend([old, new])
        source_text.append(path)
    elif name == "Write":
        content = data.get("content") or ""
        if content:
            diffs.append("--- /dev/null\n+++ b/{}\n{}".format(path, content))
        source_text.extend([path, content])
    if not diffs:
        return None
    paths, strong = _entities("\n".join(source_text))
    # 范围覆盖整条消息，BM25 命中这条 tool_use 时仍能把它认作 anchor
    return EvidenceSpan(index, 0, content_len, "assistant", "patch",
                        "\n".join(diffs),
                        file_paths=paths | ({path} if path else ()),
                        strong=strong, primary_path=path or None)


def _parse_assistant(index, content):
    if not content.strip() or content.startswith("[thinking]"):
        return []
    if content.startswith("[tool_use Bash]"):
        match = _TOOL_RE.match(content)
        try:
            data = json.loads(match.group(2)) if match else {}
        except json.JSONDecodeError:
            return []
        command = data.get("command") or ""
        if not command or not _TEST_CMD_RE.search(command):
            return []
        paths, strong = _entities(command)
        return [EvidenceSpan(index, 0, len(content), "assistant", "test",
                             "$ " + command, file_paths=paths, strong=strong,
                             test_names=_TEST_NAME_RE.findall(command),
                             primary_path=_primary_path(command))]
    if content.startswith("[tool_use"):
        match = _TOOL_RE.match(content)
        if not match or match.group(1) not in ("Edit", "Write", "MultiEdit"):
            return []
        try:
            data = json.loads(match.group(2))
        except json.JSONDecodeError:
            return []
        span = _parse_patch(index, match.group(1), data, len(content))
        return [span] if span else []
    # 末尾的普通 assistant 文本视为总结候选；阈值过滤掉过程性短句
    if len(content) >= 200:
        paths, strong = _entities(content)
        return [EvidenceSpan(index, 0, len(content), "assistant", "summary",
                             content, file_paths=paths, strong=strong,
                             primary_path=_primary_path(content))]
    return []


def _classify_result(text):
    low = text.lower()
    if ("traceback (most recent call last)" in low
            or re.match(r"\s*exit code [1-9]", low)):
        return "error"
    if (re.search(r"\b\d+ (?:passed|failed|error|skipped)\b", low)
            or re.search(r"(?:^|\n)(?:FAILED|PASSED) ", text)
            or re.search(r"=+ .*?\b(?:passed|failed)\b.*? =+", low)):
        return "test"
    if _NUMBERED_LINE_RE.search(text) or _DEF_RE.search(text):
        return "source"
    return None


def _parse_user(index, content, inherited):
    if not content.startswith(_TOOL_RESULT):
        paths, strong = _entities(content)
        return [EvidenceSpan(index, 0, len(content), "user", "problem",
                             content, file_paths=paths, strong=strong,
                             primary_path=_primary_path(content))]
    body = content[len(_TOOL_RESULT):].lstrip("\n ")
    kind = _classify_result(body)
    if not kind:
        return []
    start = len(content) - len(body)
    paths, strong = _entities(body)
    inherited_paths, inherited_strong, inherited_primary = inherited
    return [EvidenceSpan(index, start, len(content), "user", kind, body,
                         file_paths=paths, strong=strong,
                         test_names=_TEST_NAME_RE.findall(body),
                         inherited=inherited_paths | inherited_strong,
                         primary_path=(_primary_path(body)
                                       or inherited_primary))]


def _tool_entities(content):
    """工具调用自带的实体，供紧随其后的 tool_result 继承。"""
    empty = (frozenset(), frozenset(), None)
    if not content.startswith("[tool_use"):
        return empty
    match = _TOOL_RE.match(content)
    if not match:
        return empty
    paths, strong = _entities(match.group(2))
    return (paths, strong, _primary_path(match.group(2)))


def _clip_lines(text, limit):
    """按完整行截断，避免切坏代码行或 diff hunk。"""
    if len(text) <= limit:
        return text
    head = text[:limit]
    cut = head.rfind("\n")
    if cut <= 0:
        return head
    return head[:cut] + "\n..."


def _focus_source(text, symbols, limit, anchor_range=None):
    """长源码只保留命中符号附近的完整行；anchor 命中时以 anchor 位置为中心。"""
    if len(text) <= limit:
        return text
    lines = text.splitlines()
    target = 0
    if anchor_range:
        target = text[:anchor_range[0]].count("\n")
    else:
        for number, line in enumerate(lines):
            if any(symbol in line for symbol in symbols):
                target = number
                break
    start = max(0, target - 20)
    picked, used = [], 0
    for line in lines[start:]:
        if used + len(line) + 1 > limit:
            break
        picked.append(line)
        used += len(line) + 1
    if start > 0:
        picked.insert(0, "...")
    return "\n".join(picked)


def _head_tail(text, limit):
    """错误日志保留首尾关键行，中间省略。"""
    if len(text) <= limit:
        return text
    head = text[: limit * 2 // 3]
    tail = text[-(limit - len(head) - 20):]
    return head + "\n...\n" + tail


def _section_header(span, relevant):
    header = "[{} | m={}".format(span.kind, span.message_index)
    if span.primary_path:
        header += " | " + span.primary_path
    elif span.file_paths:
        header += " | " + sorted(span.file_paths, key=len)[-1]
    elif span.strong:
        matched = sorted(span.strong & relevant)
        if matched:
            header += " | " + matched[0]
    return header + "]"


class EvidenceBuilder:
    """Parent 内证据解析缓存 + 选择 + 渲染（Parent 内容不可变，按 id 缓存）。"""

    def __init__(self):
        self._cache = {}

    def clear(self):
        self._cache.clear()

    def _spans(self, mem):
        mid = mem.get("id") or id(mem)
        if mid not in self._cache:
            spans, inherited = [], (frozenset(), frozenset(), None)
            for index, message in enumerate(mem.get("messages") or []):
                content = message.get("content") or ""
                role = message.get("role") or ""
                if role == "assistant":
                    spans.extend(_parse_assistant(index, content))
                    inherited = _tool_entities(content)
                elif role == "user":
                    spans.extend(_parse_user(index, content, inherited))
                    inherited = (frozenset(), frozenset(), None)
                else:
                    inherited = (frozenset(), frozenset(), None)
            # 只有末尾的普通 assistant 文本才算总结；中途的推理不算证据
            summaries = [s for s in spans if s.kind == "summary"]
            if summaries:
                last = max(summaries, key=lambda s: s.message_index)
                spans = [s for s in spans
                         if s.kind != "summary" or s is last]
            self._cache[mid] = spans
        return self._cache[mid]

    def render(self, mem, anchor_unit, query, max_chars):
        """返回组装好的证据文本；放不下时由调用方跳过该候选。"""
        limit = min(int(max_chars), MAX_PARENT_CONTENT_CHARS)
        if limit <= 0 or not mem.get("messages"):
            return ""
        anchor_index, anchor_start, anchor_end = (
            anchor_unit[1], anchor_unit[3], anchor_unit[4])
        query_paths, query_strong = _entities(query or "")
        relevant = frozenset(query_paths | query_strong)

        spans = list(self._spans(mem))
        anchor_span = next(
            (s for s in spans
             if s.message_index == anchor_index and s.start <= anchor_end
             and s.end >= anchor_start), None)
        if anchor_span is None:
            # BM25 命中的是 thinking 等未分类片段时，仍保留原文；标成 note
            # 而不是 source/patch，避免把推理伪装成可执行证据。
            message = mem["messages"][anchor_index].get("content") or ""
            text = message[anchor_start:anchor_end]
            if text.startswith("[thinking]"):
                text = text[len("[thinking]"):].lstrip()
            paths, strong = _entities(text)
            anchor_span = EvidenceSpan(
                anchor_index, anchor_start, anchor_end,
                mem["messages"][anchor_index].get("role") or "",
                "note", text, file_paths=paths, strong=strong)
            spans.append(anchor_span)

        anchor_relevant = set(anchor_span.strong | anchor_span.file_paths)
        if anchor_span.primary_path:
            anchor_relevant.add(anchor_span.primary_path)
            anchor_relevant.add(anchor_span.primary_path.rsplit("/", 1)[-1])
        relation = relevant | frozenset(anchor_relevant)

        def same_group(span):
            """主文件不同的证据属于另一条任务，除非 query 明确点了它的符号/文件。"""
            if (span is anchor_span or not span.primary_path
                    or not anchor_span.primary_path):
                return True
            if span.primary_path == anchor_span.primary_path:
                return True
            basename = span.primary_path.rsplit("/", 1)[-1]
            return (bool(span.strong & query_strong)
                    or span.primary_path in query_paths or basename in query_paths)

        related = []
        for span in spans:
            shared_entities = (span.strong | span.file_paths) & relation
            # 工具结果可以继承工具调用的实体；错误日志只认直接匹配，避免
            # “pytest 没装”这类环境噪声被当成任务证据。
            inherited = bool(span.inherited & relation) and span.kind != "error"
            if not (span is anchor_span or shared_entities or inherited):
                continue
            if not same_group(span):
                continue
            if span.kind in HIGH_STAKES and span is not anchor_span:
                # 单个通用符号（如 replace_column）不足以把别的任务拼进来
                path_shared = bool(span.primary_path
                                   and span.primary_path in relation)
                strong_query = bool(span.strong & query_strong)
                if not (inherited or strong_query
                        or (path_shared and shared_entities)
                        or len(span.strong & relation) >= 2):
                    continue
            related.append(span)

        def score(span):
            direct = len((span.strong | span.file_paths) & relevant) * 3
            relation_hits = len((span.strong | span.file_paths) & relation) * 2
            if span.primary_path and span.primary_path in relation:
                relation_hits += 2
            return (span is anchor_span, direct + relation_hits,
                    span.message_index)

        parts, used = [], 0
        for kind in SECTION_ORDER:
            candidates = sorted((s for s in related if s.kind == kind),
                                key=score, reverse=True)
            for span in candidates[:MAX_SECTIONS[kind]]:
                header = _section_header(span, relation)
                room = min(MAX_SECTION_CHARS[kind],
                           limit - used - len(header) - 2)
                if room < 80:
                    continue
                if span.kind == "source":
                    offset = max(0, anchor_start - span.start)
                    body = _focus_source(
                        span.body, relation, room,
                        (offset, anchor_end) if span is anchor_span else None)
                elif span.kind == "error":
                    body = _head_tail(span.body, room)
                else:
                    body = _clip_lines(span.body, room)
                if not body.strip():
                    continue
                block = header + "\n" + body
                extra = 2 if parts else 0
                if used + len(block) + extra > limit:
                    continue
                parts.append(block)
                used += len(block) + extra
        return "\n\n".join(parts)
