# -*- coding: utf-8 -*-
"""self.xxx 属性一致性检查器（2026-09-30）

为什么需要：Tkinter GUI 里属性名写错（`self.ai_suggestions` 写成 `self.ai_suggestion`）
不会在启动时报错 —— 只有**点到那个按钮**时才 AttributeError。7000 行的 GUI 里靠肉眼
逐个按钮点是不现实的，而 pyflakes 只管局部变量、不管属性。

规则：
  一个类里凡是出现过 `self.X = ...` 或 `def X(self)`，X 就算「已定义」。
  任何 `self.X` 的**读取**若不在已定义集合里 → 报可疑（大概率是拼写错误）。

已知会误报、需白名单的情况：
  · 属性由父类 / mixin 定义 —— **已处理**：类若有本文件之外的父类，直接跳过
    （否则 `unittest.TestCase` 里的 self.assertEqual 会刷出几十条假警报，
     真问题被淹掉）
  · 通过 setattr 动态挂的
  · Tk 的事件回调名（tag_bind 传字符串，不是 self.X）
用法：
    python _lint_attrs.py <file.py> [...]
退出码：0 = 干净，1 = 有可疑。
"""
import ast
import io
import sys

out = []


def say(s=''):
    out.append(s)


def defined_names(cls):
    """类内所有 self.X 的写入目标 + 方法名"""
    names = set()
    for item in cls.body:
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(item.name)
        # 类级常量 / 类属性：`BASE_URL = "..."` 之后用 self.BASE_URL 读是合法的
        elif isinstance(item, ast.Assign):
            for t in item.targets:
                if isinstance(t, ast.Name):
                    names.add(t.id)
        elif isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
            names.add(item.target.id)
    for n in ast.walk(cls):
        if isinstance(n, ast.Attribute) and isinstance(n.ctx, (ast.Store, ast.Del)):
            if isinstance(n.value, ast.Name) and n.value.id == 'self':
                names.add(n.attr)
        # self.X += 1 这种 AugAssign 目标也是 Store，上面已覆盖
        if isinstance(n, ast.Assign):
            for t in n.targets:
                for sub in ast.walk(t):
                    if isinstance(sub, ast.Attribute) and isinstance(sub.value, ast.Name) \
                            and sub.value.id == 'self':
                        pass  # 已在 Attribute/Store 分支收集
    return names


def used_names(cls):
    """类内所有 self.X 的读取点"""
    used = {}
    for n in ast.walk(cls):
        if isinstance(n, ast.Attribute) and isinstance(n.ctx, ast.Load):
            if isinstance(n.value, ast.Name) and n.value.id == 'self':
                used.setdefault(n.attr, n.lineno)
    return used


def foreign_bases(cls, local_classes):
    """返回该类的「本文件之外的父类」名列表。

    有这种父类时，类的属性可能由父类/mixin 提供，静态看不到 —— 必须跳过，
    否则 unittest.TestCase 子类里的 self.assertEqual 会被当成拼写错误。
    """
    out = []
    for b in cls.bases:
        if isinstance(b, ast.Name):
            if b.id == 'object' or b.id in local_classes:
                continue
            out.append(b.id)
        elif isinstance(b, ast.Attribute):
            # unittest.TestCase 这种「模块.类」写法
            out.append('%s.%s' % (getattr(b.value, 'id', '?'), b.attr))
        else:
            out.append('<表达式>')
    return out


def check(path):
    try:
        src = open(path, 'rb').read().decode('utf-8')
        tree = ast.parse(src)
    except Exception as e:
        say('  !! %s: %s' % (path, e))
        return 1
    n = 0
    local_classes = set(x.name for x in ast.walk(tree) if isinstance(x, ast.ClassDef))
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        fb = foreign_bases(node, local_classes)
        if fb:
            say('  SKIP %s :: class %s（父类 %s，继承来的属性静态判不了）'
                % (path, node.name, '、'.join(fb)))
            continue
        d = defined_names(node)
        u = used_names(node)
        bad = sorted((k, v) for k, v in u.items() if k not in d)
        for name, line in bad:
            say('  [未定义属性] %s :: class %s  L%d  self.%s'
                % (path, node.name, line, name))
            n += 1
    if n == 0:
        say('  OK   %s' % path)
    return n


def main():
    args = [a for a in sys.argv[1:] if not a.startswith('-')]
    if not args:
        say('用法：python _lint_attrs.py <file.py> [...]')
        return 2
    total = 0
    for p in args:
        total += check(p)
    say('')
    say('未定义属性合计：%d 处' % total)
    return 1 if total else 0


if __name__ == '__main__':
    rc = main()
    with io.open('_lint_attrs_out.txt', 'w', encoding='utf-8') as f:
        f.write('\n'.join(out) + '\n')
    print('\n'.join(out))
    sys.exit(rc)
