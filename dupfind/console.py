"""输出重定向：CLI 直接打印，Web 后台线程各收各的。

不能用 :func:`contextlib.redirect_stdout` —— 它替换的是**进程全局**的
``sys.stdout``。两个处置任务重叠时（双标签页、两个 web 实例、或一个任务
还没结束时又点了「还原」），进入/退出顺序一交错，后退出者恢复的是另一个
任务的 StringIO，进程的 stdout 就永久变成了一个没人读的缓冲：服务端之后
所有输出静默消失，任何需要 ``sys.stdout.fileno()`` 的代码直接抛异常。

这里用线程局部变量：每个线程写自己的缓冲区，互不干扰，也不会动全局
``sys.stdout``。
"""

from __future__ import annotations

import io
import threading

_state = threading.local()


class capture:
    """只捕获**当前线程**的 print 输出。

    用法::

        with capture() as buffer:
            run_restore(trash, options)
        text = buffer.getvalue()
    """

    __slots__ = ("_buffer", "_previous")

    def __init__(self) -> None:
        self._buffer = io.StringIO()
        self._previous: io.StringIO | None = None

    def __enter__(self) -> io.StringIO:
        self._previous = getattr(_state, "sink", None)
        _state.sink = self._buffer
        return self._buffer

    def __exit__(self, *exc_info: object) -> bool:
        _state.sink = self._previous
        return False


def say(*args: object, **kwargs: object) -> None:
    """``print`` 的线程局部版本：捕获中写缓冲区，否则写真正的 stdout。"""
    sink = getattr(_state, "sink", None)
    if sink is None:
        print(*args, **kwargs)
    else:
        print(*args, file=sink, **kwargs)


__all__ = ["capture", "say"]
