"""文件名归一化与词元包含判定。

归一化规则见规格 4.2；包含判定见 4.3（词元子集 + 纯数字额外词元否决）。
"""

from __future__ import annotations

import ntpath
import re

from .config import COPY_MARKERS

_SEPARATORS = re.compile(r"[ \t\r\n\u00a0\u3000\-_.,;()\[\]{}]+")
_WHITESPACE = re.compile(r"\s+")


def to_halfwidth(text: str) -> str:
    """全角转半角：U+FF01..FF5E 映射到 ASCII，全角空格转普通空格。"""
    chars: list[str] = []
    for char in text:
        code = ord(char)
        if code == 0x3000:
            chars.append(" ")
        elif 0xFF01 <= code <= 0xFF5E:
            chars.append(chr(code - 0xFEE0))
        else:
            chars.append(char)
    return "".join(chars)


def _marker_patterns(markers: tuple[str, ...] | list[str]) -> list[re.Pattern[str]]:
    patterns: list[re.Pattern[str]] = []
    for marker in sorted({str(item).strip() for item in markers if str(item).strip()}, key=len, reverse=True):
        normalized = to_halfwidth(marker).lower()
        if not normalized:
            continue
        escaped = re.escape(normalized)
        body = escaped + r"(?:\s*\(\d+\))?"
        if normalized[0].isascii() and normalized[0].isalnum():
            body = r"(?<![0-9a-z])" + body
        patterns.append(re.compile(r"\s*" + body + r"\s*$"))
    # 规格 4.2 第 5 条的通用规则：单个数字的括号/下划线/空格后缀
    patterns.append(re.compile(r"\s*\(\d\)\s*$"))
    patterns.append(re.compile(r"\s*_\d\s*$"))
    patterns.append(re.compile(r"\s+[1-9]\s*$"))
    return patterns


_TRAILING_SEPARATORS = re.compile(r"[\s\-_,.;]+$")


class FilenameNormalizer:
    """可配置副本标记的文件名归一化器。"""

    __slots__ = ("_patterns",)

    def __init__(self, copy_markers: tuple[str, ...] | list[str] = COPY_MARKERS) -> None:
        self._patterns = _marker_patterns(copy_markers)

    def normalize(self, filename: str) -> str:
        text = filename.strip()
        text = to_halfwidth(text).lower()
        stem = ntpath.splitext(text)[0] if ntpath.splitext(text)[1] else text
        text = stem
        # 反复剔除后缀标记，处理 "报告 - 副本" / "报告 - 副本(2)" 之类的叠加
        for _ in range(4):
            changed = False
            for pattern in self._patterns:
                match = pattern.search(text)
                if match is None or match.start() == 0:
                    continue
                text = _TRAILING_SEPARATORS.sub("", text[: match.start()])
                changed = True
            if not changed:
                break
        return _WHITESPACE.sub(" ", text).strip()

    def token_set(self, normalized_name: str) -> frozenset[str]:
        return frozenset(token for token in _SEPARATORS.split(normalized_name) if token)

    @staticmethod
    def is_valid_short_name(normalized_name: str, tokens: frozenset[str]) -> bool:
        """短名归一化长度 ≥ 2 个字符，且至少含一个有意义的词元。

        纯数字词元（如 ``10``）与单字母词元（如 ``a``）不计入命中。
        """
        if len(normalized_name) < 2:
            return False
        return any(not token.isdigit() and len(token) >= 2 for token in tokens)

    @classmethod
    def is_contained(cls, short_name: str, short_tokens: frozenset[str], long_tokens: frozenset[str]) -> bool:
        """short 是否为 long 的词元子集；多出的纯数字词元（长度 > 1）视为非副本，否决。"""
        if not short_tokens or not cls.is_valid_short_name(short_name, short_tokens):
            return False
        if not short_tokens <= long_tokens:
            return False
        for extra in long_tokens - short_tokens:
            if extra.isdigit() and len(extra) > 1:
                return False
        return True
