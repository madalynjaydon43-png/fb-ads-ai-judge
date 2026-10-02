# -*- coding: utf-8 -*-
"""重复定义检查器（2026-09-30）

背景：fb_ai_scheduler.py 里曾同时存在两个 `is_running` —— 新加的防重入版本写在类中间，
类末尾原有的同名方法把它**静默覆盖**了（Python 后定义者胜，不报错不警告）。
后果是「手动立即判断永远被拒」，且完全没有报错痕迹。

pyflakes / pylint 默认都不报这个（同名覆盖在子类场景是合法写法）。
所以自己扫：类内同名方法、模块级同名函数、同名顶层赋值。

用法：
    python _lint_dupdef.py <file.py> [file2.py ...]
退出码：0 = 干净，1 = 发现问题。
"""
import ast
import io
import sys

out = []


def say(s=''):
    out.append(s)


def scan_class(node):
    """返回类内重复定义的方法名（含 @property / setter 的正常成对写法不算重复）"""
    seen = {}
    dups = []
    for item in node.body:
        if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        name = item.name
        # property setter / deleter 是合法同名的成对写法，跳过
        decs = []
        for d in item.decorator_list:
            d = d.func if isinstance(d, ast.Call) else d
            if isinstance(d, ast.Attribute):
                decs.append(d.attr)
            elif isinstance(d, ast.Name):
                decs.append(d.id)
        if any(x in ('setter', 'deleter', 'getter') for x in decs):
            continue
        # 普通 @property 之后再定义同名普通方法才是真重复
        if name in seen:
            dups.append((name, seen[name], item.lineno))
        else:
            seen[name] = item.lineno
    return dups


def _reads_between(body, lo, hi, name):
    """body 中行号落在 (lo, hi) 开区间内的语句里，有没有【读取】过 name。

    这是区分「真重复定义」和「正常重新赋值」的关键：
      snap = make_snapshot(...)      # L129
      print(len(snap))               # 中间读过了 → 第一份值被用了，赋值是有意义的
      snap = [r for r in snap ...]   # L146  → 不是 bug
    反之中间没人读，第一份值就是**死写入**（如两个 is_running，前一个白写）。
    """
    for stmt in body:
        if not (lo < getattr(stmt, 'lineno', -1) < hi):
            continue
        for n in ast.walk(stmt):
            if isinstance(n, ast.Name) and n.id == name and isinstance(n.ctx, ast.Load):
                return True
    return False


def scan_module(node):
    """模块级重复定义。

    - def / class 同名两次 → 一定可疑（后定义覆盖前定义）
    - 普通赋值同名两次 → 仅当第一份值**中间从未被读取**时才算可疑（死写入）
    """
    seen = {}
    dups = []
    for item in node.body:
        names = []
        hard = False
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.append(item.name)
            hard = True
        elif isinstance(item, ast.Assign):
            for t in item.targets:
                if isinstance(t, ast.Name):
                    names.append(t.id)
        elif isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
            names.append(item.target.id)
        for name in names:
            if name in seen:
                first = seen[name]
                if hard or not _reads_between(node.body, first, item.lineno, name):
                    dups.append((name, first, item.lineno))
                else:
                    seen[name] = item.lineno   # 正常重新赋值，以最新一次为准
            else:
                seen[name] = item.lineno
    return dups


def check(path):
    try:
        raw = open(path, 'rb').read()
        src = raw.decode('utf-8')
    except Exception as e:
        say('  !! 读不了 %s: %s' % (path, e))
        return 1
    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        say('  !! 语法错误 %s: %s' % (path, e))
        return 1

    n = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for name, first, again in scan_class(node):
                say('  [类内重复] %s :: class %s.%s  首次 L%d / 覆盖 L%d'
                    % (path, node.name, name, first, again))
                n += 1
    for name, first, again in scan_module(tree):
        say('  [模块级重复] %s :: %s  首次 L%d / 覆盖 L%d' % (path, name, first, again))
        n += 1
    if n == 0:
        say('  OK   %s' % path)
    return n


def main():
    args = [a for a in sys.argv[1:] if not a.startswith('-')]
    if not args:
        say('用法：python _lint_dupdef.py <file.py> [...]')
        return 2
    total = 0
    for p in args:
        total += check(p)
    say('')
    say('重复定义合计：%d 处' % total)
    return 1 if total else 0


if __name__ == '__main__':
    rc = main()
    with io.open('_lint_dupdef_out.txt', 'w', encoding='utf-8') as f:
        f.write('\n'.join(out) + '\n')
    print('\n'.join(out))
    sys.exit(rc)
