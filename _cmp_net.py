# -*- coding: utf-8 -*-
"""核验：同一个"可见 5 天净"，三处算法是否一致。

  ① 真值表-v5.csv 的「前5天净」列
  ② 模拟广告数据-100广告10天-v5.csv 里 Σ购物转化价值 − Σ已花费金额
  ③ AI逐条结论-v11.csv 的 花费 与 ROAS 反推（花费×(ROAS−1)）

任何两处不一致 = 又是「同一件事两把尺子」，必须先修再谈优化。
"""
import csv
import io
import os
from collections import defaultdict

D = os.path.dirname(os.path.abspath(__file__))
P_TRUTH = os.path.join(D, '真值表-v5.csv')
P_DATA = os.path.join(D, '模拟广告数据-100广告10天-v5.csv')
P_AI = os.path.join(D, 'AI逐条结论-v11.csv')
P_OUT = os.path.join(D, '_cmp_net_out.txt')

L = []


def say(s=''):
    L.append(s)


def num(x):
    try:
        return float(str(x).strip())
    except Exception:
        return 0.0


t1 = {}
with io.open(P_TRUTH, encoding='utf-8-sig') as f:
    for r in csv.DictReader(f):
        t1[r['广告']] = r

day = defaultdict(list)
with io.open(P_DATA, encoding='utf-8-sig') as f:
    rd = csv.DictReader(f)
    cols = rd.fieldnames
    for r in rd:
        day[r['广告系列名称']].append(r)

ai = {}
with io.open(P_AI, encoding='utf-8-sig') as f:
    for r in csv.DictReader(f):
        ai[r['对象']] = r

say('模拟数据列名：%s' % ' | '.join(cols))
say()

names = [n for n in t1 if n in day and n in ai]
say('三处都有: %d 条' % len(names))

bad = []
for n in names:
    n1 = num(t1[n]['前5天净'])
    rows = day[n]
    n2 = sum(num(r['购物转化价值']) for r in rows) - sum(num(r['已花费金额 (USD)']) for r in rows)
    sp = num(ai[n]['花费'])
    n3 = sp * (num(ai[n]['ROAS']) - 1.0)
    if abs(n1 - n2) > 0.02:
        bad.append((n, n1, n2, n3))

say()
say('①真值表 vs ②逐日聚合 不一致的条数：%d' % len(bad))
for n, n1, n2, n3 in bad[:20]:
    say('  %-46s 真值表 %+8.2f ｜ 逐日 %+8.2f ｜ 差 %+.2f' % (n[:46], n1, n2, n2 - n1))

c1 = sum(1 for n in names if num(t1[n]['前5天净']) <= 0)
c2 = sum(1 for n in names if (sum(num(r['购物转化价值']) for r in day[n])
                              - sum(num(r['已花费金额 (USD)']) for r in day[n])) <= 0)
say()
say('「净 <= 0」的条数：真值表口径 %d ｜ 逐日聚合口径 %d' % (c1, c2))

stop_thresh = sum(1 for n in names
                  if num(t1[n]['前5天净']) < -max(5.0, 0.06 * num(t1[n]['总花费'])))
say('真值「暂停」的原始阈值口径（净 < -max(5, 6%%花费)）报出：%d 条' % stop_thresh)
say('真值表里实际标「暂停」：%d 条' % sum(1 for n in names if t1[n]['正确动作'] == '暂停'))

say()
say('净在 (-阈值, 0] 之间的（小亏但没超线）逐条：')
for n in names:
    n1 = num(t1[n]['前5天净'])
    lim = -max(5.0, 0.06 * num(t1[n]['总花费']))
    if lim < n1 <= 0:
        say('  %-46s 净 %+7.2f ｜ 阈值 %+7.2f ｜ 真值=%s' % (n[:46], n1, lim, t1[n]['正确动作']))

with io.open(P_OUT, 'w', encoding='utf-8') as f:
    f.write('\n'.join(L))
