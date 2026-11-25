"""JSON 读写：原子写入，避免中断产生半写文件。"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def write_json_atomic(path: str | Path, data: Any) -> None:
    """先写同目录临时文件，再 os.replace 原子替换。"""
    target = os.path.abspath(str(path))
    directory = os.path.dirname(target) or os.getcwd()
    os.makedirs(directory, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=".dupfind-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as fp:
            json.dump(data, fp, ensure_ascii=False, indent=2)
            fp.write("\n")
        os.replace(temporary, target)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def read_json(path: str | Path) -> Any:
    with open(path, encoding="utf-8") as fp:
        return json.load(fp)
