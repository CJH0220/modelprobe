# -*- coding: utf-8 -*-
"""python -m mprobe 的入口。

CLI 是唯一真源，这里只负责把退出码交回给 shell——
计划任务靠退出码判断有没有告警，吞掉它等于把告警吞掉。
"""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
