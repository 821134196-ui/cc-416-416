"""测试公共夹具：每次重建匿名演示数据库（纯 unittest，无第三方依赖）。"""

import os
import sys
import tempfile

APP_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

import seed  # noqa: E402
from db import init_db  # noqa: E402


def fresh_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)
    os.environ["MEAL_DB"] = path
    seed.seed(reset=True)
    return init_db(path), path
