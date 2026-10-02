"""Static AST call-graph builder for the stock dashboard.

Scans local Python source, extracts functions/methods, callsites,
``st.session_state`` keys, ``st.cache_data`` decorators and Streamlit tab
spans, then writes ``trace/graph.json``.

Modes: full rescans everything; incremental rescans only changed modules
and must be edge-for-edge identical to a full build, otherwise the build
falls back to a full scan.
"""

from __future__ import annotations

import argparse
import ast
import builtins
import hashlib
import json
import os
import tempfile
from dataclasses import dataclass, field

UNRESOLVABLE_THRESHOLD = 0.15
COVERAGE_GATE = 0.60
CACHE_DECORATORS = {"cache_data", "cache_resource"}
SKIP_DIRS = {
    ".git", ".hg", ".svn", "__pycache__", "node_modules",
    ".venv", "venv", "env", ".mypy_cache", ".pytest_cache", "trace",
}
BUILTIN_NAMES = set(dir(builtins))


def relpos(path, root):
    return os.path.relpath(path, root).replace(os.sep, "/")


def module_name_of(relpath):
    stem = relpath[:-3] if relpath.endswith(".py") else relpath
    parts = stem.split("/")
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def sha256_of(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def discover_python_files(root):
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for name in sorted(filenames):
            if name.endswith(".py"):
                found.append(relpos(os.path.join(dirpath, name), root))
    return sorted(found)


@dataclass
class Edge:
    src: str
    name: str
    kind: str
    file: str
    lineno: int
    index: int
    scopes: list
    dst: object = None
    resolved: bool = True
    external: bool = False
    target: object = None
    reason: object = None
    linked_modules: list = field(default_factory=list)
    tab_boundary: object = None

    def identity(self):
        if not self.resolved:
            dst = "UNRESOLVED"
        elif self.external:
            dst = "EXTERNAL:" + (self.target or "")
        else:
            dst = self.dst or "?"
        return f"{self.file}|{self.src}|{self.lineno}:{self.index}|{self.kind}|{self.name}|{dst}"

    def as_dict(self):
        return {
            "src": self.src, "dst": self.dst, "name": self.name, "kind": self.kind,
            "resolved": self.resolved, "external": self.external, "target": self.target,
            "reason": self.reason, "file": self.file, "lineno": self.lineno,
            "index": self.index, "scopes": self.scopes,
            "linked_modules": sorted(set(self.linked_modules)),
            "tab_boundary": self.tab_boundary, "key": self.identity(),
        }


@dataclass
class Node:
    id: str
    name: str
    qualname: str
    type: str
    module: str
    file: str
    lineno: int
    end_lineno: int
    scopes: list = field(default_factory=list)
    state_keys: list = field(default_factory=list)
    cache: object = None
    returns: object = None

    def as_dict(self):
        return {
            "id": self.id, "name": self.name, "qualname": self.qualname,
            "type": self.type, "module": self.module, "file": self.file,
            "lineno": self.lineno, "end_lineno": self.end_lineno,
            "scopes": sorted(set(self.scopes)),
            "state_keys": self.state_keys, "cache": self.cache, "returns": self.returns,
        }


class ModuleAnalyzer:
    """Extracts nodes and raw callsite records from one module."""

    def __init__(self, module, relpath, source):
        self.module = module
        self.relpath = relpath
        self.source = source
        self.tree = ast.parse(source, filename=relpath)
        self.nodes = []
        self.raw_edges = []
        self.defs = {}
        self.classes = {}
        self.imports = []
        self.tabs = []
        self.invalidations = []

    def analyze(self):
        self._annotate_parents()
        first_lineno = self._first_executable_line()
        self.nodes.append(Node(
            id=f"{self.module}::<module>", name="<module>", qualname="<module>",
            type="module", module=self.module, file=self.relpath,
            lineno=first_lineno, end_lineno=first_lineno,
        ))
        for stmt in self.tree.body:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self._collect_def(stmt, prefix="")
            elif isinstance(stmt, ast.ClassDef):
                self._collect_class(stmt)
        self._collect_imports()
        self._collect_tabs()
        for node in self.nodes:
            fn_ast = self._find_def(node)
            if fn_ast is not None:
                node.state_keys = self._collect_state_keys(fn_ast)
        self._collect_invalidations()

    def fact_dict(self):
        return {
            "module": self.module, "file": self.relpath,
            "nodes": [n.as_dict() for n in self.nodes],
            "defs": self.defs, "classes": self.classes,
            "imports": self.imports, "tabs": self.tabs,
            "invalidations": self.invalidations,
            "raw_edges": self.raw_edges,
            "params": self.params, "self_attrs": self.self_attrs,
            "origins": self.origins,
        }

    def _annotate_parents(self):
        for parent in ast.walk(self.tree):
            for child in ast.iter_child_nodes(parent):
                child.parent_node = parent
                child.parent_ctx = parent

    def _first_executable_line(self):
        for stmt in self.tree.body:
            if isinstance(stmt, (ast.Import, ast.ImportFrom)):
                continue
            if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant):
                continue
            return stmt.lineno
        return 1

    def _collect_class(self, cls):
        node_id = f"{self.module}::{cls.name}"
        self.nodes.append(Node(
            id=node_id, name=cls.name, qualname=cls.name, type="class",
            module=self.module, file=self.relpath,
            lineno=cls.lineno, end_lineno=cls.end_lineno or cls.lineno,
        ))
        self.defs[cls.name] = node_id
        method_ids = {}
        for stmt in cls.body:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                method_ids[stmt.name] = self._collect_def(stmt, prefix=cls.name + ".")
        base_names = []
        for base in cls.bases:
            if isinstance(base, ast.Name):
                base_names.append(base.id)
            elif isinstance(base, ast.Attribute):
                base_names.append(base.attr)
        self.classes[node_id] = {"methods": method_ids, "bases": base_names}

    def _collect_def(self, fn, prefix):
        qualname = f"{prefix}{fn.name}"
        node_type = "method" if prefix else "function"
        node = Node(
            id=f"{self.module}::{qualname}", name=fn.name, qualname=qualname,
            type=node_type, module=self.module, file=self.relpath,
            lineno=fn.lineno, end_lineno=fn.end_lineno or fn.lineno,
        )
        cache = self._cache_decorator(fn)
        if cache:
            node.cache = cache
        node.returns = self._origin_from_annotation(fn.returns)
        self.nodes.append(node)
        self.defs[fn.name] = node.id
        for stmt in fn.body:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                nested_id = self._collect_def(stmt, prefix=qualname + ".")
                self.defs[stmt.name] = nested_id
            elif isinstance(stmt, ast.ClassDef):
                self._collect_class(stmt)
        return node.id

    def _cache_decorator(self, fn):
        arg_names = [a.arg for a in fn.args.args]
        for dec in fn.decorator_list:
            call = dec if isinstance(dec, ast.Call) else None
            target = call.func if call else dec
            decorator_name = None
            if isinstance(target, ast.Attribute) and target.attr in CACHE_DECORATORS:
                decorator_name = target.attr
            elif isinstance(target, ast.Name) and target.id in CACHE_DECORATORS:
                decorator_name = target.id
            if decorator_name is None:
                continue
            info = {
                "type": decorator_name,
                "key_params": arg_names,
                "key_template": fn.name + ":" + ":".join("{" + a + "}" for a in arg_names),
                "ticker_param": next((a for a in arg_names if a in ("symbol", "ticker", "sym")), None),
                "extra_params": [a for a in arg_names if a not in ("symbol", "ticker", "sym")],
                "ttl": None,
                "max_entries": None,
            }
            if call:
                for kw in call.keywords:
                    if kw.arg in ("ttl", "max_entries") and isinstance(kw.value, ast.Constant):
                        info[kw.arg] = kw.value.value
            return info
        return None

    def _find_def(self, node):
        if node.type == "module":
            return self.tree
        parts = node.qualname.split(".")

        def walk(body):
            for stmt in body:
                if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and stmt.name == parts[0]:
                    current = stmt
                    for part in parts[1:]:
                        nxt = None
                        for sub in current.body:
                            if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and sub.name == part:
                                nxt = sub
                                break
                        if nxt is None:
                            return None
                        current = nxt
                    return current
            return None

        return walk(self.tree.body)

    def _collect_imports(self):
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    local = alias.asname or alias.name.split(".")[0]
                    self.imports.append({"local": local, "module": alias.name, "name": None})
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                for alias in node.names:
                    local = alias.asname or alias.name
                    self.imports.append({"local": local, "module": module, "name": alias.name})

    def _collect_tabs(self):
        tab_targets = []
        for stmt in ast.walk(self.tree):
            if not isinstance(stmt, ast.Assign):
                continue
            call = stmt.value if isinstance(stmt.value, ast.Call) else None
            if call is None or not isinstance(call.func, ast.Attribute) or call.func.attr != "tabs":
                continue
            if call.args and isinstance(call.args[0], (ast.List, ast.Tuple)):
                names = [
                    el.value for el in call.args[0].elts
                    if isinstance(el, ast.Constant) and isinstance(el.value, str)
                ]
                for target in stmt.targets:
                    if isinstance(target, (ast.Tuple, ast.List)):
                        for elt, tab_name in zip(target.elts, names):
                            if isinstance(elt, ast.Name):
                                tab_targets.append((elt.id, tab_name, stmt.lineno))
        tab_targets.sort(key=lambda item: item[2])

        withs = []
        for stmt in ast.walk(self.tree):
            if isinstance(stmt, ast.With):
                for item in stmt.items:
                    if isinstance(item.context_expr, ast.Name):
                        withs.append((item.context_expr.id, stmt.lineno, stmt.end_lineno or stmt.lineno))
        withs.sort(key=lambda item: item[1])

        tab_vars = {t[0] for t in tab_targets}
        name_map = {t[0]: t[1] for t in tab_targets}
        ordered = [w for w in withs if w[0] in tab_vars]
        for idx, (var, start, end) in enumerate(ordered):
            span_end = ordered[idx + 1][1] - 1 if idx + 1 < len(ordered) else end
            self.tabs.append({
                "name": name_map[var], "variable": var, "file": self.relpath,
                "lineno": start, "span_start": start + 1, "span_end": span_end,
            })

    @staticmethod
    def _is_state_expr(expr):
        if isinstance(expr, ast.Attribute):
            return isinstance(expr.value, ast.Name) and expr.value.id == "st" and expr.attr == "session_state"
        if isinstance(expr, ast.Name):
            return expr.id == "session_state"
        if isinstance(expr, (ast.Subscript, ast.Attribute)):
            return ModuleAnalyzer._is_state_expr(expr.value)
        return False

    def _collect_state_keys(self, fn):
        keys = []

        def add(key, access, lineno):
            for existing in keys:
                if existing["key"] == key and existing["access"] == access:
                    existing.setdefault("lines", []).append(lineno)
                    return
            keys.append({"key": key, "access": access, "lines": [lineno]})

        for sub in ast.walk(fn):
            if isinstance(sub, ast.Subscript) and self._is_state_expr(sub.value):
                written = isinstance(sub.ctx, ast.Store)
                access = "write" if written else "read"
                if isinstance(sub.parent_ctx, (ast.AugAssign, ast.AnnAssign)):
                    access = "readwrite"
                slc = sub.slice
                key = slc.value if isinstance(slc, ast.Constant) and isinstance(slc.value, str) else "<dynamic>"
                add(key, access, sub.lineno)
            elif isinstance(sub, ast.Attribute) and self._is_state_expr(sub.value) and sub.attr != "session_state":
                parent = getattr(sub, "parent_node", None)
                if isinstance(parent, ast.Call) and parent.func is sub:
                    continue  # session_state.<method>(...) — a dict method, not a key
                access = "write" if isinstance(sub.ctx, ast.Store) else "read"
                add(sub.attr, access, sub.lineno)
        for entry in keys:
            entry["lines"] = sorted(set(entry["lines"]))
        return keys

    def _collect_invalidations(self):
        for sub in ast.walk(self.tree):
            if not isinstance(sub, ast.Call) or not isinstance(sub.func, ast.Attribute):
                continue
            if sub.func.attr not in ("clear",):
                continue
            owner = sub.func.value
            if isinstance(owner, ast.Attribute) and isinstance(owner.value, ast.Name) and owner.value.id == "st" and owner.attr in CACHE_DECORATORS:
                self.invalidations.append({
                    "type": "global-cache-clear", "target": f"st.{owner.attr}",
                    "file": self.relpath, "lineno": sub.lineno,
                    "condition": "manual refresh (button) / explicit st." + owner.attr + ".clear()",
                })
            elif isinstance(owner, ast.Name) and owner.id.endswith("_cache"):
                self.invalidations.append({
                    "type": "per-cache-clear", "target": owner.id,
                    "file": self.relpath, "lineno": sub.lineno,
                    "condition": "manual refresh: " + owner.id + ".clear()",
                })
            elif isinstance(owner, ast.Name) and owner.id in self.defs:
                node_id = self.defs[owner.id]
                node = next((n for n in self.nodes if n.id == node_id), None)
                if node is not None and getattr(node, "cache", None):
                    self.invalidations.append({
                        "type": "cached-fn-clear", "target": owner.id,
                        "file": self.relpath, "lineno": sub.lineno,
                        "condition": "manual refresh button: " + owner.id + ".clear() drops its ticker/period entries",
                    })

    # -- callsites --------------------------------------------------------

    def collect_facts(self):
        self._collect_edges()
        self._build_origins()

    def _collect_edges(self):
        sequence = {}
        module_id = f"{self.module}::<module>"
        for call in ast.walk(self.tree):
            if not isinstance(call, ast.Call):
                continue
            parent = getattr(call, "parent_node", None)
            if isinstance(parent, ast.FunctionDef) and any(call is d for d in parent.decorator_list):
                continue
            enclosing = module_id
            cursor = parent
            while cursor is not None:
                if isinstance(cursor, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    enclosing = f"{self.module}::{self._qualname_of(cursor)}"
                    break
                cursor = getattr(cursor, "parent_node", None)
            order = sequence.get(enclosing, 0)
            sequence[enclosing] = order + 1
            callee = call.func
            if isinstance(callee, ast.Name):
                kind, name = "name", callee.id
            elif isinstance(callee, ast.Attribute):
                kind, name = "attr", callee.attr
            elif isinstance(callee, ast.Subscript):
                kind, name = "subscript", self._unparse(callee.value)
            elif isinstance(callee, ast.Lambda):
                kind, name = "lambda", "<lambda>"
            else:
                kind, name = "indirect", self._unparse(callee)
            self.raw_edges.append({
                "src": enclosing, "file": self.relpath, "lineno": call.lineno,
                "index": order, "kind": kind, "name": name,
                "callee": self._describe(callee),
            })

    def _qualname_of(self, fn):
        parts = []
        cursor = fn
        while isinstance(cursor, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            parts.append(cursor.name)
            cursor = getattr(cursor, "parent_node", None)
        return ".".join(reversed(parts))

    @staticmethod
    def _unparse(node):
        try:
            return ast.unparse(node)
        except Exception:
            return "<expr>"

    def _describe(self, node):
        if isinstance(node, ast.Name):
            return {"t": "name", "name": node.id}
        if isinstance(node, ast.Attribute):
            return {"t": "attr", "attr": node.attr, "value": self._describe(node.value)}
        if isinstance(node, ast.Call):
            return {"t": "call", "func": self._describe(node.func)}
        if isinstance(node, ast.Subscript):
            return {"t": "subscript", "value": self._describe(node.value), "slice": self._describe(node.slice)}
        if isinstance(node, ast.Lambda):
            return {"t": "lambda"}
        if isinstance(node, ast.Constant):
            return {"t": "const", "value": repr(node.value)[:40]}
        return {"t": "other", "expr": self._unparse(node)[:80]}

    # -- value-origin analysis --------------------------------------------

    def _build_origins(self):
        """Map local names / self attributes inside each function to origins.

        Origins are conservative descriptors: parameter types from annotations,
        self-attribute assignment (constructor tracking), call-results of known
        external APIs and imported modules.
        """
        self.params = {}       # enclosing -> {name: origin dict}
        self.self_attrs = {}   # class qualname -> {attr: origin}
        self.origins = {}      # enclosing -> {expr_key: origin}

        for node in self.nodes:
            if node.type == "method":
                cls_qual = node.qualname.rsplit(".", 1)[0]
                self.self_attrs.setdefault(cls_qual, {})

        for node in self.nodes:
            fn = self._find_def(node)
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            param_origins = {}
            for arg in fn.args.args + fn.args.kwonlyargs:
                origin = self._origin_from_annotation(arg.annotation)
                if arg.arg in ("self", "cls"):
                    origin = {"kind": "self-ref"}
                if origin:
                    param_origins[arg.arg] = origin
            if fn.args.vararg and fn.args.vararg.annotation:
                origin = self._origin_from_annotation(fn.args.vararg.annotation)
                if origin:
                    param_origins[fn.args.vararg.arg] = origin
            self.params[node.id] = param_origins

            scope = {}
            self.origins[node.id] = scope
            for stmt in ast.walk(fn):
                if not isinstance(stmt, ast.Assign):
                    continue
                rhs_origin = self._origin_of_expr(stmt.value, node.id, scope)
                if rhs_origin is None:
                    continue
                for target in stmt.targets:
                    self._record_target_origin(target, rhs_origin, node.id, scope)
            # self.x = X() inside __init__ (and any method)
            for stmt in ast.walk(fn):
                if not isinstance(stmt, ast.Assign):
                    continue
                rhs_origin = self._origin_of_expr(stmt.value, node.id, scope)
                if rhs_origin is None:
                    continue
                for target in stmt.targets:
                    if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name) and target.value.id == "self":
                        cls_qual = node.qualname.rsplit(".", 1)[0] if node.type == "method" else None
                        if cls_qual is not None:
                            self.self_attrs.setdefault(cls_qual, {})[target.attr] = rhs_origin

    def _origin_from_annotation(self, ann):
        if ann is None:
            return None
        if isinstance(ann, ast.Name):
            mod = self._local_module_for_name(ann.id)
            if mod is None:
                if ann.id in ("dict", "list", "tuple", "set", "str", "int", "float", "bool"):
                    return {"kind": "external-type", "module": "builtins", "name": ann.id}
                return {"kind": "local-class", "name": ann.id}
            return {"kind": "external-type", "module": mod, "name": ann.id}
        if isinstance(ann, ast.Attribute):
            root = ann
            while isinstance(root, ast.Attribute):
                root = root.value
            if isinstance(root, ast.Name):
                mod = self._local_module_for_name(root.id)
                return {"kind": "external-type", "module": mod or root.id, "name": ann.attr}
        return None

    def _local_module_for_name(self, local_name):
        for imp in self.imports:
            if imp["local"] == local_name:
                return imp["module"]
        return None

    def _find_import(self, local_name):
        for imp in self.imports:
            if imp["local"] == local_name:
                return imp["module"], imp["name"]
        return None

    def _record_target_origin(self, target, origin, enclosing, scope):
        if isinstance(target, ast.Name):
            scope["name:" + target.id] = origin
        elif isinstance(target, (ast.Tuple, ast.List)):
            for elt in target.elts:
                self._record_target_origin(elt, origin, enclosing, scope)

    def _origin_of_expr(self, expr, enclosing, scope):
        if isinstance(expr, ast.Name):
            if expr.id in ("self", "cls"):
                return {"kind": "self-ref"}
            if expr.id in self.params.get(enclosing, {}):
                return self.params[enclosing][expr.id]
            scoped = scope.get("name:" + expr.id)
            if scoped:
                return scoped
            imported = self._find_import(expr.id)
            if imported:
                mod, imported_name = imported
                if imported_name is None:
                    return {"kind": "module", "module": mod}
                return {"kind": "imported-name", "module": mod, "name": imported_name}
            if expr.id in self.defs:
                return {"kind": "local-fn", "id": self.defs[expr.id]}
            return {"kind": "unknown-name", "name": expr.id}
        if isinstance(expr, ast.Call):
            return self._origin_of_call(expr, enclosing, scope)
        if isinstance(expr, ast.Attribute):
            return self._origin_of_attr(expr, enclosing, scope)
        if isinstance(expr, ast.Subscript):
            base = self._origin_of_expr(expr.value, enclosing, scope)
            return self._subscript_origin(base, expr)
        if isinstance(expr, (ast.List, ast.ListComp)):
            return {"kind": "external-type", "module": "builtins", "name": "list"}
        if isinstance(expr, (ast.Tuple,)):
            return {"kind": "external-type", "module": "builtins", "name": "tuple"}
        if isinstance(expr, (ast.Dict, ast.DictComp, ast.SetComp, ast.Set)):
            return {"kind": "external-type", "module": "builtins", "name": "dict"}
        return None

    def _subscript_origin(self, base, expr):
        if base is None:
            return None
        name = base.get("name", "")
        kind = base.get("kind")
        if kind in ("external-type", "external-instance"):
            if name == "DataFrame":
                slc = expr.slice
                if isinstance(slc, (ast.List, ast.Tuple)):
                    return {"kind": "external-type", "module": "pandas", "name": "DataFrame"}
                return {"kind": "external-type", "module": "pandas", "name": "Series"}
            return dict(base)
        return dict(base)

    def _origin_of_attr(self, expr, enclosing, scope):
        root = expr
        chain = []
        while isinstance(root, ast.Attribute):
            chain.append(root.attr)
            root = root.value
        if not isinstance(root, ast.Name):
            return None
        chain.reverse()
        if root.id in ("self", "cls"):
            if not chain:
                return {"kind": "self-ref"}
            cls_qual = enclosing.split("::", 1)[1].rsplit(".", 1)[0] if "::" in enclosing else None
            first = self.self_attrs.get(cls_qual, {}).get(chain[0])
            if first is None:
                return {"kind": "self-attr", "attr": chain[0]}
            origin = dict(first)
            origin["attr_chain"] = chain[1:]
            return origin
        base = self._origin_of_expr(root, enclosing, scope)
        if base is None:
            return None
        origin = dict(base)
        origin["attr_chain"] = origin.get("attr_chain", []) + chain
        return origin

    def _origin_of_call(self, call, enclosing, scope):
        func = call.func
        if isinstance(func, ast.Attribute):
            root = func
            chain = []
            while isinstance(root, ast.Attribute):
                chain.append(root.attr)
                root = root.value
            chain.reverse()
            receiver_origin = self._origin_of_expr(root, enclosing, scope) if isinstance(root, ast.Name) else None
            if receiver_origin and receiver_origin.get("kind") == "module" and chain:
                constructor_origin = self._constructor_origin(receiver_origin["module"], chain[-1])
                if constructor_origin:
                    return constructor_origin
            mapped = self._analyzer_chain_return(receiver_origin, func.attr)
            if mapped is not None:
                return mapped
            return {
                "kind": "call-result",
                "method_chain": chain,
                "receiver": receiver_origin,
            }
        if isinstance(func, ast.Name):
            if func.id in self.defs:
                target = self._node_ast(self.defs[func.id])
                ret_origin = self._origin_from_annotation(getattr(target, "returns", None)) if target else None
                if ret_origin:
                    return ret_origin
                if any(cid.split("::", 1)[1] == func.id for cid in self.classes):
                    return {"kind": "local-class", "name": func.id}
                return {"kind": "call-result", "local_fn": self.defs[func.id]}
            imported = self._find_import(func.id)
            if imported:
                mod, imported_name = imported
                origin = self._constructor_origin(mod, imported_name)
                if origin:
                    return origin
                return {"kind": "constructor", "constructor": imported_name, "module": mod}
            return {"kind": "call-result", "name": func.id}

    @staticmethod
    def _constructor_origin(module, name):
        plotly_traces = {"Candlestick", "Scatter", "Bar", "Histogram", "Box"}
        if module in ("plotly.graph_objects", "plotly.subplots") and name == "Figure":
            return {"kind": "external-type", "module": "plotly.graph_objects", "name": "Figure"}
        if module == "plotly.graph_objects" and name in plotly_traces:
            return {"kind": "external-type", "module": "plotly", "name": "trace"}
        if module == "plotly.express":
            return {"kind": "external-type", "module": "plotly", "name": "Figure"}
        if module == "plotly.subplots" and name == "make_subplots":
            return {"kind": "external-type", "module": "plotly.graph_objects", "name": "Figure"}
        if module and module.startswith("sklearn"):
            return {"kind": "external-type", "module": module, "name": name}
        if module == "yfinance" and name == "Ticker":
            return {"kind": "external-type", "module": "yfinance", "name": "Ticker"}
        return None

    CHAIN_RETURN = None

    def _analyzer_chain_return(self, receiver, method):
        if not receiver or receiver.get("kind") not in ("external-type", "external-instance"):
            return None
        table = {
            "copy": "DataFrame", "dropna": "DataFrame", "fillna": "DataFrame",
            "sort_values": "DataFrame", "round": "DataFrame", "head": "DataFrame",
            "tail": "DataFrame", "where": "DataFrame", "transform": "DataFrame",
            "shift": "DataFrame", "diff": "DataFrame", "pct_change": "DataFrame",
            "rolling": "Rolling", "ewm": "EWM", "expanding": "Expanding",
            "mean": "Series", "std": "Series", "max": "Series", "min": "Series",
            "any": "Series", "all": "Series", "sum": "Series", "cumprod": "Series",
            "isna": "Series", "iloc": "Series", "loc": "Series",
            "history": "DataFrame",
            "add_trace": "Figure", "add_hline": "Figure",
            "update_layout": "Figure", "update_xaxes": "Figure",
            "fit": "__same__", "fit_transform": "__same__",
            "transform": "__same__", "predict": "ndarray", "score": "float",
        }
        tag = table.get(method)
        if tag is None:
            return None
        if tag == "__same__":
            return dict(receiver)
        module_map = {"Figure": "plotly", "ndarray": "numpy"}
        if receiver.get("name") in ("Rolling", "EWM", "Expanding"):
            module_map = {**module_map, "Series": "pandas", "DataFrame": "pandas"}
        module = module_map.get(tag, "pandas")
        return {"kind": "external-type", "module": module, "name": tag}

    def _node_ast(self, node_id):
        for node in self.nodes:
            if node.id == node_id:
                return self._find_def(node)
        return None


class Resolver:
    """Resolves portable callsite facts into local / external / unresolvable edges."""

    # attribute -> return type tag; used to propagate the receiver type of
    # well-known third-party method chains (pandas / sklearn / plotly / yfinance)
    SAME = "__same__"
    CHAIN_RETURN = {
        # pandas DataFrame
        "copy": "pandas.DataFrame", "dropna": "pandas.DataFrame", "fillna": "pandas.DataFrame",
        "sort_values": "pandas.DataFrame", "round": "pandas.DataFrame", "head": "pandas.DataFrame",
        "tail": "pandas.DataFrame", "where": "pandas.DataFrame", "transform": "pandas.DataFrame",
        "shift": "pandas.DataFrame", "diff": "pandas.DataFrame", "pct_change": "pandas.DataFrame",
        "rolling": "pandas.Rolling", "ewm": "pandas.EWM", "expanding": "pandas.Expanding",
        "mean": "pandas.Series", "std": "pandas.Series", "max": "pandas.Series",
        "min": "pandas.Series", "any": "pandas.Series", "all": "pandas.Series",
        "sum": "pandas.Series", "cumprod": "pandas.Series", "isna": "pandas.Series",
        "iloc": "pandas.Series", "loc": "pandas.Series",
        # plotly
        "add_trace": "plotly.Figure", "add_hline": "plotly.Figure",
        "update_layout": "plotly.Figure", "update_xaxes": "plotly.Figure",
        # yfinance
        "history": "pandas.DataFrame",
        # sklearn estimators/scalers return the fitted instance
        "fit": SAME, "fit_transform": SAME, "transform": SAME, "score": "sklearn.float",
        "predict": "numpy.ndarray",
        # dict / misc
        "get": "object", "to_csv": "object", "items": "object",
        "keys": "object", "upper": "str", "strftime": "str", "clear": "__same__",
    }
    LITERAL_TYPE = {"list": "list", "tuple": "tuple", "dict": "dict"}

    def __init__(self, facts):
        self.facts = facts
        self.by_module = {f["module"]: f for f in facts}
        self.node_ids = {n["id"] for f in facts for n in f["nodes"]}
        self.lookup = {}
        for f in facts:
            for n in f["nodes"]:
                self.lookup[(f["module"], n["name"])] = n["id"]
                self.lookup[(f["module"], n["qualname"])] = n["id"]

    def resolve_all(self):
        edges = []
        for fact in self.facts:
            for raw in fact["raw_edges"]:
                edges.append(self._resolve(fact, raw))
        return edges

    def _resolve(self, fact, raw):
        scopes = self._scopes_at(fact, raw["lineno"])
        base = {
            "src": raw["src"], "name": raw["name"], "kind": raw["kind"],
            "file": raw["file"], "lineno": raw["lineno"], "index": raw["index"],
            "scopes": scopes,
        }
        kind = raw["kind"]
        if kind == "name":
            outcome = self._resolve_name(fact, raw["src"], raw["callee"]["name"])
        elif kind == "attr":
            outcome = self._resolve_attr(fact, raw["src"], raw["callee"])
        elif kind == "subscript":
            outcome = {
                "resolved": False,
                "reason": "indirect call: target selected by subscript (obj[...](...)); concrete callable is unknown statically",
                "linked_modules": self._modules_in_desc(fact, raw["callee"].get("value")),
            }
        elif kind == "lambda":
            outcome = {"resolved": False, "reason": "call target is an inline lambda expression", "linked_modules": []}
        else:
            outcome = {
                "resolved": False,
                "reason": "indirect call through a computed callable value",
                "linked_modules": self._modules_in_desc(fact, raw["callee"]),
            }
        return Edge(**base, **outcome)

    def _scopes_at(self, fact, lineno):
        scopes = []
        for node in fact["nodes"]:
            if node["type"] in ("function", "method") and node["lineno"] <= lineno <= node["end_lineno"]:
                scopes.append(node["id"])
        scopes.sort(key=lambda sid: -sid.count("."))
        return scopes

    # -- name calls -------------------------------------------------------

    def _resolve_name(self, fact, enclosing, name):
        local_id = self._local_binding(fact, enclosing, name)
        if local_id:
            return {"resolved": True, "dst": local_id, "linked_modules": []}
        imported = self._find_import(fact, name)
        if imported:
            mod, imported_name = imported
            if imported_name is None:
                return {"resolved": True, "external": True, "target": mod, "linked_modules": []}
            local_mod = self._local_module(mod)
            if local_mod:
                target_id = self.lookup.get((local_mod, imported_name))
                if target_id:
                    return {"resolved": True, "dst": target_id, "linked_modules": [local_mod]}
                return {
                    "resolved": False,
                    "reason": f"imported name '{imported_name}' is not a statically defined def/class in local module '{local_mod}' (dynamic/indirect export)",
                    "linked_modules": [local_mod],
                }
            return {"resolved": True, "external": True, "target": f"{mod}.{imported_name}", "linked_modules": []}
        if name in BUILTIN_NAMES:
            return {"resolved": True, "external": True, "target": "builtins." + name, "linked_modules": []}
        param = fact.get("params", {}).get(enclosing, {}).get(name)
        if param:
            return {
                "resolved": False,
                "reason": f"indirect call: '{name}' is a callable parameter; target depends on the runtime caller",
                "linked_modules": [],
            }
        scoped = fact.get("origins", {}).get(enclosing, {}).get("name:" + name)
        if scoped and scoped.get("kind") in ("unknown-name",):
            return {
                "resolved": False,
                "reason": f"indirect call through local variable '{name}' of unknown type",
                "linked_modules": [],
            }
        return {
            "resolved": False,
            "reason": f"unbound/implicitly defined callable '{name}'; cannot resolve statically",
            "linked_modules": [],
        }

    @staticmethod
    def _cache_attr_target(fact, name, attr):
        node_id = fact["defs"].get(name)
        if not node_id:
            return None
        for node in fact["nodes"]:
            if node["id"] == node_id and node.get("cache") and attr in ("clear",):
                return "streamlit.cache-clear:" + name
        return None

    def _local_binding(self, fact, enclosing, name):
        if name in fact["defs"]:
            return fact["defs"][name]
        for class_id, info in fact["classes"].items():
            if enclosing in info["methods"].values() and name in info["methods"]:
                return info["methods"][name]
        return None

    def _find_import(self, fact, local_name):
        for imp in fact["imports"]:
            if imp["local"] == local_name:
                return imp["module"], imp["name"]
        return None

    def _local_module(self, dotted):
        candidates = [dotted]
        if "." in dotted:
            candidates.append(dotted.split(".")[0])
        for candidate in candidates:
            if candidate in self.by_module:
                return candidate
        return None

    def _local_module_attr(self, module, name):
        """Resolve ``from pkg import mod; mod.name`` to local module pkg.mod."""
        combined = f"{module}.{name}"
        if combined in self.by_module:
            return combined
        return self._local_module(module)

    # -- attribute calls --------------------------------------------------

    def _resolve_attr(self, fact, enclosing, desc):
        attr = desc["attr"]
        value = desc.get("value") or {}
        origin = self._origin_of_desc(fact, enclosing, value)
        kind = origin["kind"]

        if kind == "getattr":
            return {
                "resolved": False,
                "reason": "dynamic attribute lookup via getattr(); concrete method cannot be statically resolved",
                "linked_modules": self._modules_in_origin(origin),
            }
        if kind == "module":
            mod_path = origin["module"]
            chain = origin.get("attr_chain", [])
            local_mod = self._local_module_attr(mod_path, chain[0]) if chain else self._local_module(mod_path)
            if local_mod:
                target_id = self.lookup.get((local_mod, attr))
                if target_id:
                    return {"resolved": True, "dst": target_id, "linked_modules": [local_mod]}
                return {
                    "resolved": False,
                    "reason": f"'{attr}' is not a static def/class in local module '{local_mod}' (module-level indirect call)",
                    "linked_modules": [local_mod],
                }
            return {"resolved": True, "external": True, "target": f"{mod_path}.{attr}", "linked_modules": []}
        if kind in ("external-type", "external-instance"):
            name = origin.get("name", "?")
            chain = origin.get("attr_chain", [])
            if origin.get("module") == "builtins":
                return {"resolved": True, "external": True,
                        "target": f"builtins.{name}.{attr}", "linked_modules": []}
            module = origin.get("module", "")
            target = name + (("." + ".".join(chain)) if chain else "") + "." + attr
            return {"resolved": True, "external": True,
                    "target": f"{module}.{target}" if module else target, "linked_modules": []}
        if kind == "imported-name":
            return {"resolved": True, "external": True, "target": f"{origin['module']}.{origin.get('name', '')}.{attr}", "linked_modules": []}
        if kind == "self-ref":
            cls_qual = self._class_of_method(fact, enclosing)
            if cls_qual:
                method_id = self._method_in_any(f"{fact['module']}::{cls_qual}", attr)
                if method_id:
                    return {"resolved": True, "dst": method_id, "linked_modules": []}
            return {"resolved": False, "reason": f"dynamic attribute: self.{attr} dispatch depends on runtime instance type", "linked_modules": []}
        if kind == "self-attr":
            return {
                "resolved": False,
                "reason": f"dynamic attribute self.{origin.get('attr')}.{attr}: attribute has no statically known type",
                "linked_modules": [],
            }
        if kind == "local-class":
            # origin["name"] is a fully-qualified class id when carried from self_attrs
            class_id = origin["name"] if "::" in origin["name"] else f"{fact['module']}::{origin['name']}"
            if class_id in self.node_ids:
                target_node = next(n for n in fact["nodes"] if n["id"] == class_id)
                if target_node["type"] == "class":
                    method_id = self._method_in_any(class_id, attr)
                    if method_id:
                        return {"resolved": True, "dst": method_id, "linked_modules": []}
            if origin.get("module") == "builtins" or origin["name"] in ("dict", "list", "tuple", "set"):
                return {"resolved": True, "external": True,
                        "target": f"builtins.{origin['name']}.{attr}", "linked_modules": []}
            return {
                "resolved": False,
                "reason": f"class '{origin['name']}' has no statically defined method '{attr}'",
                "linked_modules": [],
            }
        if kind in ("local-call", "local-fn") and origin.get("id"):
            ret_origin = self._return_annotation_for_id(fact, origin["id"])
            if ret_origin and ret_origin.get("name") in ("dict", "Dict", "tuple", "Tuple"):
                return {"resolved": True, "external": True, "target": f"builtins.{ret_origin['name'].lower()}.{attr}", "linked_modules": []}
            if ret_origin and ret_origin.get("kind") in ("external-type", "external-instance"):
                mapped = self._chain_return(ret_origin, attr)
                if mapped is not None:
                    return {"resolved": True, "external": True,
                            "target": f"{ret_origin.get('module', '')}.{ret_origin.get('name', '')}.{attr}",
                            "linked_modules": []}
            return {
                "resolved": False,
                "reason": f"attribute call '.{attr}' on the return value of a local function with untyped result",
                "linked_modules": [],
            }
        if kind == "external-call":
            return {"resolved": True, "external": True, "target": f"{origin.get('module', '')}.{origin.get('name', '')}.{attr}", "linked_modules": []}
        if kind == "constructor":
            mod = origin.get("module")
            local_mod = self._local_module(mod) if mod else None
            ctor_name = origin.get("constructor") or origin.get("name")
            if local_mod and ctor_name:
                class_id = f"{local_mod}::{ctor_name}"
                method_id = self._method_in_any(class_id, attr)
                if method_id:
                    return {"resolved": True, "dst": method_id, "linked_modules": [local_mod]}
                return {
                    "resolved": False,
                    "reason": f"constructed local class '{ctor_name}' has no static method '{attr}' (indirect/dynamic method)",
                    "linked_modules": [local_mod],
                }
            target = f"{mod or '?'}.{ctor_name or '?'}.{attr}"
            return {"resolved": True, "external": True, "target": target, "linked_modules": []}
        if kind in ("parameter-untyped", "unknown-name", "indirect", "untyped"):
            return {
                "resolved": False,
                "reason": f"indirect call '.{attr}' on value of unknown runtime type ({kind})",
                "linked_modules": self._modules_in_origin(origin),
            }
        if kind == "call-result":
            mapped = self._chain_return(origin, attr)
            if mapped is not None and mapped.get("kind") in ("external-type", "external-instance"):
                return {"resolved": True, "external": True,
                        "target": f"{mapped.get('module', '')}.{mapped.get('name', '')}.{attr}",
                        "linked_modules": []}
        if kind == "super":
            return self._resolve_super(fact, enclosing, attr)
        if kind == "cached-fn" and attr == "clear":
            return {"resolved": True, "external": True,
                    "target": "streamlit.cache_data.clear:" + origin.get("name", ""), "linked_modules": []}
        return {"resolved": False, "reason": f"attribute call '.{attr}' on {kind} value cannot be resolved", "linked_modules": []}

    def _resolve_super(self, fact, enclosing, attr):
        class_id = None
        for cid, info in fact["classes"].items():
            if enclosing in info["methods"].values():
                class_id = cid
                break
        if not class_id:
            return {"resolved": False, "reason": "super() call outside a class cannot be resolved", "linked_modules": []}
        for base_name in fact["classes"][class_id]["bases"]:
            base_id = fact["defs"].get(base_name)
            if base_id and base_id in fact["classes"]:
                method_id = fact["classes"][base_id]["methods"].get(attr)
                if method_id:
                    return {"resolved": True, "dst": method_id, "linked_modules": []}
        return {"resolved": False, "reason": f"super().{attr} targets a non-local/dynamic base class", "linked_modules": []}

    def _method_in_any(self, class_id, attr):
        module, _, qual = class_id.partition("::")
        fact = self.by_module.get(module)
        if not fact:
            return None
        visited = set()
        queue = [class_id]
        while queue:
            current = queue.pop(0)
            if current in visited:
                continue
            visited.add(current)
            info = fact["classes"].get(current)
            if not info:
                continue
            method_id = info["methods"].get(attr)
            if method_id:
                return method_id
            for base_name in info["bases"]:
                base_id = fact["defs"].get(base_name)
                if base_id:
                    queue.append(base_id)
        return None

    # -- descriptor origins -----------------------------------------------

    def _origin_of_desc(self, fact, enclosing, desc):
        t = desc.get("t")
        if t == "attr":
            return self._attr_origin(fact, enclosing, desc)
        if t == "name":
            return self._name_origin(fact, enclosing, desc["name"])
        if t == "call":
            return self._call_origin(fact, enclosing, desc)
        if t == "subscript":
            inner = self._origin_of_desc(fact, enclosing, desc.get("value") or {})
            if inner is None:
                return {"kind": "indirect", "expr": "subscripted value"}
            if inner.get("kind") in ("external-type", "external-instance"):
                if inner.get("name") == "DataFrame":
                    value = desc.get("value") or {}
                    # multi-column selection (a list) stays a DataFrame
                    if value.get("t") == "subscript" and isinstance((value.get("slice")), dict):
                        pass
                    slice_desc = desc.get("slice")
                    if slice_desc and slice_desc.get("t") in ("list", "tuple"):
                        return {"kind": "external-type", "module": "pandas", "name": "DataFrame"}
                    return {"kind": "external-type", "module": "pandas", "name": "Series"}
                return inner
            return inner
        if t == "lambda":
            return {"kind": "indirect", "expr": "lambda"}
        return {"kind": "indirect", "expr": desc.get("expr", "expr")}

    def _name_origin(self, fact, enclosing, name):
        if name in ("self", "cls"):
            return {"kind": "self-ref"}
        cache_target = self._cache_attr_target(fact, name, "")
        if cache_target:
            return {"kind": "cached-fn", "name": name}
        param = fact.get("params", {}).get(enclosing, {}).get(name)
        if param:
            if param.get("kind") == "external-type":
                return {"kind": "external-instance", "module": param["module"], "name": param["name"]}
            if param.get("kind") == "local-class":
                return {"kind": "local-class", "name": param["name"]}
            return dict(param)
        scoped = fact.get("origins", {}).get(enclosing, {}).get("name:" + name)
        if scoped:
            if scoped.get("kind") == "external-type":
                return {"kind": "external-instance", "module": scoped["module"], "name": scoped["name"]}
            return dict(scoped)
        imported = self._find_import(fact, name)
        if imported:
            mod, imported_name = imported
            if imported_name is None:
                return {"kind": "module", "module": mod}
            return {"kind": "imported-name", "module": mod, "name": imported_name}
        if name in fact["defs"]:
            return {"kind": "local-fn", "id": fact["defs"][name]}
        return {"kind": "unknown-name", "name": name}

    def _attr_origin(self, fact, enclosing, desc):
        chain = []
        cur = desc
        while cur.get("t") == "attr":
            chain.append(cur["attr"])
            cur = cur.get("value") or {}
        chain.reverse()
        root = self._origin_of_desc(fact, enclosing, cur)

        if root["kind"] == "module":
            mod = root["module"]
            if chain:
                name = chain[-1]
                local_mod2 = self._local_module_attr(mod, chain[0])
                if local_mod2:
                    target_id = self.lookup.get((local_mod2, name))
                    if target_id:
                        return {"kind": "local-fn", "id": target_id, "module": local_mod2}
                return {"kind": "module", "module": mod, "attr": name, "attr_chain": chain}
            return root
        if root["kind"] == "self-ref":
            if not chain:
                return root
            cls_qual = self._class_of_method(fact, enclosing)
            attr_origin = fact.get("self_attrs", {}).get(cls_qual, {}).get(chain[0])
            if attr_origin is None:
                return {"kind": "self-attr", "attr": chain[0]}
            origin = dict(attr_origin)
            if origin.get("kind") == "external-type":
                origin = {"kind": "external-instance", "module": origin["module"], "name": origin["name"]}
            origin["attr_chain"] = origin.get("attr_chain", []) + chain[1:]
            return origin
        if root["kind"] in ("external-type", "external-instance", "imported-name", "indirect"):
            origin = dict(root)
            mapped = []
            current = root
            for method in chain:
                tag = self._chain_return(current, method)
                if tag is not None:
                    current = tag
                    mapped.append(method)
                else:
                    current = dict(current)
                    current["attr_chain"] = current.get("attr_chain", []) + [method]
            origin = dict(current)
            if origin["kind"] == "indirect" and mapped:
                origin["expr"] = ".".join(chain)
            return origin
        if root["kind"] == "local-fn":
            return {"kind": "indirect", "expr": "attribute of local callable"}
        return {"kind": "indirect", "expr": ".".join(chain)}

    def _call_origin(self, fact, enclosing, desc):
        func = desc.get("func") or {}
        if func.get("t") == "name" and func.get("name") == "getattr":
            return {"kind": "getattr"}
        if func.get("t") == "attr":
            chain = []
            cur = func
            while cur.get("t") == "attr":
                chain.append(cur["attr"])
                cur = cur.get("value") or {}
            chain.reverse()
            receiver = self._origin_of_desc(fact, enclosing, cur)
            method = chain[-1] if chain else ""
            if receiver["kind"] == "call-result":
                mapped = self._chain_return(receiver, method)
                if mapped is not None:
                    return mapped
            if receiver["kind"] == "constructor" and receiver.get("module", "").startswith("sklearn"):
                mapped = self._chain_return(
                    {"kind": "external-instance", "module": "sklearn", "name": receiver.get("constructor", "Estimator")},
                    method,
                )
                if mapped is not None:
                    return mapped
                return {"kind": "external-type", "module": "sklearn", "name": receiver.get("constructor", "Estimator")}
            if receiver["kind"] == "self-ref":
                # self.method(...) is a local bound-method invocation
                cls_qual = self._class_of_method(fact, enclosing)
                method_id = self._method_in_any(f"{fact['module']}::{cls_qual}", method)
                if method_id:
                    ret = self._return_annotation(fact, method_id)
                    return ret or {"kind": "local-call", "id": method_id, "returns": "object"}
            if receiver["kind"] == "local-class":
                class_id = f"{fact['module']}::{receiver['name']}"
                method_id = self._method_in_any(class_id, method)
                if method_id:
                    ret = self._return_annotation(fact, method_id)
                    return ret or {"kind": "local-call", "id": method_id, "returns": "object"}
            if receiver["kind"] in ("external-type", "external-instance"):
                mapped = self._chain_return(receiver, method)
                if mapped is not None:
                    return mapped
                origin = dict(receiver)
                origin["attr_chain"] = origin.get("attr_chain", []) + chain[:-1]
                return origin
            if receiver["kind"] == "self-attr":
                mapped = self._chain_return(receiver, method)
                return mapped or {"kind": "indirect", "expr": f"self.{receiver.get('attr')}.{method}"}
            if receiver["kind"] in ("indirect", "unknown-name", "untyped", "parameter-untyped"):
                return {"kind": "indirect", "expr": ".".join(chain)}
            if receiver["kind"] == "module":
                name = chain[-1]
                mod = receiver["module"]
                local_mod = self._local_module(mod)
                if local_mod:
                    target_id = self.lookup.get((local_mod, name))
                    if target_id:
                        ret = self._return_annotation(self.by_module[local_mod], target_id)
                        return ret or {"kind": "local-fn", "id": target_id}
                return {"kind": "external-call", "module": mod, "name": name}
            if chain and chain[0] == "super":
                return {"kind": "super"}
            return {"kind": "indirect", "expr": ".".join(chain)}
        if func.get("t") == "name":
            name = func["name"]
            imported = self._find_import(fact, name)
            if imported:
                origin = {"kind": "constructor", "constructor": imported[1], "module": imported[0]}
                if imported[1] == "Ticker":
                    origin["typed_name"] = "yfinance.Ticker"
                return origin
            if name in fact["defs"]:
                node_id = fact["defs"][name]
                node_qual = node_id.split("::", 1)[1]
                if any(cid.split("::", 1)[1] == node_qual for cid in fact["classes"]):
                    return {"kind": "local-class", "name": node_qual}
                ret = self._return_annotation(fact, node_id)
                return ret or {"kind": "local-call", "id": node_id, "returns": "object"}
            param = fact.get("params", {}).get(enclosing, {}).get(name)
            if param and param.get("kind") == "local-class":
                return {"kind": "local-class", "name": param["name"]}
            return {"kind": "indirect", "expr": name}
        return {"kind": "indirect"}

    def _chain_return(self, receiver, method):
        tag = self.CHAIN_RETURN.get(method)
        if tag is None:
            return None
        if tag == self.SAME:
            if receiver.get("kind") in ("external-type", "external-instance"):
                return dict(receiver)
            if receiver.get("typed_name") == "sklearn":
                return {"kind": "external-type", "module": "sklearn", "name": receiver.get("name", "Estimator")}
            return None
        if tag == "object":
            return None
        if "." in tag:
            mod, name = tag.rsplit(".", 1)
            return {"kind": "external-type", "module": mod, "name": name}
        mod_map = {
            "DataFrame": "pandas", "Series": "pandas", "Rolling": "pandas",
            "EWM": "pandas", "Expanding": "pandas", "Figure": "plotly",
        }
        return {"kind": "external-type", "module": mod_map.get(tag, ""), "name": tag}

    def _return_annotation(self, fact, node_id):
        for node in fact["nodes"]:
            if node["id"] == node_id:
                ret = node.get("returns")
                if ret:
                    return dict(ret)
        return None

    def _return_annotation_for_id(self, fact, node_id):
        module, _, _qual = node_id.partition("::")
        target_fact = self.by_module.get(module, fact)
        return self._return_annotation(target_fact, node_id)

    def _class_of_method(self, fact, enclosing):
        for class_id, info in fact["classes"].items():
            if enclosing in info["methods"].values():
                return class_id.split("::", 1)[1]
        return None

    def _modules_in_desc(self, fact, desc):
        if not desc:
            return []
        modules = set()
        if desc.get("t") == "name":
            imported = self._find_import(fact, desc["name"])
            if imported:
                local_mod = self._local_module(imported[0])
                if local_mod:
                    modules.add(local_mod)
        for key, value in desc.items():
            if isinstance(value, dict):
                modules.update(self._modules_in_desc(fact, value))
        return sorted(modules)

    def _modules_in_origin(self, origin):
        mod = origin.get("module")
        if not mod:
            return []
        local_mod = self._local_module(mod)
        return [local_mod] if local_mod else []


class CallGraphBuilder:
    """Full/incremental orchestration with edge-level consistency checks."""

    def __init__(self, root, graph_path, state_path):
        self.root = os.path.abspath(root)
        self.graph_path = graph_path
        self.state_path = state_path

    def run(self, force_full=False):
        rel_files = discover_python_files(self.root)
        hashes = {rel: sha256_of(os.path.join(self.root, rel)) for rel in rel_files}
        previous = self._load_state() if not force_full else {}

        old_hashes = previous.get("hashes", {}) if previous else {}
        old_modules = set(old_hashes)
        new_modules = set(hashes)
        changed = sorted(m for m in new_modules if old_hashes.get(m) != hashes.get(m))
        added = sorted(new_modules - old_modules)
        removed = sorted(old_modules - new_modules)
        can_increment = bool(previous) and not force_full

        mode = "incremental" if can_increment else "full"
        fallback_reason = None

        if can_increment and self._has_cross_module_indirect(
            previous, set(changed) | set(added) | set(removed)
        ):
            can_increment = False
            mode = "full"
            fallback_reason = (
                "changed module is referenced by an unresolved cross-module/indirect edge; "
                "old edge could stay stale, falling back to full rescan"
            )

        if can_increment:
            facts = []
            reused_modules, rescanned_modules = [], []
            for rel in rel_files:
                module = module_name_of(rel)
                if rel in changed or rel in added:
                    facts.append(self._scan_one(rel).fact_dict())
                    rescanned_modules.append(module)
                else:
                    cached = previous["facts_by_module"].get(module)
                    if cached is None:
                        can_increment = False
                        fallback_reason = f"no cached facts for unchanged module {module}; falling back"
                        break
                    facts.append(cached)
                    reused_modules.append(module)
            if not can_increment:
                mode = "full"

        if mode == "incremental":
            resolver = Resolver(facts)
            edges = resolver.resolve_all()
            full_edges = self._full_resolve(rel_files)
            edge_set = {e.identity() for e in edges}
            full_set = {e.identity() for e in full_edges}
            if edge_set != full_set:
                mode = "full"
                stale = sorted(edge_set - full_set)
                missing = sorted(full_set - edge_set)
                fallback_reason = (
                    "incremental result differed from full rescan "
                    f"(stale edges: {len(stale)}, missing edges: {len(missing)}); falling back"
                )

        if mode == "full":
            facts = [self._scan_one(rel).fact_dict() for rel in rel_files]
            resolver = Resolver(facts)
            edges = resolver.resolve_all()
            reused_modules, rescanned_modules = [], [module_name_of(r) for r in rel_files]

        tabs = self._merged_tabs(facts)
        nodes = [n for f in facts for n in f["nodes"]]
        node_ids = {n["id"] for n in nodes}
        edges = [e for e in edges if e.src in node_ids]
        edges = self._annotate_tab_boundaries(nodes, edges, tabs)
        invalidations = [inv for f in facts for inv in f["invalidations"]]

        total = len(edges)
        unresolved = sum(1 for e in edges if not e.resolved)
        resolved_local = sum(1 for e in edges if e.resolved and not e.external)
        resolved_external = sum(1 for e in edges if e.resolved and e.external)
        unresolvable_ratio = round(unresolved / total, 6) if total else 0.0
        coverage = round((total - unresolved) / total, 6) if total else 0.0

        graph = {
            "schema_version": 1,
            "generated_at": _now_iso(),
            "root": relpos(self.root, self.root) or ".",
            "modules": sorted(module_name_of(r) for r in rel_files),
            "thresholds": {
                "unresolvable_ratio": UNRESOLVABLE_THRESHOLD,
                "coverage_gate": COVERAGE_GATE,
            },
            "build": {
                "mode": mode,
                "fallback_reason": fallback_reason,
                "rescanned_modules": sorted(rescanned_modules),
                "reused_modules": sorted(reused_modules),
                "changed_files": sorted(changed),
                "added_files": sorted(added),
                "removed_files": sorted(removed),
            },
            "nodes": sorted(nodes, key=lambda n: (n["file"], n["lineno"], n["id"])),
            "edges": sorted((e.as_dict() for e in edges), key=lambda e: (e["file"], e["lineno"], e["index"])),
            "tabs": tabs,
            "invalidations": sorted(invalidations, key=lambda i: (i["file"], i["lineno"])),
            "stats": {
                "total_callsites": total,
                "resolved_local": resolved_local,
                "resolved_external": resolved_external,
                "unresolved": unresolved,
                "coverage": coverage,
                "unresolvable_ratio": unresolvable_ratio,
                "coverage_gate_passed": coverage >= COVERAGE_GATE,
                "unresolvable_gate_passed": unresolvable_ratio <= UNRESOLVABLE_THRESHOLD,
                "functions": sum(1 for n in nodes if n["type"] in ("function", "method")),
                "classes": sum(1 for n in nodes if n["type"] == "class"),
                "modules_scanned": len(rel_files),
            },
        }

        self._atomic_write_json(self.graph_path, graph)
        self._atomic_write_json(self.state_path, {
            "schema_version": 1, "hashes": hashes, "facts_by_module": {f["module"]: f for f in facts},
        })
        return graph

    def _scan_one(self, rel):
        path = os.path.join(self.root, rel)
        with open(path, "r", encoding="utf-8") as handle:
            source = handle.read()
        analyzer = ModuleAnalyzer(module_name_of(rel), rel, source)
        analyzer.analyze()
        analyzer.collect_facts()
        return analyzer

    def _full_resolve(self, rel_files):
        fresh_facts = []
        for rel in rel_files:
            try:
                fresh_facts.append(self._scan_one(rel).fact_dict())
            except SyntaxError:
                continue
        return Resolver(fresh_facts).resolve_all()

    def _load_state(self):
        if not os.path.exists(self.state_path):
            return {}
        try:
            with open(self.state_path, "r", encoding="utf-8") as handle:
                state = json.load(handle)
        except (json.JSONDecodeError, OSError):
            return {}
        if state.get("schema_version") != 1:
            return {}
        return state

    @staticmethod
    def _has_cross_module_indirect(previous, changed_files):
        changed_modules = {module_name_of(f) for f in changed_files}
        known_modules = set(previous.get("facts_by_module", {}))
        for module, fact in previous.get("facts_by_module", {}).items():
            if module in changed_modules:
                continue
            for raw in fact["raw_edges"]:
                if raw.get("kind") not in ("subscript", "indirect", "lambda"):
                    # name calls and static module.attr calls are fully re-resolved;
                    # only subscript/indirect/dynamic-attr callsites can keep stale edges
                    if not _desc_has_indirect_receiver(raw.get("callee", {})):
                        continue
                linked = _raw_edge_modules(fact, raw, known_modules)
                if linked & changed_modules:
                    return True
        return False

    @staticmethod
    def _merged_tabs(facts):
        tabs = []
        for fact in facts:
            tabs.extend(fact["tabs"])
        return sorted(tabs, key=lambda t: (t["file"], t["lineno"]))

    @staticmethod
    def _annotate_tab_boundaries(nodes, edges, tabs):
        node_by_id = {n["id"]: n for n in nodes}

        def containing_tabs(file, lineno):
            return [t["name"] for t in tabs if t["file"] == file and t["span_start"] <= lineno <= t["span_end"]]

        for edge in edges:
            src_tabs = set(containing_tabs(edge.file, edge.lineno))
            dst_node = node_by_id.get(edge.dst) if edge.dst else None
            dst_tabs = set()
            if dst_node:
                dst_tabs = set(containing_tabs(dst_node["file"], dst_node["lineno"]))
            shared = src_tabs & dst_tabs
            boundary = {
                "caller_tabs": sorted(src_tabs),
                "callee_tabs": sorted(dst_tabs),
                "classification": (
                    "shared" if shared and src_tabs and dst_tabs else
                    "recompute" if src_tabs and not dst_tabs else
                    "cross-tab" if src_tabs and dst_tabs and not shared else
                    "outside-tabs"
                ),
            }
            edge.tab_boundary = boundary
        return edges

    @staticmethod
    def _atomic_write_json(path, payload):
        directory = os.path.dirname(path)
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".graph-", suffix=".json", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)


def _desc_modules(desc):
    modules = set()
    if isinstance(desc, dict):
        if desc.get("t") == "name" and isinstance(desc.get("name"), str):
            pass
        for value in desc.values():
            if isinstance(value, dict):
                modules.update(_desc_modules(value))
    return modules


def _raw_edge_modules(fact, raw, known_modules):
    """Local modules referenced by names in a callsite descriptor."""
    imports_by_local = {imp["local"]: imp for imp in fact.get("imports", [])}
    modules = set()

    def resolve(imp):
        module = imp["module"]
        candidates = []
        if imp["name"]:
            candidates.append(f"{module}.{imp['name']}")
        candidates.append(module)
        if "." in module:
            candidates.append(module.split(".")[0])
        for candidate in candidates:
            if candidate in known_modules:
                return candidate
        return None

    def walk(desc):
        if isinstance(desc, dict):
            if desc.get("t") == "name":
                imp = imports_by_local.get(desc.get("name"))
                if imp:
                    local = resolve(imp)
                    if local:
                        modules.add(local)
            for value in desc.values():
                if isinstance(value, dict):
                    walk(value)

    walk(raw.get("callee", {}))
    return modules


def _desc_has_indirect_receiver(desc):
    if desc.get("t") in ("subscript", "call", "lambda", "other"):
        return True
    if desc.get("t") == "attr":
        return _desc_has_indirect_receiver(desc.get("value") or {})
    return False


def _now_iso():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Build the static call graph for the stock dashboard")
    default_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    parser.add_argument("--root", default=default_root, help="project root to scan")
    parser.add_argument("--graph", default=None, help="output graph.json path")
    parser.add_argument("--state", default=None, help="incremental state path")
    parser.add_argument("--full", action="store_true", help="force a full rescan")
    args = parser.parse_args(argv)

    graph_path = args.graph or os.path.join(default_root, "trace", "graph.json")
    state_path = args.state or os.path.join(default_root, "trace", ".graph-state.json")
    builder = CallGraphBuilder(args.root, graph_path, state_path)
    graph = builder.run(force_full=args.full)
    stats = graph["stats"]
    print(
        f"[trace] mode={graph['build']['mode']} "
        f"modules={stats['modules_scanned']} functions={stats['functions']} callsites={stats['total_callsites']} "
        f"coverage={stats['coverage']:.4f} unresolvable={stats['unresolvable_ratio']:.4f}"
    )
    if graph["build"]["fallback_reason"]:
        print("[trace] fallback: " + graph["build"]["fallback_reason"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
