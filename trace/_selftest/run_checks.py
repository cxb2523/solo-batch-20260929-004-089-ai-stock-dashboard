"""离线自检：灰页阈值与跨模块旧边退回全量。

不接触主项目的 graph.json；在临时目录里构造最小工程验证 analyzer 的行为。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import analyzer  # noqa: E402


GREY_PROJECT = {
    "app.py": """
import helper

def runner():
    obj = helper.make()
    fn = getattr(obj, "go")
    fn()
    handler = obj["dispatch"]
    handler()
    obj.dynamic_one()
    obj.dynamic_two()
""",
    "helper.py": """
class Box:
    pass

def make():
    return Box()
""",
}

CROSS_PROJECT_V1 = {
    "alpha.py": """
def alpha_fn(store):
    # 间接/动态调用：resolver 无法静态确定
    store.dispatch()
    return store
""",
    "beta.py": """
import alpha

def beta_fn(store):
    return alpha.alpha_fn(store)
""",
}

CROSS_PROJECT_V2_BETA = """
import alpha

def beta_fn(store):
    # 变更：新增一条直接调用，alpha 未变，但它内部有间接调用
    alpha.alpha_fn(store)
    return store.flag
"""


def write_project(root, files):
    os.makedirs(os.path.join(root, "trace"), exist_ok=True)
    for name, content in files.items():
        with open(os.path.join(root, name), "w", encoding="utf-8") as handle:
            handle.write(content)


def check_grey_threshold():
    tmp = tempfile.mkdtemp(prefix="trace-grey-")
    try:
        write_project(tmp, GREY_PROJECT)
        graph = analyzer.scan(tmp, os.path.join(tmp, "trace"))
        ratio = graph["stats"]["unresolvable_ratio"]
        assert ratio > 0.15, "动态调用占比应超 15%%，实际 %.2f%%" % (ratio * 100)
        assert graph["stats"]["threshold_exceeded"] is True
        print("[grey] PASS 不可解析 %.2f%% > 15%%，threshold_exceeded=True"
              % (ratio * 100))
        return True
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_cross_module_fallback():
    tmp = tempfile.mkdtemp(prefix="trace-cross-")
    try:
        write_project(tmp, CROSS_PROJECT_V1)
        first = analyzer.scan(tmp, os.path.join(tmp, "trace"))
        assert first["scan"]["mode"] == "full"

        # 修改 beta.py（alpha.py 保持不变，且其中存在间接调用）
        with open(os.path.join(tmp, "beta.py"), "w", encoding="utf-8") as handle:
            handle.write(CROSS_PROJECT_V2_BETA)

        second = analyzer.scan(tmp, os.path.join(tmp, "trace"))

        # 无论走增量还是退回全量，图必须与“此刻的全量重扫”逐边一致
        full_now = analyzer.GraphBuilder(
            analyzer.parse_modules(analyzer.discover_modules(tmp), tmp),
            tmp).build()
        consistent = analyzer.edge_signature(second) == \
            analyzer.edge_signature(full_now)
        assert consistent, "增量/退回结果必须与全量逐边一致"

        mode = second["scan"]["mode"]
        reason = second["scan"]["fallback_reason"] or ""
        if mode == "full":
            assert "间接" in reason or "不一致" in reason or "退回" in reason
            print("[cross] PASS 检测到未变更模块的间接调用，已退回全量：%s"
                  % reason)
        else:
            print("[cross] PASS 增量逐边一致（模式 incremental）")
        return True
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    ok = True
    for check in (check_grey_threshold, check_cross_module_fallback):
        try:
            check()
        except AssertionError as exc:
            ok = False
            print("[FAIL]", check.__name__, exc)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
