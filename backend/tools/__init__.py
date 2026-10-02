"""backend/tools 脚本包。

**为什么需要这个文件**
`tools/` 下的脚本有两种入口，对 sys.path 的要求不同：

1. **当脚本跑**：`python tools/import_data.py` —— `sys.path[0]` 是 `tools/`，
   脚本内部可以 `import _bootstrap`。
2. **当包被导入**：`from tools.augment_eval_questions import _validate`
   （测试与 `fetch_sales_scheduled.py` 都走这条）—— 此时 `tools/` 本身
   **不在** sys.path 上，只有 `backend/` 在。

入口 2 早先靠 Python 3 的隐式命名空间包工作；`scripts/*.py` 里新增
`from _bootstrap import ...` 之后它就会失败（`No module named '_bootstrap'`）。
本文件把 `tools/` 自己也挂上 sys.path，两个入口就统一了。

`backend/` 同样挂上，是为了让 `import app.*` 在任何工作目录下都成立——
脚本内部原本各自重复的 `sys.path.insert(...)` 已由
`tools/_bootstrap.py` 收敛，见其 docstring。
"""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)

# 顺序：先 tools/ 后 backend/，与各脚本原先的插入顺序一致（tools/ 在前）。
for _p in (_HERE, _BACKEND):
    if _p not in sys.path:
        sys.path.insert(0, _p)
