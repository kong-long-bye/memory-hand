# -*- coding: utf-8 -*-
"""Evidence Builder：在同一个 Parent 内选择与查询相关的证据并渲染。

两种渲染模式（env RETRIEVAL_MODE，默认 v2_1，可一键回滚）：
- v2_1：固定分节 problem → note → source → error → patch → test → summary；
- v3_0：先按"路径 + 符号"二元强关联把 patch/test/summary 绑定到当前问题，
  判定 solution_status（verified_solution / patch_available / summary_only /
  source_only / related_only），再按状态决定渲染顺序、预算与诚实声明；
  绑不上的补丁/总结一律不渲染（False Solution 门禁）。

只改变返回内容的组织方式，不参与打分：BM25、Parent 聚合和全局预算都保持
V1.1 不变。安全规则：
- BM25 命中的最高分 unit 所在消息永远是 anchor，保证命中不丢；同 Parent 的
  前多个高分 unit 一起作为锚点，只合并实体，不改变 Parent 的分数与排序；
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
import os
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

# ---- V3.0-lite：解答状态判定与答案优先渲染 ----
# 只有绑定到当前问题的 patch/test/summary 才允许进入"解决方案"分节
SOLUTION_KINDS = ("patch", "test", "summary")
# 每种状态的单条内容预算（实验起点，仍受 MAX_PARENT_CONTENT_CHARS 与全局预算约束）
STATUS_BUDGET = {
    "verified_solution": 8000,
    "patch_available": 8000,
    "summary_only": 4000,
    "source_only": 4000,
    "related_only": 1000,
}
# 每种状态的分节顺序：答案性证据放最前，保证前 2KB 内出现补丁/测试
STATUS_ORDER = {
    "verified_solution": ("problem", "patch", "test", "source", "error",
                          "summary"),
    "patch_available": ("problem", "patch", "test", "source", "error",
                        "summary"),
    "summary_only": ("problem", "summary", "source", "error", "test"),
    "source_only": ("problem", "source", "error", "test", "note"),
    "related_only": ("note", "problem"),
}
# 诚实声明：明确告诉 Agent 记忆里的证据强度，防止把历史片段误当本题答案
STATUS_NOTE = {
    "verified_solution": (
        "NOTE: the patch and test records above were verified in a PAST "
        "session; the current repository may differ. Re-verify before "
        "applying."),
    "patch_available": (
        "NOTE: a historical patch was found but no trusted passing-test "
        "record. Re-verify before applying."),
    "summary_only": (
        "NOTE: only a historical fix summary for this problem; no directly "
        "recoverable patch in memory."),
    "source_only": (
        "NOTE: this memory contains NO verified patch, fix summary, or "
        "passing test for this problem. Above is historical source only "
        "(may differ from the current repository); design and verify the "
        "fix yourself."),
    "related_only": (
        "NOTE: only topically related historical fragments; not a solution "
        "to this problem."),
}
# 测试结果判定：必须"有 passed 且无 failed/error"才算历史验证通过
_TEST_PASS_RE = re.compile(r"\b\d+ passed\b", re.I)
_TEST_FAIL_RE = re.compile(
    r"\b\d+ failed\b|\b\d+ errors?\b|\bFAILED\b|\bERROR\b", re.I)

# 消息标记同时兼容 "[tool_use Bash] {...}" 与 "tool_use Bash {...}"。
# 用 (?!\w) 而非 \b，避免 thinking_about / tool_usage /
# tool_result_summary 这类普通标识符被误判为消息标记。
_TOOL_MARK = (
    r"(?:\[tool_use (?P<bracket>[A-Za-z_][A-Za-z0-9_]*)\]"
    r"|tool_use (?P<plain>[A-Za-z_][A-Za-z0-9_]*))")
_TOOL_RE = re.compile(r"^" + _TOOL_MARK + r"\s*(?P<data>\{.*\})\s*\Z", re.S)
_TOOL_MARK_RE = re.compile(r"^" + _TOOL_MARK)
_TOOL_RESULT_RE = re.compile(
    r"^(?:\[tool_result\]|tool_result(?!\w)(?:\s*:\s*)?)")
_THINKING_RE = re.compile(r"^(?:\[thinking\]|thinking(?!\w))")
_TEST_CMD_RE = re.compile(
    r"\b(?:pytest|py\.test|unittest|tox|nox)\b|"
    r"(?:npm|yarn|pnpm)\s+test|\bgo\s+test\b|\bcargo\s+test\b", re.I)
# 扩展名白名单跨语言；结尾 \b 防止 .cc 被截成 .c、.pyx 被截成 .py
_PATH_EXTS = (
    "py|pyi|pyx|pxd|ipynb|"
    "java|kt|kts|scala|groovy|gradle|gvy|clj|cljs|cljc|edn|clojure|"
    "cs|csx|fs|fsx|vb|swift|m|mm|h|"
    "c|cc|cpp|cxx|hpp|hh|ino|rs|go|dart|php|rb|rake|gemspec|podspec|"
    "ex|exs|erl|hrl|behav|hs|lhs|ml|mli|r|jl|lua|pl|pm|t|nim|zig|"
    "ts|tsx|js|jsx|mjs|cjs|vue|svelte|astro|wxml|wxss|"
    "html|htm|xhtml|xml|css|scss|sass|less|sql|proto|thrift|tf|tfvars|"
    "sh|bash|zsh|fish|ps1|psm1|bat|cmd|mk|mak|cmake|"
    "toml|json|jsonc|json5|ya?ml|yml|properties|cfg|conf|ini|env|"
    "txt|md|markdown|rst|adoc|tex|org"
)
_PATH_RE = re.compile(
    r"(?:/testbed/)?(?:[\w.-]+/)*[\w.-]+\.(?:" + _PATH_EXTS + r")\b", re.I) #提取文件路径
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
        # 该证据在 Parent 消息列表中的索引位置
        self.message_index = message_index
        # 证据内容在消息 text 中的起止偏移量，用于回溯原文
        self.start = start
        self.end = end
        # 消息角色（assistant / user）
        self.role = role
        # 证据类型：problem / note / source / error / patch / test / summary
        self.kind = kind
        # 证据的原始文本内容
        self.body = body
        # 该证据中提到的所有文件路径集合
        self.file_paths = frozenset(file_paths)
        # 强实体集合（函数名、类名、测试名等任务相关符号）
        self.strong = frozenset(strong)
        # 该证据中出现的测试用例名（test_xxx）
        self.test_names = frozenset(test_names)
        # 从前一条工具调用继承的实体，用于 tool_result 关联上下文
        self.inherited = frozenset(inherited)
        # 该证据的主要文件路径（出现次数最多的路径），用于判断是否属于同一任务
        self.primary_path = primary_path


def _entities(text):
    """返回 (文件路径, 强实体)；强实体只收具体到任务的符号。"""
    paths = set()
    # 提取文件路径，去掉testbed前缀，并添加文件名作为路径
    for raw in _PATH_RE.findall(text or ""):
        path = raw.replace("/testbed/", "")
        paths.add(path)
        paths.add(path.rsplit("/", 1)[-1])

    strong = set()
    # 提取强实体，也就是函数名、类名、测试名等任务相关符号
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


def _tool_call(content):
    """解析两种格式的 tool_use，返回 (工具名, 参数字典, JSON 原文)。

    只识别消息开头的标记；JSON 无效/缺失时仍返回工具名与原文，由调用方
    决定是否产生证据，避免不完整消息被误当成普通总结。
    """
    mark = _TOOL_MARK_RE.match(content)
    if not mark:
        return None, None, None
    name = mark.group("bracket") or mark.group("plain")
    match = _TOOL_RE.match(content)
    if not match:
        return name, None, None
    raw = match.group("data")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return name, None, raw
    if not isinstance(data, dict):
        return name, None, raw
    return name, data, raw


def _parse_assistant(index, content):
    # thinking 是思考内容，无论带不带方括号都不保留
    if not content.strip() or _THINKING_RE.match(content):
        return []
    name, data, _ = _tool_call(content)
    if name:
        # 不完整的工具消息不回落成总结/补丁
        if data is None:
            return []
        # 判断是否是测试命令
        if name == "Bash":
            command = data.get("command") or ""
            if not command or not _TEST_CMD_RE.search(command):
                return []
            # 提取文件路径和强实体，用于证据类型判断和关联实体
            paths, strong = _entities(command)
            return [EvidenceSpan(index, 0, len(content), "assistant", "test",
                                 "$ " + command, file_paths=paths,
                                 strong=strong,
                                 test_names=_TEST_NAME_RE.findall(command),
                                 primary_path=_primary_path(command))]
        if name in ("Edit", "Write", "MultiEdit"):
            # 解析工具调用，提取证据
            span = _parse_patch(index, name, data, len(content))
            return [span] if span else []
        return []
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

# 解析 user 文本，提取证据
def _parse_user(index, content, inherited):
    # 判断是否是工具调用结果
    marker = _TOOL_RESULT_RE.match(content)
    if not marker:
        paths, strong = _entities(content)
        return [EvidenceSpan(index, 0, len(content), "user", "problem",
                             content, file_paths=paths, strong=strong,
                             primary_path=_primary_path(content))]
    body = content[marker.end():].lstrip("\n ")
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
    _, _, raw = _tool_call(content)
    if raw is None:
        return empty
    paths, strong = _entities(raw)
    return (paths, strong, _primary_path(raw))


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


def _span_paths(span):
    paths = set(span.file_paths)
    if span.primary_path:
        paths.add(span.primary_path)
    return paths


def _binds(span, focus_paths, focus_strong, query_strong):
    """二元强关联：这条证据和当前问题是不是同一件事。

    满足其一即绑定（对齐 V3.0 计划 §6.1 的"显式重合"与"工具调用链"）：
    - 文件重合：证据路径（含继承自前一工具调用的路径，如测试结果之于
      测试命令）与问题焦点路径有交集；
    - 符号重合：与焦点共享 >= 2 个强符号；query 明确点名其中 1 个时，
      再共享任意 1 个焦点符号也算。
    仅同 Parent、仅时间邻近、仅共享宽泛类名都不算绑定。
    """
    inherited = span.inherited
    if (_span_paths(span) & focus_paths) or (inherited & focus_paths):
        return True
    shared = (span.strong | inherited) & focus_strong
    if len(shared) >= 2:
        return True
    return bool((span.strong | inherited) & query_strong) and bool(shared)


def _test_passed(span):
    return bool(_TEST_PASS_RE.search(span.body)
                and not _TEST_FAIL_RE.search(span.body))


class EvidenceBuilder:
    """Parent 内证据解析缓存 + 选择 + 渲染（Parent 内容不可变，按 id 缓存）。

    mode: "v2_1"（固定分节，默认）或 "v3_0"（解答状态判定 + 答案优先渲染）。
    未显式传入时读 env RETRIEVAL_MODE，非法值一律回落 v2_1。
    """

    def __init__(self, mode=None):
        raw = (mode or os.environ.get("RETRIEVAL_MODE")
               or "v2_1").strip().lower()
        self.mode = "v3_0" if raw in ("v3_0", "v3") else "v2_1"
        self._cache = {}

    def clear(self):
        self._cache.clear()

    def _spans(self, mem):
        mid = mem.get("id") or id(mem)
        if mid not in self._cache:
            #spans 是一个空列表，用来存放提取出的证据。
            # inherited 用来记录上一个 assistant 的工具调用信息，供当前 user 继承
            spans, inherited = [], (frozenset(), frozenset(), None)
            for index, message in enumerate(mem.get("messages") or []):
                content = message.get("content") or ""
                role = message.get("role") or ""
                if role == "assistant":
                    # 解析 assistant 文本，提取证据
                    spans.extend(_parse_assistant(index, content))
                    inherited = _tool_entities(content)
                elif role == "user":
                    # 解析 user 文本，提取证据
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
    def render(self, mem, anchor_units, query, max_chars):
        """返回组装好的证据文本；放不下时由调用方跳过该候选。

        anchor_units 可以是单个 unit，也可以是同一 Parent 内按分数排序的
        多个 unit；多个锚点时实体取并集，但每个 Parent 仍只渲染一条内容。
        mode=v3_0 时先判定解答状态，再按状态答案优先渲染。
        """
        limit = min(int(max_chars), MAX_PARENT_CONTENT_CHARS)
        if limit <= 0 or not mem.get("messages"):
            return ""
        if isinstance(anchor_units, tuple):
            anchor_units = [anchor_units]
        if not anchor_units:
            return ""
        sel = self._select(mem, anchor_units, query)
        if self.mode == "v3_0":
            return self._render_v3(mem, sel, limit)
        return self._render_sections(sel, limit)

    def _select(self, mem, anchor_units, query):
        """锚点解析 + 实体关联选择，两种渲染模式共享（与 V2.1 完全一致）。"""
        # 得到query的路径和强实体，作为anchor的关联实体；query也提取路径和强实体
        query_paths, query_strong = _entities(query or "")
        #合并集合，自动去重
        relevant = frozenset(query_paths | query_strong)

        spans = list(self._spans(mem))
        anchor_spans, anchor_ranges = [], {}
        for unit in anchor_units:
            anchor_index, anchor_start, anchor_end = unit[1], unit[3], unit[4]
            anchor_span = next(
                (s for s in spans
                 if s.message_index == anchor_index and s.start <= anchor_end
                 and s.end >= anchor_start), None)
            if anchor_span is None:
                # BM25 命中的是 thinking 等未分类片段时，仍保留原文；标成 note
                # 而不是 source/patch，避免把推理伪装成可执行证据。
                message = mem["messages"][anchor_index].get("content") or ""
                text = message[anchor_start:anchor_end]
                thinking = _THINKING_RE.match(text)
                if thinking:
                    text = text[thinking.end():].lstrip()
                paths, strong = _entities(text)
                anchor_span = EvidenceSpan(
                    anchor_index, anchor_start, anchor_end,
                    mem["messages"][anchor_index].get("role") or "",
                    "note", text, file_paths=paths, strong=strong)
                spans.append(anchor_span)
            if anchor_span in anchor_spans:
                # 同一条消息的多个块同时命中时合并覆盖范围
                start, end = anchor_ranges[id(anchor_span)]
                anchor_ranges[id(anchor_span)] = (
                    min(start, anchor_start), max(end, anchor_end))
                continue
            anchor_spans.append(anchor_span)
            anchor_ranges[id(anchor_span)] = (anchor_start, anchor_end)

        relation = set(relevant)
        for anchor_span in anchor_spans:
            relation |= anchor_span.strong | anchor_span.file_paths
            if anchor_span.primary_path:
                relation.add(anchor_span.primary_path)
                relation.add(anchor_span.primary_path.rsplit("/", 1)[-1])
        relation = frozenset(relation)

        def same_group(span):
            """主文件不同的证据属于另一条任务，除非 query 明确点了它的符号/文件。"""
            if span in anchor_spans:
                return True
            if not span.primary_path:
                return True
            anchor_paths = [a.primary_path for a in anchor_spans if a.primary_path]
            # thinking 之类的锚点没有主文件，不能因此对所有任务开门
            if not anchor_paths:
                return True
            if span.primary_path in anchor_paths:
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
            if not (span in anchor_spans or shared_entities or inherited):
                continue
            if not same_group(span):
                continue
            if span.kind in HIGH_STAKES and span not in anchor_spans:
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
            return (span in anchor_spans, direct + relation_hits,
                    span.message_index)

        return {
            "related": related,
            "score": score,
            "relation": relation,
            "relevant": relevant,
            "anchor_spans": anchor_spans,
            "anchor_ranges": anchor_ranges,
            "query_paths": query_paths,
            "query_strong": query_strong,
        }

    def _render_sections(self, sel, limit):
        """V2.1 原路径：固定 SECTION_ORDER 分节渲染。"""
        related, score = sel["related"], sel["score"]
        relation = sel["relation"]
        anchor_spans, anchor_ranges = sel["anchor_spans"], sel["anchor_ranges"]
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
                    anchor_range = anchor_ranges.get(id(span))
                    body = _focus_source(
                        span.body, relation, room,
                        (max(0, anchor_range[0] - span.start), anchor_range[1])
                        if anchor_range else None)
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

    def _render_v3(self, mem, sel, limit):
        """V3.0-lite：绑定判定 → solution_status → 答案优先渲染。

        False Solution 门禁：绑不上当前问题的 patch/test/summary 一律不渲染；
        状态与诚实声明永远输出，Agent 能明确知道记忆里有没有已验证的修复。
        """
        related = sel["related"]
        anchor_spans = sel["anchor_spans"]
        query_strong = sel["query_strong"]
        relation = sel["relation"]
        anchor_ranges = sel["anchor_ranges"]

        # 问题焦点 = query 实体 ∪ 最高分锚点实体。故意不含次锚点：top-3 锚点
        # 可能横跨同 Parent 的两条子任务，若都算焦点，B 任务的补丁会被绑成
        # A 问题的答案（T2 负例）。上下文证据仍用全量 relation，只有
        # patch/test/summary 的"是否同问题"判定收紧到主锚点。
        focus_paths = set(sel["query_paths"])
        focus_strong = set(query_strong)
        top_anchor = anchor_spans[0]
        focus_paths |= _span_paths(top_anchor)
        focus_strong |= top_anchor.strong

        bound_patches = [s for s in related if s.kind == "patch"
                         and _binds(s, focus_paths, focus_strong, query_strong)]
        patch_paths, patch_strong = set(), set()
        for span in bound_patches:
            patch_paths |= _span_paths(span)
            patch_strong |= span.strong

        def binds_solution(span):
            return _binds(span, focus_paths | patch_paths,
                          focus_strong | patch_strong, query_strong)

        bound_tests = [s for s in related
                       if s.kind == "test" and binds_solution(s)]
        bound_summaries = [s for s in related
                           if s.kind == "summary" and binds_solution(s)]
        passed_tests = [t for t in bound_tests if _test_passed(t)]

        kinds = {s.kind for s in related}
        if bound_patches and passed_tests:
            status = "verified_solution"
        elif bound_patches:
            status = "patch_available"
        elif bound_summaries:
            status = "summary_only"
        elif kinds & {"source", "error", "problem", "test"}:
            status = "source_only"
        else:
            status = "related_only"

        budget = min(limit, STATUS_BUDGET[status])
        note = STATUS_NOTE[status]
        header = "[solution_status={} | parent={} | anchors={}]".format(
            status, mem.get("id") or "-",
            ",".join("m{}".format(s.message_index) for s in anchor_spans))
        # 状态头和诚实声明必须放得下，否则宁可返回空也不给半截证据
        reserve = len(header) + len(note) + 4
        if budget - reserve < 80:
            return ""

        bound_ids = {id(s) for s in bound_patches + bound_tests
                     + bound_summaries}
        content_budget = budget - len(note) - 2
        parts, used, seen = [header], len(header), set()
        for kind in STATUS_ORDER[status]:
            candidates = [s for s in related if s.kind == kind
                          and (kind not in SOLUTION_KINDS
                               or id(s) in bound_ids)]
            if not candidates:
                continue
            if kind in ("patch", "test"):
                # 版本序列按时间保留：多次修改时展示最终状态（最后 2 条）
                candidates.sort(key=lambda s: s.message_index)
                if len(candidates) > MAX_SECTIONS[kind]:
                    candidates = candidates[-MAX_SECTIONS[kind]:]
            else:
                candidates.sort(key=sel["score"], reverse=True)
                candidates = candidates[:MAX_SECTIONS[kind]]
            for span in candidates:
                # 去重：相同正文的源码/补丁只渲染一次
                key = (span.kind, span.primary_path, span.body)
                if key in seen:
                    continue
                header_line = _section_header(span, relation)
                room = min(MAX_SECTION_CHARS[kind],
                           content_budget - used - len(header_line) - 2)
                if room < 80:
                    continue
                if span.kind == "source":
                    anchor_range = anchor_ranges.get(id(span))
                    body = _focus_source(
                        span.body, relation, room,
                        (max(0, anchor_range[0] - span.start), anchor_range[1])
                        if anchor_range else None)
                elif span.kind == "error":
                    body = _head_tail(span.body, room)
                else:
                    body = _clip_lines(span.body, room)
                if not body.strip():
                    continue
                block = header_line + "\n" + body
                extra = 2  # parts 恒含状态头，每个块前都有 "\n\n" 分隔
                if used + len(block) + extra > content_budget:
                    continue
                seen.add(key)
                parts.append(block)
                used += len(block) + extra
        parts.append(note)
        return "\n\n".join(parts)
