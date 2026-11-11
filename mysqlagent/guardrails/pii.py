"""mysqlagent.guardrails.pii — PII 检测与脱敏。

移植自 Go 版 internal/guardrails/pii.go。
金融合规前置条件：敏感数据必须在进入 LLM 上下文之前完成掩码——
"敏感内容已经进了 prompt，等于已泄露"，事后过滤无效。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Optional

from mysqlagent.guardrails.guardrails import sanitize_tool_result


def _luhn_ok(s: str) -> bool:
    """Luhn 校验：真实银行卡号必过，随机 19 位主键（雪花 ID 等）约 90% 不过。"""
    total = 0
    for i, c in enumerate(reversed(s)):
        d = int(c)
        if i % 2:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _id_card_ok(s: str) -> bool:
    """身份证号合理性：出生日期段必须是合法日期（排除 18 位雪花 ID 等纯数字主键）。"""
    if len(s) == 18:
        y, m, d = int(s[6:10]), int(s[10:12]), int(s[12:14])
        if not 1900 <= y <= 2100:
            return False
    else:  # 15 位旧版：YYMMDD
        m, d = int(s[8:10]), int(s[10:12])
    return 1 <= m <= 12 and 1 <= d <= 31


@dataclass(frozen=True)
class _PIIPattern:
    kind: str
    pattern: re.Pattern
    validator: Optional[Callable[[str], bool]] = None  # 命中后的二次校验（None = 不校验）


# 邮箱 / 中国手机号 / 身份证 / 银行卡。
# 数字类用 (?<!\d)…(?!\d) + re.ASCII 而不是 \b：Python 的 \b 把中文也当单词字符，
# "电话13812345678" 里手机号紧贴汉字时 \b 不成立，会整段漏掩（Go 版 \b 仅认 ASCII，无此问题）。
PII_PATTERNS: list[_PIIPattern] = [
    _PIIPattern("email", re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", re.ASCII)),
    _PIIPattern("phone_cn", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)", re.ASCII)),
    _PIIPattern("id_card_cn", re.compile(r"(?<![\dXx])(?:\d{17}[\dXx]|\d{15})(?![\dXx])", re.ASCII), _id_card_ok),
    _PIIPattern("bank_card", re.compile(r"(?<!\d)\d{16,19}(?!\d)", re.ASCII), _luhn_ok),
]


def mask(s: str) -> str:
    """对单个命中片段做保留首尾的掩码。"""
    r = list(s)
    n = len(r)
    if n <= 2:
        return "*" * n
    if n <= 6:
        return r[0] + "*" * (n - 2) + r[-1]
    return "".join(r[:2]) + "*" * 6 + "…" + "".join(r[-3:])


def mask_pii(s: str) -> tuple[str, list[str]]:
    """对文本中命中的 PII 片段（邮箱 / 中国手机号 / 身份证 / 银行卡）做保留首尾的掩码。

    应用于工具结果进入 LLM 上下文之前。
    返回掩码后的文本与命中的 PII 类型集合（供审计记录，不含原文）。
    """
    if s == "":
        return s, []
    hits: list[str] = []
    for p in PII_PATTERNS:
        found = False

        def _repl(m: "re.Match[str]", p: _PIIPattern = p) -> str:
            nonlocal found
            if p.validator is not None and not p.validator(m.group(0)):
                return m.group(0)  # 形似但校验不过（多半是业务主键），不掩码
            found = True
            return mask(m.group(0))

        s = p.pattern.sub(_repl, s)
        if found:
            hits.append(p.kind)
    return s, hits


def sanitize_tool_result_full(result: str) -> tuple[str, list[str], bool]:
    """工具结果的完整出站处理管线：间接注入隔离 → PII 掩码。

    审计事件只落类型与哈希，不落原文。
    返回 (处理后的文本, PII 类型列表, 是否发生隔离)。
    """
    before = result
    result = sanitize_tool_result(result)
    isolated = result != before
    result, pii_kinds = mask_pii(result)
    return result, pii_kinds, isolated
