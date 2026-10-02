# -*- coding: utf-8 -*-
"""
_lint_unbound.py —— 查「可能未绑定」的局部变量。

为什么需要它：pyflakes **不报**这一类 ——
    for v in x.get('action_values', []):
        if v.get('action_type') == 'purchase':
            dpv = float(v.get('value', 0) or 0)   # 赋值语句存在，但可能一次都不执行
    ... round(dpv, 2)                             # 循环 0 次 / 条件不成立 → UnboundLocalError
语法上 dpv「有赋值」，所以 pyflakes 认为是已定义（已实测确认它不报）。
真实案例：2026-09-30 `api_fb.py:1189` 用户账户实测崩溃。

规则（按块内源码顺序做保守数据流）：
  · 块内**直接**赋值 → 视为已绑定，后续可用
  · for / while / if / try / with 的**子块内**赋值 → 不提升到外层（子块可能不执行）
  · 例外：if/else 两分支都赋值同名变量 → 提升（减少误报）
  · Load 时该名字既不在已绑定集合、也不是内建/全局/参数 → 报「可能未绑定」

用法：python _lint_unbound.py <目标.py> [更多.py ...]
"""
import ast
import builtins
import io
import os
import sys

# 模块级隐式全局：由解释器在 import 时注入，任何函数里都可见，不算「未绑定」
MODULE_DUNDERS = {
    '__file__', '__name__', '__doc__', '__package__', '__spec__', '__builtins__',
    '__loader__', '__annotations__', '__cached__',
}
BUILTINS = set(dir(builtins)) | MODULE_DUNDERS

# 哪些 AST 节点带「子块」，以及子块属性名
CHILD_BLOCKS = {
    ast.For: ('body', 'orelse'),
    ast.AsyncFor: ('body', 'orelse'),
    ast.While: ('body', 'orelse'),
    ast.If: ('body', 'orelse'),
    ast.Try: ('body', 'handlers', 'orelse', 'finalbody'),
    ast.With: ('body',),
    ast.AsyncWith: ('body',),
}
if hasattr(ast, 'TryStar'):
    CHILD_BLOCKS[ast.TryStar] = ('body', 'handlers', 'orelse', 'finalbody')


def _targets(node):
    """赋值目标的 Name 集合（递归解包 元组/列表/星号/属性不算）"""
    names = set()
    if isinstance(node, ast.Name):
        names.add(node.id)
    elif isinstance(node, (ast.Tuple, ast.List)):
        for e in node.elts:
            names |= _targets(e)
    elif isinstance(node, ast.Starred):
        names |= _targets(node.value)
    return names


class Blk:
    """一个语句块的检查结果：本块内直接赋值的名字"""

    def __init__(self, checker, owner):
        self.c = checker
        self.owner = owner


class Checker:
    def __init__(self, path, src):
        self.path = path
        self.src = src
        self.issues = []          # (lineno, name, kind)
        self.module_names = set()  # 模块级可见的名字

    # ---------- 收集「子块里出现的赋值」的辅助 ----------
    def child_stmts(self, stmt):
        """产出 (子块语句列表, 该子块额外绑定的名字)
        except 分支要额外绑定 `as e` 里的 e，否则 handler 体内用 e 会被误报。"""
        out = []
        for attr in CHILD_BLOCKS.get(type(stmt), ()):
            node = getattr(stmt, attr, None)
            if node is None:
                continue
            if attr == 'handlers':
                for h in node:
                    out.append((h.body, {h.name} if h.name else set()))
            elif isinstance(node, list):
                out.append((node, set()))
        return out

    def directs(self, stmt):
        """本语句『直接』赋值的名字（不含子块内部）"""
        names = set()
        if isinstance(stmt, ast.Assign):
            for t in stmt.targets:
                names |= _targets(t)
        elif isinstance(stmt, (ast.AugAssign, ast.AnnAssign)):
            names |= _targets(stmt.target)
        elif isinstance(stmt, (ast.For, ast.AsyncFor)):
            names |= _targets(stmt.target)      # 循环变量按直接赋值处理（务实，避免大量误报）
        elif isinstance(stmt, (ast.With, ast.AsyncWith)):
            for it in stmt.items:
                if it.optional_vars is not None:
                    names |= _targets(it.optional_vars)
        elif isinstance(stmt, ast.NamedExpr):
            names |= _targets(stmt.target)
        elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(stmt.name)
        elif isinstance(stmt, (ast.Import, ast.ImportFrom)):
            for a in stmt.names:
                names.add((a.asname or a.name).split('.')[0])
        elif isinstance(stmt, ast.ExceptHandler) and stmt.name:
            names.add(stmt.name)
        elif isinstance(stmt, ast.Global):
            names |= set(stmt.names)
        return names

    def loads_of(self, stmt, skip_children=True):
        """本语句中读取的名字（skip_children=True 时不进入子块）"""
        # 函数/类定义语句：它的函数体是独立作用域，绝不能扫进来
        # （否则嵌套函数 `def _c2u(v): return float(v)...` 的参数 v 会被当成外层未绑定变量）
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            return []
        found = []

        class V(ast.NodeVisitor):
            def visit_Name(self, n):
                if isinstance(n.ctx, ast.Load):
                    found.append(n)
                self.generic_visit(n)

            def visit_FunctionDef(self, n):   # 不进入嵌套函数/类（独立作用域）
                pass
            visit_AsyncFunctionDef = visit_FunctionDef

            def visit_ClassDef(self, n):
                pass

            def visit_Lambda(self, n):
                pass

            def visit_ListComp(self, n):      # 推导式有自己的作用域，跳过以降噪
                pass
            visit_SetComp = visit_ListComp
            visit_DictComp = visit_ListComp
            visit_GeneratorExp = visit_ListComp

        v = V()
        if skip_children:
            for attr in CHILD_BLOCKS.get(type(stmt), ()):
                pass
            # 手动遍历，跳过子块
            for field, value in ast.iter_fields(stmt):
                if field in CHILD_BLOCKS.get(type(stmt), ()):
                    # 子块只取其「头部表达式」（如 for 的 iter、if 的 test、with 的 items）
                    if field in ('body', 'orelse', 'finalbody'):
                        continue
                    if field == 'handlers':
                        continue
                    v.visit(value)
                    continue
                if isinstance(value, list):
                    for x in value:
                        if isinstance(x, ast.AST):
                            v.visit(x)
                elif isinstance(value, ast.AST):
                    v.visit(value)
            # for/while 的 test/iter 在其它 field，已覆盖
            if isinstance(stmt, (ast.For, ast.AsyncFor)):
                v.visit(stmt.iter)
                v.visit(stmt.target)
            if isinstance(stmt, ast.While):
                v.visit(stmt.test)
            if isinstance(stmt, (ast.Import, ast.ImportFrom)):
                return []
        else:
            v.visit(stmt)
        return found

    def stores_in_subtree(self, stmts):
        """子块内所有**直接**赋值（②b 循环/try 提升用，刻意不递归以保住 dpv 类检出）"""
        names = set()
        for s in stmts:
            names |= self.directs(s)
        return names

    def _collect_stores(self, node):
        """递归收集 node 子树里所有 Store 的名字（不进入嵌套函数/类/推导式）"""
        names = set()

        def walk(n):
            for ch in ast.iter_child_nodes(n):
                if isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef,
                                   ast.ClassDef, ast.Lambda)):
                    continue
                if isinstance(ch, ast.Name) and isinstance(ch.ctx, ast.Store):
                    names.add(ch.id)
                walk(ch)

        walk(node)
        return names

    def deep_stores(self, stmts):
        """语句列表里（含所有子块）赋值的名字 —— 判断「所有路径都赋值」用"""
        names = set()
        for s in stmts:
            names |= self._collect_stores(s)
        return names

    def ends_with_exit(self, stmts):
        """该语句块是否以终止语句收尾（走不到块后的代码）"""
        if not stmts:
            return False
        last = stmts[-1]
        if isinstance(last, (ast.Raise, ast.Return, ast.Continue, ast.Break)):
            return True
        if isinstance(last, ast.If) and last.orelse:
            return self.ends_with_exit(last.body) and self.ends_with_exit(last.orelse)
        return False

    # ---------- 主流程 ----------
    def check_block(self, stmts, defined, fn_name):
        defined = set(defined)
        for stmt in stmts:
            own = self.directs(stmt)          # 本语句自身赋值（for 目标 / with as 也算）
            inner = defined | own             # 能进入子块 ⇒ 这些一定已绑定

            # ① 递归检查子块（子块内的赋值不提升到外层）
            sub_sets = []
            for sub, extra in self.child_stmts(stmt):
                self.check_block(sub, inner | extra, fn_name)
                sub_sets.append(self.stores_in_subtree(sub))

            # ② if / elif / else：只有「所有可行路径都赋值」时才提升。
            #    终止性分支（以 raise / return / continue / break 收尾）走不到后面，不参与判断。
            #    无 orelse ⇒ 存在「整块都不执行」的路径 ⇒ 绝不提升
            #    （这正是 dpv 那类 bug 的判定依据，不能放宽）
            if isinstance(stmt, ast.If):
                bs = self.deep_stores(stmt.body)
                bt = self.ends_with_exit(stmt.body)
                if stmt.orelse:
                    es = self.deep_stores(stmt.orelse)
                    et = self.ends_with_exit(stmt.orelse)
                    if bt and not et:
                        defined |= es
                    elif et and not bt:
                        defined |= bs
                    elif not bt and not et:
                        defined |= (bs & es)
                    # bt and et：两条路都终止，后面的代码不可达
                # 无 orelse：不提升

            # ②b try / with / 循环体的「直接层」赋值视为已绑定 → 提升。
            #   理由：`try: resp = req() ... except: return` 之后用 resp、
            #         `for c in cs: cur = c` 之后用 cur，都是极常见且安全的写法；
            #         不提升会产出海量噪声，把真正的 bug 淹掉。这是刻意的权衡。
            #   ⚠️ 只提升**直接层**（stores_in_subtree 只取本层 directs）——
            #      这样 `for v: if cond: dpv = ...` 里藏在 if 内部的 dpv 依然会被报出来。
            _SAFE = (ast.Try, ast.With, ast.AsyncWith,
                     ast.For, ast.AsyncFor, ast.While)
            if hasattr(ast, 'TryStar'):
                _SAFE = _SAFE + (ast.TryStar,)
            if isinstance(stmt, _SAFE):
                for s in sub_sets:
                    defined |= s

            # ③ 检查本语句自己的读取
            for n in self.loads_of(stmt):
                nm = n.id
                if nm in inner or nm in self.module_names or nm in BUILTINS:
                    continue
                self.issues.append((n.lineno, nm, '可能未绑定：仅在某些分支/循环里赋值，此处可能读不到'))

            # ④ 本语句自己的赋值
            defined |= own
        return defined

    def collect_module(self, tree):
        """只收集**模块级**的名字；不进入函数/类体，否则会把函数内局部变量误当全局名。"""
        for stmt in tree.body:
            self.module_names |= self.directs(stmt)

    def run(self, tree):
        self.collect_module(tree)

        # 建立父子关系，用于收集「外层函数的参数」——嵌套函数/闭包能直接读外层参数与 self，
        # 否则 `def worker(): ... self.x ...` 这类会被误报成未绑定。
        parents = {}
        for n in ast.walk(tree):
            for ch in ast.iter_child_nodes(n):
                parents[ch] = n

        def scope_names(fn):
            """fn 可见的名字 = 自身参数 + 所有**外层**函数的参数与局部变量（闭包）。
            注意：只对外层函数收集局部变量 —— 若把自己也收进来，fn 内的 dpv 类问题会被掩盖。"""
            names = set()
            node = fn
            count = 0
            while node is not None:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                    count += 1
                    a = node.args
                    for x in list(a.posonlyargs) + list(a.args) + list(a.kwonlyargs):
                        names.add(x.arg)
                    if a.vararg:
                        names.add(a.vararg.arg)
                    if a.kwarg:
                        names.add(a.kwarg.arg)
                    if count > 1:                     # 外层函数的局部变量，闭包内可见
                        names |= self._collect_stores(node)
                node = parents.get(node)
            return names

        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.check_block(node.body, scope_names(node), node.name)
        return self.issues


def lint(path):
    try:
        src = io.open(path, encoding='utf-8', errors='replace').read()
        tree = ast.parse(src, filename=path)
    except SyntaxError as e:
        return [(e.lineno or 0, '<syntax>', str(e))]
    return Checker(path, src).run(tree)


if __name__ == '__main__':
    targets = sys.argv[1:]
    if not targets:
        print('用法: python _lint_unbound.py <file.py> [more.py ...]')
        sys.exit(2)
    total = 0
    for p in targets:
        issues = lint(p)
        if issues:
            print(f'=== {os.path.basename(p)} ({len(issues)} 处) ===')
            for ln, nm, msg in sorted(issues):
                print(f'  {ln:5d}  [{nm}] {msg}')
                total += 1
        else:
            print(f'=== {os.path.basename(p)}  干净 ===')
    print(f'\n合计 {total} 处可疑')
    sys.exit(1 if total else 0)
