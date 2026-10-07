"""静态检查：抓「重构残留」这一类错误。

这些错误有个共同点 —— **语法合法、导入正常、单元测试也能过，只有真跑到那行才炸**。
本会话里已经被坑了三次，每次都靠人工发现：

  1. `browser_context` 里两个 `yield`，CDP 分支漏了 `return`
     → 运行时抛 `RuntimeError: generator didn't stop`
  2. `XiaohongshuSource` 里留着旧的 `crawl_target`（新写的在上面、旧的没删）
     → Python 取最后一个定义，跑起来调用已删除的 `_ensure_login` 直接 AttributeError
  3. `do_login` 签名里没有 `args`，函数体里却写了 `args.cdp`
     → 用户点下登录按钮的瞬间才 NameError

前两个 AST 能查，第三个也能。第三个尤其值得查 —— 它发生在用户交互的那一刻，
是"我准备好了但你告诉我有报错"的那种最扫兴的失败。

（第 1 个需要控制流分析，AST 查不了，只能在 browser.py 里留注释 + 靠实测。
  实测会暴露它：一旦走到那条分支就必崩。）
"""

from __future__ import annotations

import ast
import sys
import unittest
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

PACKAGE_ROOT = Path(__file__).resolve().parent.parent / "frisbee_radar"


def parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def py_files() -> list[Path]:
    return sorted(PACKAGE_ROOT.rglob("*.py"))


class TestNoDuplicateDefinitions(unittest.TestCase):
    """同一个作用域里不许重复定义同名函数/类。

    Python 静默取最后一个定义，前面的成了死代码 —— 看起来像改了，
    实际没生效，非常难查。
    """

    def test_no_duplicate_function_in_class(self):
        problems = []
        for path in py_files():
            tree = parse(path)
            for node in ast.walk(tree):
                if not isinstance(node, ast.ClassDef):
                    continue
                names = [
                    item.name
                    for item in node.body
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                ]
                for name, count in Counter(names).items():
                    if count > 1:
                        problems.append(f"{path.name}::{node.name}.{name} 定义了 {count} 次")
        self.assertFalse(
            problems,
            "有重复定义（Python 只取最后一个，前面的白改了）：\n  " + "\n  ".join(problems),
        )

    def test_no_duplicate_module_level_function(self):
        problems = []
        for path in py_files():
            tree = parse(path)
            names = [
                node.name
                for node in tree.body
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            ]
            for name, count in Counter(names).items():
                if count > 1:
                    problems.append(f"{path.name}::{name} 定义了 {count} 次")
        self.assertFalse(problems, "模块级函数重复定义：\n  " + "\n  ".join(problems))


class TestNoDanglingArgsReference(unittest.TestCase):
    """函数体里用 `args.xxx`，参数表里却没有 `args` —— 一调用就 NameError。

    这个 bug 出现在 do_login 里，触发时机是"用户点了登录"，
    代价是用户白等一次、还得再来一轮。
    """

    def test_every_args_reference_has_an_args_parameter(self):
        problems = []
        for path in py_files():
            tree = parse(path)
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                param_names = {a.arg for a in node.args.args}
                param_names |= {a.arg for a in node.args.kwonlyargs}
                param_names |= {a.arg for a in node.args.posonlyargs}
                if node.args.vararg:
                    param_names.add(node.args.vararg.arg)
                if node.args.kwarg:
                    param_names.add(node.args.kwarg.arg)

                if "args" in param_names:
                    continue

                # args 也可能是函数内的局部变量（比如 main 里的
                # args = parser.parse_args(argv)），那不算悬空引用
                local_names = {
                    target.id
                    for sub in ast.walk(node)
                    if isinstance(sub, ast.Assign)
                    for target in sub.targets
                    if isinstance(target, ast.Name)
                }
                if "args" in local_names:
                    continue

                used = [
                    sub.attr
                    for sub in ast.walk(node)
                    if isinstance(sub, ast.Attribute)
                    and isinstance(sub.value, ast.Name)
                    and sub.value.id == "args"
                ]
                if used:
                    problems.append(
                        f"{path.name}::{node.name}() 用了 args."
                        f"{sorted(set(used))}，但它既不是参数、也没在函数里赋值"
                    )
        self.assertFalse(problems, "悬空的 args 引用：\n  " + "\n  ".join(problems))


class TestSourceMethodsExist(unittest.TestCase):
    """每个 `self.xxx(` 调用的方法，类里都得真的有。"""

    def test_no_calls_to_undefined_methods(self):
        problems = []
        for path in py_files():
            tree = parse(path)
            for cls in (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)):
                defined = set()
                for base in ast.walk(cls):
                    if isinstance(base, ast.ClassDef):
                        defined |= {
                            item.name for item in base.body
                            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                        }
                    elif isinstance(base, ast.Assign):
                        defined |= {
                            t.id for t in base.targets if isinstance(t, ast.Name)
                        }

                called = {
                    sub.func.attr
                    for sub in ast.walk(cls)
                    if isinstance(sub, ast.Call)
                    and isinstance(sub.func, ast.Attribute)
                    and isinstance(sub.func.value, ast.Name)
                    and sub.func.value.id == "self"
                }
                # 属性可能是运行时赋值或父类提供的，只报「明显是方法调用」的
                for name in sorted(called - defined):
                    if name.startswith("_"):
                        problems.append(f"{path.name}::{cls.name} 调用了未定义的 self.{name}()")
        self.assertFalse(
            problems,
            "调用了不存在的方法（重构删了实现但没删调用点）：\n  " + "\n  ".join(problems),
        )


class TestCliSourceChoices(unittest.TestCase):
    def test_source_choices_all_build(self):
        """--source 的每个可选值都得能真的造出采集器。"""
        from frisbee_radar.config import load_config
        from frisbee_radar.sources import AVAILABLE, build_source

        cfg = load_config()
        for name in AVAILABLE:
            with self.subTest(source=name):
                build_source(name, cfg)


if __name__ == "__main__":
    unittest.main(verbosity=2)
