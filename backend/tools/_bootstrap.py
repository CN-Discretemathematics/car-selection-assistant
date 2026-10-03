"""tools/ 脚本的公共启动引导（2026-10-02 抽取）。

**为什么要抽**
`backend/tools/` 下 19 个脚本逐字重复同一段样板：

    import os
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from app.xxx import Yyy  # noqa: E402

其中 `sys.path.insert(...)` 是**语句**而非 import，于是它之后的每一个
`from app...` 都被 ruff 判为 E402（module level import not at top of file），
全仓因此堆了 96 处 `# noqa: E402` 抑制注释。

**抽成 import 之后 E402 自然消失**：E402 只在「import 之前出现了非 import 的
代码」时触发；`from _bootstrap import ...` 本身是 import 语句，
后面的 `from app...` 就不再违规。**这是根因修复，不是把警告藏起来。**

**用法**（放在模块顶部、`from app...` 之前）：

    from _bootstrap import ensure_backend_on_path  # noqa: F401
    from app.common.database import get_session_factory

`ensure_backend_on_path` 在 import 本模块时**自动执行**（见文件末尾），
显式调用只是为了可读性与可测试性——两者等价，脚本里写不写都不影响行为。

脚本以 `python tools/xxx.py` 运行时 `sys.path[0]` 就是 `tools/`，
被 pytest 以模块方式导入时（`sys.path.insert(0, BACKEND / "tools")`）
同理，因此 `import _bootstrap` 在两种入口下都成立。
"""
from __future__ import annotations

import os
import sys

#: `backend/` 目录（本文件的父目录的父目录）
BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def ensure_backend_on_path() -> None:
    """把 `backend/` 挂到 sys.path，使 `import app.*` 在任何工作目录下都成立。"""
    if BACKEND_ROOT not in sys.path:
        sys.path.insert(0, BACKEND_ROOT)


# 导入即生效：脚本不必显式调用也能工作（显式调用仅为可读性）。
ensure_backend_on_path()
