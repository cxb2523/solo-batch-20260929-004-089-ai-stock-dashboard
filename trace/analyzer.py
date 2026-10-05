"""AST 调用图分析器。

职责
----
- 扫描项目根目录下的本地 Python 模块（排除 trace/tools/docs 等非源码目录）。
- 用 ``ast`` 为每个函数生成节点、调用边、session_state 键、缓存键与失效条件。
- 动态属性（``getattr`` / 对象来源无法确定）与间接调用（下标/表达式取可调用对象）
  一律标记为 ``unresolved``，计入不可解析比例。
- 增量重扫只解析变更模块；每个调用点以“描述子”落盘，合并后统一做全局重绑定，
  再与全量重扫逐边比对；跨模块间接调用可能残留旧边时直接退回全量。

输出 trace/graph.json；增量状态 trace/.state.json。
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os

UNRESOLVABLE_LIMIT = 0.15
COVERAGE_FLOOR = 0.80
EXCLUDE_DIRS = {".git", "__pycache__", "trace", "tools", "docs", "screenshots",
                "_selftest",
                "node_modules", ".venv", "venv"}


# ---------------------------------------------------------------------------
# AST 小工具
# ---------------------------------------------------------------------------


def dotted_name(node):
    """a.b.c -> 'a.b.c'；非纯属性链返回 None。"""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def attr_chain(node):
    """属性链 -> (根 Name 或 None, 从外到内的属性名列表)。"""
    attrs = []
    while isinstance(node, ast.Attribute):
        attrs.append(node.attr)
        node = node.value
    return (node.id if isinstance(node, ast.Name) else None), attrs


def attr_suffix(node):
    _, attrs = attr_chain(node)
    return "." + ".".join(reversed(attrs)) if attrs else ""


def const_str(node):
    try:
        value = ast.literal_eval(node)
        return value if isinstance(value, str) else None
    except Exception:
        return None


def state_key(node):
    """st.session_state['k'] / st.session_state.k -> 'k'。"""
    if isinstance(node, ast.Subscript) and dotted_name(node.value) == "st.session_state":
        key = const_str(node.slice)
        return key if key is not None else "<dynamic-key>"
    root, attrs = attr_chain(node)
    if root == "st" and attrs == ["session_state"]:
        return None
    if root == "st" and len(attrs) == 2 and attrs[0] == "session_state":
        return attrs[1]
    return None


# ---------------------------------------------------------------------------
# 词法作用域（标签页 / 复选框分区）
# ---------------------------------------------------------------------------


def collect_scopes(tree):
    """返回 {func_key: [(lineno, end_lineno, kind, label)]}。

    func_key 形如 'main' 或 'StockAnalyzer.fetch_stock_data'。
    kind: tab（st.tabs 的 with 块）/ checkbox（show_* 的 if）/
    action（sidebar.button）/ branch（选股分支）。
    """
    events = []
    tab_vars = {}

    class Visitor(ast.NodeVisitor):
        def __init__(self):
            self.current = None

        def _enter_func(self, key, node):
            if self.current is not None:
                return  # 嵌套函数：整棵子树跳过
            self.current = key
            for stmt in node.body:
                self.visit(stmt)
            self.current = None

        def visit_FunctionDef(self, node):
            if self.current is None:
                self._enter_func(node.name, node)

        def visit_AsyncFunctionDef(self, node):
            if self.current is None:
                self._enter_func(node.name, node)

        def visit_ClassDef(self, node):
            if self.current is not None:
                return
            for stmt in node.body:
                if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    self._enter_func(node.name + "." + stmt.name, stmt)

        def visit_Assign(self, node):
            if isinstance(node.value, ast.Call) and \
                    dotted_name(node.value.func) == "st.tabs":
                labels = self._tab_labels(node.value)
                names = []
                for target in node.targets:
                    if isinstance(target, (ast.Tuple, ast.List)):
                        names = [e.id for e in target.elts if isinstance(e, ast.Name)]
                for name, label in zip(names, labels):
                    if isinstance(label, str):
                        tab_vars[name] = label
            self.generic_visit(node)

        @staticmethod
        def _tab_labels(call):
            out = []
            if call.args and isinstance(call.args[0], (ast.List, ast.Tuple)):
                for elt in call.args[0].elts:
                    out.append(const_str(elt))
            return out

        def visit_With(self, node):
            label = None
            for item in node.items:
                ctx = item.context_expr
                if isinstance(ctx, ast.Call) and dotted_name(ctx.func) == "st.spinner":
                    label = const_str(ctx.args[0]) if ctx.args else None
                elif isinstance(ctx, ast.Name) and ctx.id in tab_vars:
                    label = tab_vars[ctx.id]
            if label and self.current:
                events.append((self.current, node.lineno, node.end_lineno,
                               "tab", label))
            self.generic_visit(node)

        def visit_If(self, node):
            label, kind = self._if_scope(node)
            if label and self.current:
                events.append((self.current, node.lineno, node.end_lineno,
                               kind, label))
            self.generic_visit(node)

        @staticmethod
        def _if_scope(node):
            test = node.test
            if isinstance(test, ast.Name) and test.id.startswith("show_"):
                return test.id, "checkbox"
            if isinstance(test, ast.Call) and \
                    dotted_name(test.func) == "st.sidebar.button":
                label = const_str(test.args[0]) if test.args else "action"
                return label, "action"
            if isinstance(test, ast.Compare) and isinstance(test.left, ast.Name) \
                    and test.left.id in ("stock_choice", "symbol"):
                return "branch:" + test.left.id, "branch"
            return None, None

    Visitor().visit(tree)
    grouped = {}
    for func, start, end, kind, label in events:
        grouped.setdefault(func, []).append((start, end, kind, label))
    return grouped


def scopes_at(events, lineno):
    return [
        {"kind": kind, "label": label}
        for start, end, kind, label in events
        if start <= lineno <= end
    ]


# ---------------------------------------------------------------------------
# 缓存装饰器
# ---------------------------------------------------------------------------


def cache_decorator(dec):
    target = dec.func if isinstance(dec, ast.Call) else dec
    dotted = dotted_name(target)
    if dotted not in ("st.cache_data", "st.cache_resource"):
        return None
    kind = dotted.split(".")[1]
    info = {"kind": kind, "ttl": None, "max_entries": None, "show_spinner": None}
    if isinstance(dec, ast.Call):
        for kw in dec.keywords:
            if kw.arg in ("ttl", "max_entries", "show_spinner"):
                try:
                    info[kw.arg] = ast.literal_eval(kw.value)
                except Exception:
                    info[kw.arg] = "<dynamic>"
    parts = ["任一入参值变化（Streamlit 按参数序列化后的值生成缓存键）"]
    if info.get("ttl") not in (None, "<dynamic>"):
        parts.append("ttl=%s 到期" % info["ttl"])
    parts.append("显式调用 st.%s.clear()" % kind)
    parts.append("进程退出或源码改动后重启")
    info["invalidation"] = "；".join(parts)
    return info


# ---------------------------------------------------------------------------
# 值来源（过程内、流不敏感）
# ---------------------------------------------------------------------------


def rvalue_origin(node, imports, local_funcs, local_classes, class_attrs,
                  self_param, methods):
    """把右值概括成来源 dict：{tag, label}。

    tag:
      local   - 本模块函数调用结果
      class   - 本模块类构造
      method  - self.method()（同类方法）
      ext     - 外部模块/类型（可解析的属性调用）
      param   - 形参（跨函数传播时使用）
      name    - 对其它局部名的引用（稍后做别名消解）
      unknown - 来源不明
      lambda  - lambda 表达式
    """
    if isinstance(node, ast.Call):
        fn = node.func
        if isinstance(fn, ast.Name):
            if fn.id in local_funcs:
                return {"tag": "local", "label": fn.id}
            if fn.id in local_classes:
                return {"tag": "class", "label": fn.id}
        if isinstance(fn, ast.Attribute):
            root, attrs = attr_chain(fn)
            if root == self_param and attrs:
                if attrs[-1] in methods:
                    return {"tag": "method", "label": attrs[-1]}
                if attrs[-1] in class_attrs:
                    return {"tag": "ext", "label": class_attrs[attrs[-1]]}
            if root in imports:
                return {"tag": "ext", "label": imports[root][1] + attr_suffix(fn)}
            if root is None:
                # df[...].fillna() 这类以调用/下标为基对象的属性调用，
                # 基对象已是外部类型时，链式方法仍属于该外部类型
                base = rvalue_origin(fn.value, imports, local_funcs,
                                     local_classes, class_attrs,
                                     self_param, methods)
                if base.get("tag") in ("ext", "name", "param"):
                    return {"tag": "ext",
                            "label": base.get("label", "<expr>") +
                                     attr_suffix(fn)}
                if base.get("tag") == "local":
                    return {"tag": "local", "label": base["label"]}
        return {"tag": "ext", "label": dotted_name(fn) or "<call>"}
    if isinstance(node, ast.Name):
        return {"tag": "name", "label": node.id}
    if isinstance(node, ast.Attribute):
        root, attrs = attr_chain(node)
        if root == self_param and attrs and attrs[-1] in class_attrs:
            return {"tag": "ext", "label": class_attrs[attrs[-1]]}
        if root in imports:
            return {"tag": "ext", "label": imports[root][1] + attr_suffix(node)}
        if root is not None:
            return {"tag": "name", "label": root}
        return {"tag": "unknown", "label": root or "<attr>"}
    if isinstance(node, ast.Subscript):
        inner = rvalue_origin(node.value, imports, local_funcs, local_classes,
                              class_attrs, self_param, methods)
        # 下标不改变外部类型：data['Close']/data.iloc[-1] 仍是 pandas 对象
        if inner.get("tag") in ("ext", "param"):
            return inner
        if inner.get("tag") == "name":
            return inner
        return {"tag": "unknown", "label": "<subscript>"}
    if isinstance(node, ast.Lambda):
        return {"tag": "lambda", "label": "<lambda>"}
    if isinstance(node, ast.Constant):
        return {"tag": "ext", "label": type(node.value).__name__}
    if isinstance(node, (ast.List, ast.Dict, ast.Tuple, ast.Set, ast.BinOp,
                         ast.UnaryOp, ast.BoolOp, ast.Compare, ast.IfExp,
                         ast.ListComp, ast.DictComp, ast.SetComp,
                         ast.GeneratorExp, ast.JoinedStr, ast.Starred)):
        return {"tag": "ext", "label": type(node).__name__}
    return {"tag": "unknown", "label": type(node).__name__}


# ---------------------------------------------------------------------------
# 单函数分析
# ---------------------------------------------------------------------------


class FuncAnalyzer:
    def __init__(self, qualname, func_node, class_name, imports, symbols,
                 class_attrs, methods, scope_events):
        self.qualname = qualname
        self.node = func_node
        self.class_name = class_name
        self.imports = imports
        self.local_funcs = symbols["functions"]
        self.local_classes = symbols["classes"]
        self.class_attrs = class_attrs
        self.methods = methods
        self.events = scope_events
        self.origins = {}
        self.returns = []
        self.calls = []
        self.session_keys = []

        args = func_node.args
        names = [a.arg for a in args.posonlyargs + args.args + args.kwonlyargs]
        if args.vararg:
            names.append("*" + args.vararg.arg)
        if args.kwarg:
            names.append("**" + args.kwarg.arg)
        self.self_param = "self" if class_name and names and names[0] == "self" else None
        for name in names:
            self.origins[name] = {"tag": "param", "label": name}

    def analyze(self):
        self._visit(self.node, root=True)
        self._resolve_name_aliases()
        return self

    def _visit(self, node, root=False):
        if not root:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                return
            if isinstance(node, ast.Call):
                self._record_call(node)
            if isinstance(node, (ast.Subscript, ast.Attribute)):
                key = state_key(node)
                if key is not None:
                    self._touch_key(key, node.lineno)

        # 先遍历子节点（记录 RHS 调用/状态访问，用的是赋值前的来源），
        # 再更新左值来源，保证 data = f(data) 不会污染右侧实参。
        for child in ast.iter_child_nodes(node):
            self._visit(child)

        if isinstance(node, ast.Assign):
            self._assign(node.targets, node.value)
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            self._assign([node.target], node.value)
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            self._assign([node.target], node.iter)
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                if item.optional_vars is not None:
                    self._assign([item.optional_vars], item.context_expr)
        if isinstance(node, ast.Return) and node.value is not None:
            if isinstance(node.value, ast.Name):
                origin = self._chain_origin(dict(
                    self.origins.get(node.value.id,
                                     {"tag": "unknown",
                                      "label": node.value.id})))
            elif isinstance(node.value, ast.Tuple):
                origin = {
                    "tag": "tuple",
                    "els": [self._chain_origin(self._expr_tuple_origin(el))
                            for el in node.value.elts
                            if not isinstance(el, ast.Starred)],
                }
            else:
                origin = self._origin(node.value)
                origin = self._chain_origin(origin)
            self.returns.append(origin)


    def _touch_key(self, key, lineno):
        access = "write" if self._is_write_target(lineno) else "read"
        if not any(k["key"] == key and k["lineno"] == lineno
                   and k["access"] == access for k in self.session_keys):
            self.session_keys.append({"key": key, "lineno": lineno,
                                      "access": access})

    def _is_write_target(self, lineno):
        for stmt in ast.walk(self.node):
            if isinstance(stmt, ast.Assign):
                for target in stmt.targets:
                    if getattr(target, "lineno", -1) == lineno and state_key(target):
                        return True
            if isinstance(stmt, ast.AugAssign) and \
                    getattr(stmt.target, "lineno", -1) == lineno and \
                    state_key(stmt.target):
                return True
        return False

    def _origin(self, node):
        return rvalue_origin(node, self.imports, self.local_funcs,
                             self.local_classes, self.class_attrs,
                             self.self_param, self.methods)

    def _assign(self, targets, value):
        flat = self._flatten(targets)
        unpacking = (
            isinstance(value, (ast.Tuple, ast.List))
            and any(isinstance(t, (ast.Tuple, ast.List)) for t in targets)
            and all(not isinstance(v, ast.Starred) for v in value.elts)
        )
        if unpacking:
            for target, val in zip(flat, value.elts):
                if isinstance(val, ast.Name):
                    origin = self._chain_origin(dict(
                        self.origins.get(val.id,
                                         {"tag": "unknown", "label": val.id})))
                else:
                    origin = self._origin(val)
                self._assign_one(target, origin)
        else:
            origin = self._origin(value)
            for target in flat:
                self._assign_one(target, origin)

    @staticmethod
    def _flatten(targets):
        out = []
        for target in targets:
            if isinstance(target, (ast.Tuple, ast.List)):
                out.extend(e for e in target.elts if isinstance(e, ast.Name))
            elif isinstance(target, ast.Name):
                out.append(target)
        return out

    def _assign_one(self, target, origin):
        if target.id != self.self_param and \
                not (origin.get("tag") == "name" and
                     origin.get("label") == target.id):
            self.origins[target.id] = dict(origin)

    def _resolve_name_aliases(self):
        """name 标签可能只是中间别名，沿 origins 链消解成最终来源。"""
        for _ in range(4):
            changed = False
            for name, origin in list(self.origins.items()):
                if origin["tag"] == "name" and origin["label"] in self.origins:
                    target = self.origins[origin["label"]]
                    if target["tag"] != "name":
                        self.origins[name] = dict(target)
                        changed = True
            if not changed:
                break

    def _chain_origin(self, origin):
        """沿局部别名链把 name 标签消解掉（用于 return 值）。"""
        seen = set()
        while origin.get("tag") == "name" and origin["label"] in self.origins \
                and origin["label"] not in seen:
            seen.add(origin["label"])
            origin = self.origins[origin["label"]]
        return dict(origin)

    def _expr_tuple_origin(self, node):
        """return 元组里单个元素的来源（含名字消解）。"""
        if isinstance(node, ast.Name):
            return dict(self.origins.get(node.id,
                                         {"tag": "unknown", "label": node.id}))
        return self._origin(node)

    # -- 调用点：生成与全局符号无关的递归“描述子”，合并阶段统一重绑定 --

    def _record_call(self, call):
        lineno = call.lineno
        scopes = scopes_at(self.events, lineno)
        descriptor = self._callee_descriptor(call.func)
        args = [self._arg_descriptor(a) for a in call.args
                if not isinstance(a, ast.Starred)]
        self.calls.append({
            "lineno": lineno,
            "scopes": scopes,
            "d": descriptor,
            "args": args,
            "preview": self._preview(call.func),
        })

    def _arg_descriptor(self, node):
        if isinstance(node, ast.Name):
            origin = self._chain_origin(dict(
                self.origins.get(node.id,
                                 {"tag": "unknown", "label": node.id})))
            return self._origin_descriptor(origin, node.id)
        if isinstance(node, ast.Subscript):
            return self._value_descriptor(node)
        return self._value_descriptor(node)

    @staticmethod
    def _preview(func):
        dotted = dotted_name(func)
        if dotted:
            return dotted + "(...)"
        if isinstance(func, ast.Subscript):
            return "<subscript>(...)"
        if isinstance(func, ast.Call) and isinstance(func.func, ast.Name) \
                and func.func.id == "getattr":
            return "getattr(...)()"
        return "<expr>(...)"

    def _callee_descriptor(self, node):
        """被调对象本身的描述（不含它的参数）。"""
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                and node.func.id == "getattr":
            return {"t": "indirect",
                    "reason": "动态属性：getattr() 取得的属性无法静态解析"}
        if isinstance(node, ast.Subscript):
            key = state_key(node)
            if key is not None:
                return {"t": "indirect",
                        "reason": "间接调用：st.session_state['%s'] 中存储的可调用对象" % key}
            return {"t": "indirect",
                    "reason": "间接调用：下标表达式取出的对象无法静态解析"}
        if isinstance(node, ast.Name):
            return {"t": "name", "n": node.id}
        if isinstance(node, ast.Attribute):
            root_name, attrs = attr_chain(node)
            attrs = list(reversed(attrs))
            if root_name is None:
                return {"t": "chain", "value": self._callee_value(node.value),
                        "attrs": attrs}
            if root_name == self.self_param:
                return {"t": "chain", "value": {"t": "self"}, "attrs": attrs}
            if root_name in self.imports:
                return {"t": "chain",
                        "value": {"t": "ext",
                                  "label": self.imports[root_name][1]},
                        "attrs": attrs}
            origin = self._chain_origin(dict(
                self.origins.get(root_name,
                                 {"tag": "unknown", "label": root_name})))
            return {"t": "chain",
                    "value": self._origin_descriptor(origin, root_name),
                    "attrs": attrs}
        return {"t": "indirect",
                "reason": "间接调用：被调用对象是无法静态求值的表达式"}

    def _callee_value(self, node):
        """属性链中段（非根 Name）的值：调用/下标都保留完整结构。"""
        if isinstance(node, ast.Call):
            return {"t": "callvalue",
                    "callee": self._callee_descriptor(node.func),
                    "args": [self._value_descriptor(a) for a in node.args
                             if not isinstance(a, ast.Starred)]}
        return self._value_descriptor(node)

    def _origin_descriptor(self, origin, root_name):
        tag = origin.get("tag")
        label = origin.get("label")
        if tag == "ext":
            return {"t": "ext", "label": label}
        if tag in ("local", "method", "class", "param", "unknown"):
            return {"t": "var", "n": root_name,
                    "origin": {"tag": tag, "label": label}}
        return {"t": "var", "n": root_name,
                "origin": {"tag": "unknown", "label": root_name}}

    def _value_descriptor(self, node):
        """任意表达式的值描述（用于实参传播 / 属性链中段）。"""
        if isinstance(node, ast.Name):
            origin = self._chain_origin(dict(
                self.origins.get(node.id,
                                 {"tag": "unknown", "label": node.id})))
            return self._origin_descriptor(origin, node.id)
        if isinstance(node, ast.Attribute):
            callee = self._callee_descriptor(node)
            if callee["t"] == "chain":
                return {"t": "attrvalue", "value": callee["value"],
                        "attrs": callee["attrs"]}
            return {"t": "var", "n": "<attr>",
                    "origin": {"tag": "unknown", "label": "<attr>"}}
        if isinstance(node, ast.Call):
            return {"t": "callvalue",
                    "callee": self._callee_descriptor(node.func),
                    "args": [self._value_descriptor(a) for a in node.args
                             if not isinstance(a, ast.Starred)]}
        if isinstance(node, ast.Subscript):
            key = state_key(node)
            if key is not None:
                return {"t": "statevalue", "key": key}
            return {"t": "subvalue", "value": self._value_descriptor(node.value)}
        if isinstance(node, (ast.Tuple, ast.List)):
            return {"t": "tuple",
                    "els": [self._value_descriptor(e) for e in node.elts
                            if not isinstance(e, ast.Starred)]}
        if isinstance(node, ast.Lambda):
            return {"t": "ext", "label": "<lambda>"}
        if isinstance(node, ast.Constant):
            return {"t": "ext", "label": type(node.value).__name__}
        return {"t": "ext", "label": type(node).__name__}


# ---------------------------------------------------------------------------
# 模块解析
# ---------------------------------------------------------------------------


class ModuleParser:
    def __init__(self, module_name, path, source):
        self.module = module_name
        self.path = path
        self.source = source
        self.tree = ast.parse(source, filename=path)
        self.imports = {}          # 局部名 -> (import 类型, 标签/模块名)
        self.classes = {}         # 类名 -> {方法集, 属性类型}
        self.funcs = {}           # qualname -> FuncAnalyzer 结果

    def parse(self):
        self._collect_imports()
        scope_groups = collect_scopes(self.tree)
        symbols = {
            "functions": set(self._top_level_func_names()),
            "classes": set(),
        }
        for node in self.tree.body:
            if isinstance(node, ast.ClassDef):
                symbols["classes"].add(node.name)

        for node in self.tree.body:
            if isinstance(node, ast.ClassDef):
                self._parse_class(node, scope_groups, symbols)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self._parse_func(node, None, scope_groups, symbols)
        return self

    def _collect_imports(self):
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    bound = alias.asname or alias.name.split(".")[0]
                    self.imports[bound] = ("module", alias.name)
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                for alias in node.names:
                    bound = alias.asname or alias.name
                    label = module + "." + alias.name if module else alias.name
                    self.imports[bound] = ("fromimport", label)

    def _top_level_func_names(self):
        names = []
        for node in self.tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                names.append(node.name)
            if isinstance(node, ast.ClassDef):
                names.extend(
                    s.name for s in node.body
                    if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef))
                )
        return names

    def _parse_class(self, node, scope_groups, symbols):
        method_names = {
            s.name for s in node.body
            if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        class_attrs = self._init_attr_types(node, symbols)
        self.classes[node.name] = {"methods": method_names,
                                   "attrs": class_attrs}
        for sub in node.body:
            if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self._parse_func(sub, node.name, scope_groups, symbols,
                                 class_attrs=class_attrs,
                                 methods=method_names)

    def _init_attr_types(self, class_node, symbols):
        """从 __init__ 的 self.x = External(...) 收集属性的外部类型标签。"""
        attrs = {}
        init = next((s for s in class_node.body
                     if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef))
                     and s.name == "__init__"), None)
        if init is None:
            return attrs
        for stmt in ast.walk(init):
            if isinstance(stmt, ast.Assign):
                for target in stmt.targets:
                    if isinstance(target, ast.Attribute):
                        root, attr_path = attr_chain(target)
                        if root == "self" and len(attr_path) == 1:
                            origin = rvalue_origin(
                                stmt.value, self.imports, symbols["functions"],
                                symbols["classes"], {}, "self", set())
                            if origin["tag"] == "ext":
                                attrs[attr_path[0]] = origin["label"]
        return attrs

    def _parse_func(self, node, class_name, scope_groups, symbols,
                    class_attrs=None, methods=None):
        qualname = (class_name + "." if class_name else "") + node.name
        events = scope_groups.get(qualname, [])
        analyzer = FuncAnalyzer(qualname, node, class_name, self.imports,
                                symbols, class_attrs or {}, methods or set(),
                                events)
        analyzer.analyze()

        cache = None
        for dec in node.decorator_list:
            cache = cache_decorator(dec) or cache

        self.funcs[qualname] = {
            "name": node.name,
            "qualname": qualname,
            "class": class_name,
            "lineno": node.lineno,
            "end_lineno": node.end_lineno,
            "params": self._param_names(node),
            "cache": cache,
            "session_keys": analyzer.session_keys,
            "calls": analyzer.calls,
            "returns": analyzer.returns,
        }

    @staticmethod
    def _param_names(node):
        args = node.args
        names = [a.arg for a in args.posonlyargs + args.args + args.kwonlyargs]
        if args.vararg:
            names.append("*" + args.vararg.arg)
        if args.kwarg:
            names.append("**" + args.kwarg.arg)
        return names

    def to_dict(self):
        return {
            "module": self.module,
            "path": self.path,
            "imports": {k: v[1] for k, v in self.imports.items()},
            "classes": {
                name: {"methods": sorted(info["methods"]),
                        "attrs": info["attrs"]}
                for name, info in self.classes.items()
            },
            "functions": self.funcs,
        }


# ---------------------------------------------------------------------------
# 全局重绑定：调用描述子 -> 边
# ---------------------------------------------------------------------------

try:
    import builtins
    BUILTINS = set(dir(builtins))
except Exception:  # pragma: no cover
    BUILTINS = set()


class GraphBuilder:
    def __init__(self, modules, root, changed_modules=None):
        self.modules = modules
        self.root = root
        self.changed_modules = set(changed_modules or [])
        self.func_index = {}
        self.bare_funcs = {}
        for mod, data in modules.items():
            for qual, fn in data["functions"].items():
                key = mod + ":" + qual
                self.func_index[key] = (mod, fn)
                self.bare_funcs.setdefault(fn["name"], []).append(key)
                if fn["class"]:
                    self.bare_funcs.setdefault(
                        fn["class"] + "." + fn["name"], []).append(key)
        self.param_origins = {
            key: {p.lstrip("*"): {"tag": "param", "label": p.lstrip("*")}
                  for p in fn["params"]}
            for key, (_, fn) in self.func_index.items()
        }
        self.edges = []

    # -- 定位 --------------------------------------------------------------

    def _local_key(self, module, name, class_name=None, method_of=None):
        if method_of:
            key = module + ":" + method_of + "." + name
            if key in self.func_index:
                return key
        if class_name:
            key = module + ":" + class_name + "." + name
            if key in self.func_index:
                return key
        key = module + ":" + name
        if key in self.func_index:
            return key
        keys = self.bare_funcs.get(name, [])
        if len(keys) == 1:
            return keys[0]
        keys = self.bare_funcs.get((method_of or class_name or "") + "." + name, [])
        return keys[0] if len(keys) == 1 else None

    # -- 形参传播 / return 固定点 ------------------------------------------

    @staticmethod
    def _merge_origin(old, new):
        if old is None:
            return new
        # 形参缺省来源可以被任意具体来源覆盖；
        # 但已有的具体来源（ext/local/...）不能被另一路的缺省形参覆盖。
        if old.get("tag") == "param" and new.get("tag") != "param":
            return new
        if new.get("tag") == "param" and old.get("tag") != "param":
            return old
        if old == new:
            return old
        if old.get("tag") == "tuple" and new.get("tag") == "tuple":
            old_els = old.get("els", [])
            new_els = new.get("els", [])
            size = max(len(old_els), len(new_els))
            padded_old = old_els + [{"tag": "unknown", "label": "?"}] * (size - len(old_els))
            padded_new = new_els + [{"tag": "unknown", "label": "?"}] * (size - len(new_els))
            return {"tag": "tuple",
                    "els": [GraphBuilder._merge_origin(a, b)
                            for a, b in zip(padded_old, padded_new)]}
        return {"tag": "unknown", "label": "<mixed>"}

    def _return_origin(self, key, seen=None):
        seen = seen or set()
        if key in seen:
            return {"tag": "unknown", "label": "<recursive>"}
        seen.add(key)
        _, fn = self.func_index.get(key, (None, None))
        if not fn:
            return {"tag": "unknown", "label": "<missing>"}
        returns = fn.get("returns") or []
        if not returns:
            return {"tag": "unknown", "label": "<no-return>"}
        merged = None
        for origin in returns:
            merged = origin if merged is None else self._merge_origin(merged, origin)
        return self._resolve_origin(key, merged, seen)

    def _resolve_origin(self, caller_key, origin, seen=None):
        seen = seen or set()
        tag = origin.get("tag")
        label = origin.get("label")
        if tag in ("ext", "unknown", "lambda"):
            return origin
        if tag == "param":
            param_origin = self.param_origins.get(caller_key, {}).get(label)
            if param_origin and param_origin.get("tag") != "param":
                return self._resolve_origin(caller_key, param_origin, seen)
            return {"tag": "unknown", "label": "<param:" + str(label) + ">"}
        if tag == "class":
            return {"tag": "class", "label": label}
        if tag in ("local", "method"):
            module = caller_key.split(":", 1)[0]
            callee = self._local_key(module, label)
            if callee:
                return self._return_origin(callee, seen)
        if tag == "tuple":
            return {"tag": "tuple",
                    "els": [self._resolve_origin(caller_key, el, seen)
                            for el in origin.get("els", [])]}
        return {"tag": "unknown", "label": str(label or tag)}

    def _propagate_params(self):
        changed = False
        for edge in self.edges:
            if edge["kind"] != "local" or not edge.get("args"):
                continue
            target = edge["target"]
            if target not in self.param_origins:
                continue
            _, fn = self.func_index[target]
            params = [p for p in fn["params"] if not p.startswith(("*", "**"))]
            merged = self.param_origins[target]
            offset = 1 if fn["class"] and params and params[0] == "self" else 0
            caller_args = edge["args"]
            for index, arg in enumerate(caller_args):
                param = params[index]
                if offset:
                    if index + offset >= len(params):
                        break
                    param = params[index + offset]
                # 实参是 value descriptor；若是对调用方形参的引用，
                # 先沿调用方已经传播好的 param_origins 消解成具体来源。
                arg_origin = self._resolve_arg_origin(edge["source"], arg)
                updated = self._merge_origin(merged.get(param), arg_origin)
                if updated != merged.get(param):
                    merged[param] = updated
                    changed = True
        return changed

    def _resolve_arg_origin(self, caller, desc):
        origin = self._value_to_origin(desc)
        seen = set()
        while origin.get("tag") == "param":
            label = origin["label"]
            if label in seen:
                break
            seen.add(label)
            caller_origin = self.param_origins.get(caller, {}).get(label)
            if not caller_origin or caller_origin.get("tag") == "param":
                break
            origin = caller_origin
        return origin

    @staticmethod
    def _value_to_origin(desc):
        t = desc.get("t")
        if t == "ext":
            return {"tag": "ext", "label": desc.get("label", "?")}
        if t == "self":
            return {"tag": "ext", "label": "self"}
        if t == "var":
            origin = desc["origin"]
            return {"tag": origin["tag"], "label": origin["label"]}
        if t == "tuple":
            return {"tag": "tuple",
                    "els": [GraphBuilder._value_to_origin(el)
                            for el in desc.get("els", [])]}
        if t in ("subvalue", "attrvalue", "callvalue", "statevalue"):
            return {"tag": "unknown", "label": "<" + t + ">"}
        return {"tag": "unknown", "label": str(t)}

    # -- 绑定 --------------------------------------------------------------

    def _bind_all(self):
        for mod, data in self.modules.items():
            for qual, fn in data["functions"].items():
                source = mod + ":" + qual
                for call in fn["calls"]:
                    edge = self._bind_callee(source, fn, call["d"])
                    edge.update({
                        "source": source,
                        "lineno": call["lineno"],
                        "scopes": call["scopes"],
                        "preview": call["preview"],
                        "args": call.get("args") or [],
                    })
                    self.edges.append(edge)

    def build(self):
        self._bind_all()
        for _ in range(8):
            propagated = self._propagate_params()
            refined = self._refine()
            if not propagated and refined:
                break
        return self._assemble()

    def _bind_callee(self, caller, fn, desc):
        kind = desc["t"]
        if kind == "indirect":
            return {"kind": "unresolved", "d": desc, "reason": desc["reason"]}
        if kind == "name":
            return self._bind_name(caller, fn, desc)
        if kind == "chain":
            value = self._resolve_value(caller, fn, desc["value"])
            return self._apply_attrs(caller, fn, value, desc["attrs"], desc)
        return {"kind": "unresolved", "d": desc, "reason": "未知调用形态"}

    def _bind_name(self, caller, fn, desc):
        module = caller.split(":", 1)[0]
        name = desc["n"]
        local = self._local_key(module, name, class_name=fn["class"])
        if local:
            return {"kind": "local", "d": desc, "target": local}
        if name in self.modules[module]["classes"]:
            init_key = self._local_key(module, "__init__",
                                       class_name=name)
            if init_key:
                return {"kind": "local", "d": desc, "target": init_key}
            return {"kind": "local", "d": desc,
                    "target": module + ":" + name + ".__init__"}
        imported = self.modules[module]["imports"].get(name)
        if imported:
            return {"kind": "external", "d": desc, "external": imported}
        if name in BUILTINS:
            return {"kind": "external", "d": desc, "external": "builtins." + name}
        return {"kind": "unresolved", "d": desc,
                "reason": "间接调用：名字 '%s' 来源无法静态确定" % name}

    # -- 值解析 -------------------------------------------------------------

    def _resolve_value(self, caller, fn, value_desc, depth=0):
        if depth > 12:
            return {"tag": "unknown", "label": "<deep>"}
        t = value_desc["t"]
        if t == "ext":
            return {"tag": "ext", "label": value_desc["label"]}
        if t == "self":
            return {"tag": "self", "label": "self"}
        if t == "var":
            return self._resolve_var(caller, fn, value_desc, depth)
        if t == "callvalue":
            return self._resolve_callvalue(caller, fn, value_desc, depth)
        if t == "attrvalue":
            base = self._resolve_value(caller, fn, value_desc["value"],
                                       depth + 1)
            if base["tag"] == "ext":
                return {"tag": "ext",
                        "label": base["label"] +
                                 "".join("." + a for a in value_desc["attrs"])}
            return base
        if t == "subvalue":
            return self._resolve_value(caller, fn, value_desc["value"],
                                       depth + 1)
        if t == "statevalue":
            return {"tag": "unknown",
                    "label": "session_state['%s']" % value_desc["key"]}
        if t == "tuple":
            return {"tag": "tuple",
                    "els": [self._resolve_value(caller, fn, el, depth + 1)
                            for el in value_desc["els"]]}
        return {"tag": "unknown", "label": t}

    def _resolve_var(self, caller, fn, value_desc, depth):
        origin = value_desc["origin"]
        tag, label = origin["tag"], origin["label"]
        if tag == "ext":
            return {"tag": "ext", "label": label}
        if tag == "param":
            resolved = self._param_origin(caller, label, depth)
            if resolved.get("tag") == "param":
                return resolved
            return resolved
        if tag in ("local", "method"):
            module = caller.split(":", 1)[0]
            callee = self._local_key(module, label)
            if callee:
                return self._return_origin(callee, depth=depth + 1)
            return {"tag": "unknown", "label": label}
        if tag == "class":
            return {"tag": "class", "label": label}
        return {"tag": "unknown", "label": label}

    def _resolve_callvalue(self, caller, fn, value_desc, depth):
        callee = value_desc["callee"]
        if callee["t"] == "name":
            module = caller.split(":", 1)[0]
            local = self._local_key(module, callee["n"],
                                    class_name=fn["class"])
            if local:
                return self._return_origin(local, depth=depth + 1)
            return {"tag": "ext", "label": callee["n"]}
        if callee["t"] == "chain":
            value = self._resolve_value(caller, fn, callee["value"],
                                        depth + 1)
            return self._origin_after_attrs(caller, fn, value,
                                            callee["attrs"], depth + 1)
        if callee["t"] == "indirect":
            return {"tag": "unknown", "label": "<indirect>"}
        return {"tag": "unknown", "label": "<callvalue>"}

    def _param_origin(self, caller, name, depth):
        if depth > 12:
            return {"tag": "param", "label": name}
        origin = self.param_origins.get(caller, {}).get(name)
        if not origin or origin.get("tag") == "param":
            return {"tag": "param", "label": name}
        _, fn = self.func_index.get(caller, (None, {"class": None}))
        return self._resolve_value(caller, fn,
                                   self._origin_to_value(origin), depth + 1)

    @staticmethod
    def _origin_to_value(origin):
        tag = origin.get("tag")
        if tag == "self":
            return {"t": "self"}
        if tag in ("param", "local", "method", "class", "unknown"):
            return {"t": "var", "n": origin.get("label", tag),
                    "origin": {"tag": tag, "label": origin.get("label", tag)}}
        if tag == "tuple":
            return {"t": "tuple",
                    "els": [GraphBuilder._origin_to_value(el)
                            for el in origin.get("els", [])]}
        if tag == "ext":
            return {"t": "ext", "label": origin.get("label", "?")}
        return {"t": "ext", "label": str(origin.get("label", tag))}

    def _return_origin(self, key, depth=0, seen=None):
        seen = set(seen or ())
        if key in seen or depth > 12:
            return {"tag": "unknown", "label": "<recursive>"}
        seen = seen | {key}
        _, fn = self.func_index.get(key, (None, None))
        if not fn:
            return {"tag": "unknown", "label": "<missing>"}
        returns = fn.get("returns") or []
        if not returns:
            return {"tag": "unknown", "label": "<no-return>"}
        merged = None
        for origin in returns:
            resolved = self._resolve_value(
                key, fn, self._origin_to_value(origin), depth + 1)
            merged = resolved if merged is None else \
                self._merge_origin(merged, resolved)
        return merged

    # -- 属性链推进 ---------------------------------------------------------

    def _apply_attrs(self, caller, fn, value, attrs, desc):
        origin = self._origin_after_attrs(caller, fn, value, attrs, 0,
                                          top_call=True)
        tag = origin.get("tag")
        if tag in ("local-target", "class-method"):
            return {"kind": "local", "d": desc, "target": origin["target"]}
        if tag == "ext":
            return {"kind": "external", "d": desc, "external": origin["label"]}
        if tag == "tuple":
            return {"kind": "unresolved", "d": desc,
                    "reason": "动态属性：对元组结果取属性 '%s'" %
                              (attrs[-1] if attrs else "?")}
        if tag == "param":
            return {"kind": "unresolved", "d": desc,
                    "reason": "动态属性：形参 %s 的类型无法静态确定" %
                              origin["label"]}
        if tag == "self":
            return {"kind": "unresolved", "d": desc,
                    "reason": "动态属性：self.%s 类型无法静态确定" %
                              (attrs[-1] if attrs else "?")}
        return {"kind": "unresolved", "d": desc,
                "reason": "动态属性：对象来源无法静态确定（%s）"
                          % (origin.get("label") or tag)}

    def _origin_after_attrs(self, caller, fn, current, attrs, depth,
                            top_call=False):
        module = caller.split(":", 1)[0]
        for index, attr in enumerate(attrs):
            last = index == len(attrs) - 1
            tag = current.get("tag")

            if tag == "self":
                cls = self.modules[module]["classes"].get(fn["class"], {})
                if attr in cls.get("methods", {}):
                    target = self._local_key(module, attr,
                                             class_name=fn["class"])
                    if last:
                        return {"tag": "local-target", "target": target}
                    current = self._return_origin(target, depth=depth + 1)
                    continue
                if attr in cls.get("attrs", {}):
                    current = {"tag": "ext",
                               "label": cls["attrs"][attr]}
                    continue
                return {"tag": "self", "label": attr}

            if tag == "class":
                target = self._local_key(module, attr,
                                         method_of=current["label"])
                if target:
                    if last:
                        return {"tag": "class-method", "target": target}
                    current = self._return_origin(target, depth=depth + 1)
                    continue
                return {"tag": "unknown",
                        "label": "%s.%s" % (current["label"], attr)}

            if tag in ("local", "method"):
                target = self._local_key(module, current["label"])
                if not target:
                    return {"tag": "unknown", "label": current["label"]}
                current = self._return_origin(target, depth=depth + 1)
                if current.get("tag") == "ext":
                    current = {"tag": "ext",
                               "label": current["label"] + "." + attr}
                elif current.get("tag") not in ("class",):
                    # 本地方法返回 unknown/param 时，属性访问无法继续
                    if current.get("tag") in ("unknown", "param"):
                        return current
                continue

            if tag == "ext":
                current = {"tag": "ext",
                           "label": current["label"] + "." + attr}
                continue
            if tag in ("tuple", "param", "unknown"):
                return current
            return {"tag": "unknown",
                    "label": "%s.%s" % (current.get("label", "?"), attr)}
        return current

    def _refine(self):
        stable = True
        fn_cache = {}
        for edge in self.edges:
            desc = edge.get("d", {})
            if edge["kind"] != "unresolved" or desc.get("t") != "chain":
                continue
            source = edge["source"]
            if source not in fn_cache:
                mod, qual = source.split(":", 1)
                fn_cache[source] = self.modules[mod]["functions"][qual]
            fn = fn_cache[source]
            value = self._resolve_value(source, fn, desc["value"])
            refined = self._apply_attrs(source, fn, value,
                                        desc["attrs"], desc)
            if (refined["kind"], refined.get("target"),
                    refined.get("external")) != \
                    (edge["kind"], edge.get("target"),
                     edge.get("external")):
                edge["kind"] = refined["kind"]
                edge["target"] = refined.get("target")
                edge["external"] = refined.get("external")
                # 重新分类后必须同步清掉旧理由：external 边不应残留“不可解析”原因
                edge["reason"] = refined.get("reason")
                stable = False
        return stable

    # -- 组装图 ------------------------------------------------------------

    # -- 组装图 ------------------------------------------------------------

    def _assemble(self):
        nodes = {}
        sections = set()

        for mod, data in self.modules.items():
            for qual, fn in data["functions"].items():
                key = mod + ":" + qual
                reuse = bool(fn["cache"]) or bool(fn["session_keys"])
                cache_info = fn["cache"]
                if cache_info:
                    params = [p for p in fn["params"]
                              if not p.startswith(("*", "**"))]
                    cache_key = "cache:%s:%s(%s)" % (
                        cache_info["kind"], qual,
                        ", ".join(params) if params else "")
                else:
                    cache_key = None
                nodes[key] = {
                    "id": key,
                    "module": mod,
                    "name": fn["name"],
                    "qualname": qual,
                    "class": fn["class"],
                    "file": data["path"],
                    "lineno": fn["lineno"],
                    "end_lineno": fn["end_lineno"],
                    "reuse_zone": reuse,
                    "cache": cache_info,
                    "cache_key": cache_key,
                    "session_keys": fn["session_keys"],
                    "params": fn["params"],
                }

        clean_edges = []
        for edge in self.edges:
            for scope in edge["scopes"]:
                sections.add(scope["label"])
            source_node = nodes.get(edge["source"])
            target_node = nodes.get(edge.get("target")) if edge.get("target") else None
            cross_boundary = bool(
                source_node and target_node and
                source_node["reuse_zone"] != target_node["reuse_zone"]
            )
            clean_edges.append({
                "source": edge["source"],
                "target": edge.get("target"),
                "kind": edge["kind"],
                "external": edge.get("external"),
                "reason": edge.get("reason"),
                "lineno": edge["lineno"],
                "preview": edge["preview"],
                "scopes": edge["scopes"],
                "cross_boundary": cross_boundary,
            })

        # 可达性：从无入边的入口函数（main / 顶层脚本流程）出发
        incoming = {key: 0 for key in nodes}
        adjacency = {key: [] for key in nodes}
        for edge in clean_edges:
            if edge["kind"] == "local" and edge["target"] in incoming:
                incoming[edge["target"]] += 1
                adjacency[edge["source"]].append(edge["target"])
        roots = [key for key, degree in incoming.items()
                 if degree == 0 or key.endswith(":main")]
        reached = set()
        stack = list(roots)
        while stack:
            current = stack.pop()
            if current in reached:
                continue
            reached.add(current)
            stack.extend(adjacency[current])

        total_calls = len(clean_edges)
        local_edges = [e for e in clean_edges if e["kind"] == "local"]
        external_edges = [e for e in clean_edges if e["kind"] == "external"]
        unresolved_edges = [e for e in clean_edges if e["kind"] == "unresolved"]
        total_functions = len(nodes)
        reached_functions = len(reached)
        unresolvable_ratio = (len(unresolved_edges) / total_calls) if total_calls else 0.0
        coverage = (reached_functions / total_functions) if total_functions else 0.0

        per_module = {}
        for mod in self.modules:
            mod_edges = [e for e in clean_edges
                         if e["source"].split(":", 1)[0] == mod]
            mod_unresolved = sum(1 for e in mod_edges if e["kind"] == "unresolved")
            per_module[mod] = {
                "calls": len(mod_edges),
                "local": sum(1 for e in mod_edges if e["kind"] == "local"),
                "external": sum(1 for e in mod_edges if e["kind"] == "external"),
                "unresolved": mod_unresolved,
                "unresolvable_ratio": (mod_unresolved / len(mod_edges))
                                      if mod_edges else 0.0,
            }

        stats = {
            "total_calls": total_calls,
            "local_calls": len(local_edges),
            "external_calls": len(external_edges),
            "unresolved_calls": len(unresolved_edges),
            "unresolvable_ratio": round(unresolvable_ratio, 4),
            "unresolvable_limit": UNRESOLVABLE_LIMIT,
            "total_functions": total_functions,
            "reached_functions": reached_functions,
            "coverage": round(coverage, 4),
            "coverage_floor": COVERAGE_FLOOR,
            "threshold_exceeded": unresolvable_ratio > UNRESOLVABLE_LIMIT,
            "per_module": per_module,
        }

        return {
            "schema_version": 1,
            "nodes": list(nodes.values()),
            "edges": clean_edges,
            "sections": sorted(sections),
            "stats": stats,
            "modules": sorted(self.modules),
        }


# ---------------------------------------------------------------------------
# 扫描引擎（全量 / 增量）
# ---------------------------------------------------------------------------


def discover_modules(root):
    modules = {}
    for current, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
        for filename in files:
            if not filename.endswith(".py"):
                continue
            abs_path = os.path.join(current, filename)
            rel = os.path.relpath(abs_path, root).replace(os.sep, "/")
            module = rel[:-3].replace("/", ".")
            modules[module] = abs_path
    return modules


def file_hash(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        digest.update(handle.read())
    return digest.hexdigest()


def parse_modules(module_paths, root):
    parsed = {}
    for module, path in module_paths.items():
        rel_path = os.path.relpath(path, root).replace(os.sep, "/")
        with open(path, "r", encoding="utf-8") as handle:
            source = handle.read()
        parsed[module] = ModuleParser(module, rel_path, source).parse().to_dict()
    return parsed


def edge_signature(graph):
    """逐边签名：源/目标/种类/行号/外部标签/原因全部参与比较。"""
    return sorted(
        (e["source"], e.get("target") or "", e["kind"], e["lineno"],
         e.get("external") or "", e.get("reason") or "")
        for e in graph["edges"]
    )


def scan(root, trace_dir, force_full=False):
    root = os.path.abspath(root)
    trace_dir = os.path.abspath(trace_dir)
    os.makedirs(trace_dir, exist_ok=True)
    graph_path = os.path.join(trace_dir, "graph.json")
    state_path = os.path.join(trace_dir, ".state.json")

    all_paths = discover_modules(root)
    current_hashes = {mod: file_hash(path) for mod, path in all_paths.items()}

    state = None
    if not force_full and os.path.exists(state_path):
        try:
            with open(state_path, "r", encoding="utf-8") as handle:
                state = json.load(handle)
        except Exception:
            state = None

    changed = [mod for mod, digest in current_hashes.items()
               if not state or state.get("hashes", {}).get(mod) != digest]
    added = [mod for mod in current_hashes if not state or mod not in state.get("hashes", {})]
    removed = []
    if state:
        removed = [mod for mod in state.get("hashes", {}) if mod not in current_hashes]

    mode = "full"
    fallback_reason = None
    if force_full:
        fallback_reason = "命令行指定 --full"
    elif not state:
        fallback_reason = "缺少增量状态，执行首次全量扫描"
    elif added or removed:
        fallback_reason = "模块新增(%s)或删除(%s)，退回全量" % (
            ",".join(sorted(added)) or "-", ",".join(sorted(removed)) or "-")

    full_parsed = parse_modules(all_paths, root)
    full_graph = GraphBuilder(full_parsed, root).build()

    if not fallback_reason and changed:
        # 增量路径：只重扫变更模块，其余沿用状态里的解析结果
        cached_modules = state.get("modules", {})
        merged = dict(cached_modules)
        changed_paths = {mod: all_paths[mod] for mod in changed}
        merged.update(parse_modules(changed_paths, root))
        # 仅保留仍存在的模块
        merged = {mod: data for mod, data in merged.items() if mod in all_paths}
        try:
            incremental_graph = GraphBuilder(
                merged, root, changed_modules=changed).build()
        except Exception as exc:  # 解析/绑定失败必须退回全量
            incremental_graph = None
            fallback_reason = "增量合并异常（%s），退回全量" % exc

        if incremental_graph is not None:
            # 守卫：未变更模块里仍有间接/动态调用时，目标可能已落在变更模块中，
            # 为避免残留旧跨模块边，直接退回全量。
            stale_risk = [
                e for e in incremental_graph["edges"]
                if e["kind"] == "unresolved"
                and e["source"].split(":", 1)[0] not in changed
            ]
            if stale_risk:
                fallback_reason = (
                    "未变更模块存在 %d 条间接/动态调用，可能跨模块命中变更模块，"
                    "为避免残留旧边退回全量" % len(stale_risk))

        if incremental_graph is not None and not fallback_reason:
            if edge_signature(incremental_graph) == edge_signature(full_graph):
                mode = "incremental"
                graph = incremental_graph
            else:
                fallback_reason = "增量结果与全量重扫逐边不一致，退回全量"

    if mode == "full":
        graph = full_graph

    graph["scan"] = {
        "mode": mode,
        "changed_modules": sorted(changed),
        "fallback_reason": fallback_reason,
        "root": root,
    }

    tmp_path = graph_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as handle:
        json.dump(graph, handle, ensure_ascii=False, indent=2)
    os.replace(tmp_path, graph_path)

    with open(state_path, "w", encoding="utf-8") as handle:
        json.dump({"hashes": current_hashes, "modules": full_parsed},
                  handle, ensure_ascii=False)

    return graph


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv=None):
    parser = argparse.ArgumentParser(description="AST 调用图扫描器")
    parser.add_argument("--root", default=os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))
    parser.add_argument("--trace-dir", default=None)
    parser.add_argument("--full", action="store_true", help="强制全量重扫")
    parser.add_argument("--json", action="store_true", help="只输出统计 JSON")
    args = parser.parse_args(argv)

    trace_dir = args.trace_dir or os.path.join(args.root, "trace")
    graph = scan(args.root, trace_dir, force_full=args.full)
    stats = graph["stats"]
    scan_info = graph["scan"]

    if args.json:
        print(json.dumps({"scan": scan_info, "stats": stats},
                         ensure_ascii=False, indent=2))
        return

    print("扫描模式: %s%s" % (
        scan_info["mode"],
        ("（退回全量：%s）" % scan_info["fallback_reason"])
        if scan_info["fallback_reason"] and scan_info["mode"] == "full" else ""))
    if scan_info["changed_modules"]:
        print("变更模块: %s" % ", ".join(scan_info["changed_modules"]))
    print("函数覆盖率: %d/%d = %.1f%%（门槛 %.0f%%）%s" % (
        stats["reached_functions"], stats["total_functions"],
        stats["coverage"] * 100, stats["coverage_floor"] * 100,
        " PASS" if stats["coverage"] >= stats["coverage_floor"] else " FAIL"))
    print("不可解析比例: %d/%d = %.2f%%（门槛 %.0f%%）%s" % (
        stats["unresolved_calls"], stats["total_calls"],
        stats["unresolvable_ratio"] * 100,
        stats["unresolvable_limit"] * 100,
        " PASS" if not stats["threshold_exceeded"] else " FAIL（页面转灰）"))
    print("调用边: 本地 %d / 外部 %d / 不可解析 %d" % (
        stats["local_calls"], stats["external_calls"],
        stats["unresolved_calls"]))
    print("输出: %s" % os.path.join(trace_dir, "graph.json"))


if __name__ == "__main__":
    main()
