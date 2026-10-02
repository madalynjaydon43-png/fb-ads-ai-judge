# -*- coding: utf-8 -*-
"""_roll_case.py —— 滚动决策视角：回答用户的两个质疑

用户 2026-10-02 原话：
  ① 「近三天1单ROAS 1.41，是我也不加了。如果加就该在24号出单立即加，
      后面27跑完不加正常，你是怎么觉得这个应该加？」
  ② 「内衣这个三天没出单花了60刀还没停？？？是我第二天中午就停了」

要做三件事：
  A. 把这两条广告的**逐日时点演化**摊开（只含当时可见的累计数据）
     —— 看"该加/该停"的信号到底哪一天出现的。
  B. 检验用户的止损纪律：全库里「花钱没出单」的时点，往后 3 天到底是不是还在亏？
     —— 他的纪律如果在大样本上成立，那他就是对的，单条"错过一单"不算错。
  C. 记录滚动视角与窗口真值的差异。
"""
import csv
import io
import os

D = os.path.dirname(os.path.abspath(__file__))
P_ROLL = os.path.join(D, '滚动决策真值-v5.csv')
P_TRUTH = os.path.join(D, '真值表-v5.csv')
P_OUT = os.path.join(D, '_roll_case_out.txt')

L = []


def say(s=''):
    L.append(s)


def rd(p):
    with io.open(p, encoding='utf-8-sig', newline='') as f:
        return list(csv.DictReader(f))


def num(x):
    try:
        return float(x)
    except Exception:
        return 0.0


roll = rd(P_ROLL)
truth = {r['广告']: r for r in rd(P_TRUTH)}
byname = {}
for r in roll:
    byname.setdefault(r['广告'], []).append(r)

say('=== A. 卫衣1-1-新 - 广告副本 ===')
say('   （窗口真值判「加预算」、AI 判「观察」，你质疑"27号凭什么说该加"）')
say()
say('   决策日   累计花费   累计收入    累计净  累计单  末日频次  顶格率 | 往后3天: 不动净   加预算净 | 滚动应做')
NAME_A = '新销量广告系列-0923-卫衣1-1-新 - 广告副本'
for r in byname.get(NAME_A, []):
    say('   第%d天  %9.2f  %9.2f  %8.2f  %5s  %8.3f  %6.3f | %9.2f  %9.2f | %s'
        % (int(r['决策日']), num(r['累计花费']), num(r['累计收入']), num(r['累计净']),
           r['累计购买'], num(r['末日频次']), num(r['顶格率']),
           num(r['后3天_不动净']), num(r['后3天_加预算净']), r['正确动作']))
t = truth.get(NAME_A, {})
say('   · 窗口真值（第5天收盘往后看5天）: %s  不动净 %s / 加预算净 %s / 差额 %s'
    % (t.get('正确动作'), t.get('不动_净'), t.get('加预算_净'), t.get('差额')))
say('   · 可见5天实际: 花费 %s / 收入 %s / 购买 %s / 净 %s'
    % (t.get('总花费'), t.get('总收入'), t.get('总购买'), t.get('前5天净')))
say()

say('=== B. 内衣2-1-新 ===')
say('   （你说"第二天中午就停了"，AI 到第 5 天还说"观察"）')
say()
say('   决策日   累计花费   累计收入    累计净  累计单  末日频次  顶格率 | 往后3天: 不动净   加预算净 | 滚动应做')
NAME_B = '新互动广告系列-0923-内衣2-1-新'
for r in byname.get(NAME_B, []):
    say('   第%d天  %9.2f  %9.2f  %8.2f  %5s  %8.3f  %6.3f | %9.2f  %9.2f | %s'
        % (int(r['决策日']), num(r['累计花费']), num(r['累计收入']), num(r['累计净']),
           r['累计购买'], num(r['末日频次']), num(r['顶格率']),
           num(r['后3天_不动净']), num(r['后3天_加预算净']), r['正确动作']))
t = truth.get(NAME_B, {})
say('   · 窗口真值: %s  不动净 %s / 加预算净 %s / 差额 %s'
    % (t.get('正确动作'), t.get('不动_净'), t.get('加预算_净'), t.get('差额')))
say()

say('=== C. 检验你的止损纪律：全库「零单且在亏」的时点，往后 3 天还在亏吗？===')
say()
for cond, label in ((1, '第 1 天收盘即「0 单且在亏」'),
                    (2, '跑到第 2 天收盘「0 单且在亏」'),
                    (3, '跑到第 3 天收盘「0 单且在亏」')):
    sub = [r for r in roll if int(r['决策日']) == cond
           and int(r['累计购买']) == 0 and num(r['累计净']) < 0]
    if not sub:
        continue
    worse = [r for r in sub if num(r['后3天_不动净']) < 0]
    say('  %s: %d 条' % (label, len(sub)))
    say('     往后 3 天**继续亏钱**的: %d 条 (%.0f%%)  ← 停了就避免掉的'
        % (len(worse), len(worse) / len(sub) * 100))
    say('     往后 3 天翻正的:         %d 条 (%.0f%%)  ← 停了会错过的'
        % (len(sub) - len(worse), (len(sub) - len(worse)) / len(sub) * 100))
    avg = sum(num(r['后3天_不动净']) for r in sub) / len(sub)
    say('     平均往后 3 天净收益: %+.2f 美元（负 = 停掉平均是对的）' % avg)
    say()

say('=== D. 「该加」信号到底哪一天出现（窗口真值判「加预算」的 35 条）===')
say()
ups = [n for n, t in truth.items() if t.get('正确动作') == '加预算']
first_up = {}
for n in ups:
    ds = [int(r['决策日']) for r in byname.get(n, []) if r['正确动作'] == '加预算']
    first_up[n] = min(ds) if ds else None
from collections import Counter
c = Counter(v for v in first_up.values() if v)
say('   最早判「加」的决策日分布: ' + ' / '.join('第%d天 %d条' % (k, c[k]) for k in sorted(c)))
say('   → 等到第 5 天才看，有 %d/%d 条的加仓窗口已经错过'
    % (sum(1 for v in first_up.values() if v and v < 5), len(ups)))
say()

say('=== E. 滚动视角 vs 窗口真值：动作一致率 ===')
say()
agree = 0
tot = 0
for n, t in truth.items():
    rs = byname.get(n, [])
    if not rs:
        continue
    tot += 1
    if rs[-1]['正确动作'] == t.get('正确动作'):
        agree += 1
say('   用第 5 天收盘的滚动判断 对比 窗口真值（同一个决策日，但观察窗 3 天 vs 5 天）:')
say('     一致 %d / %d = %.0f%%' % (agree, tot, agree / tot * 100))
say('     差异来源：观察窗长度不同（3 天 vs 5 天），不是算法不同。')
say()

with io.open(P_OUT, 'w', encoding='utf-8') as f:
    f.write('\n'.join(L))
print('\n'.join(L))
