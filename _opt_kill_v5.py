# -*- coding: utf-8 -*-
"""该关没关（真值=暂停 但 AI 判 observe）的归因 + 补救判据实测。

只读三份已有产物，不碰生成器、不重跑 AI：
  真值表-v5.csv        ← 真值（正确动作 + 可见窗口汇总）
  AI逐条结论-v11.csv   ← AI 的动作与理由原文
"""
import csv
import io
import os
import statistics

D = os.path.dirname(os.path.abspath(__file__))
P_TRUTH = os.path.join(D, '真值表-v5.csv')
P_AI = os.path.join(D, 'AI逐条结论-v11.csv')
P_OUT = os.path.join(D, '_opt_kill_out.txt')

_out = []


def say(s=''):
    _out.append(s)
    print(s)


def num(x):
    try:
        return float(x)
    except Exception:
        return 0.0


def load(path, key):
    d = {}
    with io.open(path, encoding='utf-8-sig') as f:
        for r in csv.DictReader(f):
            d[r[key]] = r
    return d


truth = load(P_TRUTH, '广告')
ai = load(P_AI, '对象')
names = [n for n in truth if n in ai]
say('配对 %d 条' % len(names))

CN = {'pause': '暂停', 'decrease_budget': '暂停',
      'observe': '观察', 'increase_budget': '加预算'}


def net(t):
    return num(t['前5天净'])


def spend(t):
    return num(t['总花费'])


def rev(t):
    return num(t['总收入'])


def roas(t):
    return rev(t) / spend(t) if spend(t) else 0.0


def pur(t):
    return num(t['总购买'])


def fill(t):
    return num(t['_顶格率5'])


def freq(t):
    return num(t['_频次5'])


def slope(t):
    a, b = num(t['_覆盖首日']), num(t['_覆盖末日'])
    return (b - a) / a if a else 0.0


STOPPED = ('pause', 'decrease_budget')
ok = [n for n in names if truth[n]['正确动作'] == '暂停' and ai[n]['AI动作'] in STOPPED]
miss = [n for n in names if truth[n]['正确动作'] == '暂停' and ai[n]['AI动作'] not in STOPPED]

say()
say('真值「暂停」共 %d 条 —— AI 判停 %d 条 ✓ ／ 没判停 %d 条 ✗'
    % (len(ok) + len(miss), len(ok), len(miss)))

FEATS = [('可见5天净', net), ('5天花费', spend), ('5天收入', rev), ('ROAS', roas),
         ('购买数', pur), ('预算顶格率', fill), ('频次', freq), ('覆盖斜率', slope)]

say()
say('=== 一、可见特征中位数：判停成功的 vs 没判停的 ===')
say('  %-14s %14s %14s' % ('特征', 'AI判停(%d条)' % len(ok), '没判停(%d条)' % len(miss)))
say('  ' + '-' * 46)
for label, fn in FEATS:
    a = statistics.median([fn(truth[n]) for n in ok]) if ok else 0.0
    b = statistics.median([fn(truth[n]) for n in miss]) if miss else 0.0
    flag = '  ← 分不开' if abs(a - b) < 0.05 * max(1.0, abs(a)) else ''
    say('  %-14s %14.2f %14.2f%s' % (label, a, b, flag))

say()
say('=== 二、9 条漏杀的逐条画像 + AI 原话 ===')
for n in sorted(miss, key=lambda x: net(truth[x])):
    t = truth[n]
    say('  %s' % n)
    say('    花费 %.2f ｜ 收入 %.2f ｜ 净 %+.2f ｜ ROAS %.2f ｜ 购买 %d ｜ 顶格 %.2f ｜ 频次 %.2f'
        % (spend(t), rev(t), net(t), roas(t), pur(t), fill(t), freq(t)))
    say('    AI：%s' % ai[n]['理由'].replace('\n', ' ')[:150])


def evaluate(label, rule):
    pred = {}
    for n in names:
        r = rule(truth[n])
        pred[n] = r if r else CN.get(ai[n]['AI动作'], ai[n]['AI动作'])
    hit = sum(1 for n in names if pred[n] == truth[n]['正确动作'])
    tp = [n for n in names if pred[n] == '暂停' and truth[n]['正确动作'] == '暂停']
    fp = [n for n in names if pred[n] == '暂停' and truth[n]['正确动作'] != '暂停']
    fn = [n for n in names if pred[n] != '暂停' and truth[n]['正确动作'] == '暂停']
    say()
    say('  %s' % label)
    say('    总分 %d/%d = %.1f%%' % (hit, len(names), hit / len(names) * 100))
    say('    「停」报出 %d 条 → 真该停 %d（精度 %.0f%%）｜漏 %d 条（召回 %.0f%%）'
        % (len(tp) + len(fp), len(tp),
           len(tp) / (len(tp) + len(fp)) * 100 if tp or fp else 0,
           len(fn), len(tp) / (len(tp) + len(fn)) * 100 if tp or fn else 0))
    if fp:
        say('    ⚠️ 多停的 %d 条：%s' % (len(fp), ', '.join(fp)))


say()
say('=== 三、补救判据实测（全库 %d 条，只动「停」这一刀） ===' % len(names))
evaluate('A. 现状：不动（纯 AI）', lambda t: None)
evaluate('B. 硬线：可见5天净 <= 0 → 停', lambda t: '暂停' if net(t) <= 0 else None)
evaluate('C. 硬线：可见5天净 <= 0 且 花费 >= 30 → 停',
         lambda t: '暂停' if (net(t) <= 0 and spend(t) >= 30) else None)
evaluate('D. 硬线：ROAS < 1 → 停', lambda t: '暂停' if roas(t) < 1 else None)
evaluate('E. 硬线：可见5天净 <= -5（留缓冲）→ 停', lambda t: '暂停' if net(t) <= -5 else None)
evaluate('F. 硬线：可见5天净 < 0 且 (ROAS < 1 或 购买 <= 1) → 停',
         lambda t: '暂停' if (net(t) < 0 and (roas(t) < 1 or pur(t) <= 1)) else None)

with io.open(P_OUT, 'w', encoding='utf-8') as f:
    f.write('\n'.join(_out))
