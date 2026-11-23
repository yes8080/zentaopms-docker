"""Edit selected Compose dotenv fields while preserving unrelated text and comments."""

import errno
import os
import re
import stat
from pathlib import Path


_FIELD = re.compile(r"[ \t]*(?:export[ \t]+)?([\w.-]+)")
_SEPARATOR = re.compile(r"[ \t]*[=:][ \t]*")
_TARGET_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _read(path):
    if any(parent.is_symlink() for parent in path.parents):
        raise ValueError(f"环境文件目录含符号链接：{path}")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return ""
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise ValueError(f"环境文件是符号链接：{path}") from None
        raise
    with os.fdopen(fd, encoding="utf-8", newline="") as source:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError(f"环境文件不是普通文件：{path}")
        try:
            return source.read()
        except UnicodeError:
            raise ValueError(f"环境文件需要 UTF-8 编码：{path}") from None


def _quote(key, value):
    if not isinstance(value, str) or "\x00" in value or "\r" in value:
        raise ValueError(f"环境变量 {key} 的值含不支持的字符。")
    # Compose preserves backslashes in single quotes except the one escaping a quote.
    # An odd number before the delimiter cannot round-trip in this representation.
    for part in value.split("'"):
        if (len(part) - len(part.rstrip("\\"))) % 2:
            raise ValueError(f"环境变量 {key} 的反斜杠位置不适合 Compose 单引号。")
    return "'" + value.replace("'", "\\'") + "'"


def _line_end(text, start):
    newline = text.find("\n", start)
    if newline == -1:
        return len(text), len(text)
    end = newline - 1 if newline and text[newline - 1] == "\r" else newline
    return end, newline + 1


def _value_end(text, start, line):
    """Find the end of the value and the start of the next assignment."""
    if text[start:start + 1] in ("'", '"'):
        quote = text[start]
        i = start + 1
        while i < len(text):
            if text[i] == "\\":
                i += 2
            elif text[i] == quote:
                end, following = _line_end(text, i + 1)
                suffix = text[i + 1:end].lstrip(" \t")
                if suffix and not suffix.startswith("#"):
                    raise ValueError(f"环境文件第 {line} 行的引号后有含义不明的内容。")
                return i + 1, following
            else:
                i += 1
        raise ValueError(f"环境文件第 {line} 行的引号没有闭合。")

    end, following = _line_end(text, start)
    value = text[start:end]
    comment = re.search(r"[ \t]+#", value)
    if comment:
        value = value[:comment.start()]
    return start + len(value.rstrip(" \t")), following


def render_env(path: Path, updates: dict[str, str]) -> str:
    text = _read(Path(path))
    values = {}
    for key, value in updates.items():
        if not isinstance(key, str) or not _TARGET_KEY.fullmatch(key):
            raise ValueError("不支持的环境变量名称。")
        values[key] = _quote(key, value)
    if not values:
        return text
    changes = []
    seen = set()
    start = 1 if text.startswith("\ufeff") else 0
    line = 1
    while start < len(text):
        end, following = _line_end(text, start)
        content = text[start:end].lstrip(" \t")
        if content and not content.startswith("#"):
            field = _FIELD.match(text, start)
            if not field:
                raise ValueError(f"无法安全解析环境文件第 {line} 行。")
            key = field.group(1)
            separator = _SEPARATOR.match(text, field.end())
            if separator:
                value_start = separator.end()
                value_end, following = _value_end(text, value_start, line)
                prefix = ""
            elif not text[field.end():end].strip(" \t"):
                value_start = value_end = field.end()
                prefix = "="
            else:
                raise ValueError(f"无法安全解析环境文件第 {line} 行。")
            if key in values:
                if key in seen:
                    raise ValueError(f"环境文件重复定义了 {key}，请合并后重试。")
                seen.add(key)
                changes.append((value_start, value_end, prefix + values[key]))
        line += text.count("\n", start, following)
        start = following

    newline = "\r\n" if "\r\n" in text else "\n"
    for start, end, replacement in reversed(changes):
        text = text[:start] + replacement + text[end:]
    missing = [key for key in values if key not in seen]
    if missing:
        if text and text != "\ufeff" and not text.endswith("\n"):
            text += newline
        text += "".join(f"{key}={values[key]}{newline}" for key in missing)
    return text


def write_env(path: Path, updates: dict[str, str]):
    from .common import atomic_write

    atomic_write(path, render_env(path, updates), mode=0o600)
