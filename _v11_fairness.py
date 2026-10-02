# -*- coding: utf-8 -*-
"""_v11_fairness.py —— 检查「标签是否公平」：真值有没有跟可见数据明显对着干？

背景：v5 采用双口径
  · CSV 报告口径 = 整数（购物次数是整数，不会出现「0 单却有收入」）
  · 判卷口径     = 期望值（判断「该不该加钱」判的是期望效果）
副作用可能是：某条广告在前 5 天**实际**一单没出（可见收入 $0），
但**期望**上它是赚的 ⇒ 真值给「加预算」，而任何正常人都该说「暂停」。
这种题对 AI 不公平，必须数出来并公开。

输出：_v11_fairness_out.txt
"""
import csv
import io
import os

D = os.path.dirname(os.path.abspath(__file__))
P_TRUTH = os.path.join(D, '真值表-v5.csv')
P_DATA = os.path.join(D, '模拟广告数据-100广告10天-v5.csv')

L = []
def say(s=''):
    L.append(s)


def f(x):
    try:
        return float(x or 0)
    except Exception:
        return 0.0


truth = list(csv.DictReader(io.StringIO(open(P_TRUTH, encoding='utf-8-sig').read())))
rows = list(csv.DictReader(io.StringIO(open(P_DATA, encoding='utf-8-sig').read())))

real = {}
for r in rows:
    k = r['广告系列名称']
    d = real.setdefault(k, dict(spend=0.0, rev=0.0, pur=0))
    d['spend'] += f(r['已花费金额 (USD)'])
    d['rev'] += f(r['购物转化价值'])
    d['pur'] += int(f(r['购物次数']))

say('=== 标签公平性检查（%d 条）===' % len(truth))
say()

# 1. 真值=加预算，但可见 5 天收入为 0（实际一单没出）
bad1 = [t for t in truth if t['正确动作'] == '加预算' and real[t['广告']]['rev'] <= 0.01]
say('1) 真值=「加预算」，但可见 5 天**收入为 0**（AI 说停/看都算罚错）: %d 条' % len(bad1))
for t in bad1:
    say('     %s（可见花费 $%.2f / 购买 %d 单）' % (t['广告'], real[t['广告']]['spend'], real[t['广告']]['pur']))
say()

# 2. 真值=暂停，但可见 5 天明显在赚
bad2 = [t for t in truth if t['正确动作'] == '暂停' and real[t['广告']]['rev'] - real[t['广告']]['spend'] > 0.15 * real[t['广告']]['spend']]
say('2) 真值=「暂停」，但可见 5 天**净利 > 花费 15%%**（明摆着在赚钱）: %d 条' % len(bad2))
for t in bad2:
    r = real[t['广告']]
    say('     %s（花费 $%.2f / 收入 $%.2f）' % (t['广告'], r['spend'], r['rev']))
say()

# 3. 真值=观察，但可见 5 天在大亏
bad3 = [t for t in truth if t['正确动作'] == '观察' and real[t['广告']]['rev'] - real[t['广告']]['spend'] < -0.5 * real[t['广告']]['spend']]
say('3) 真值=「观察」，但可见 5 天**净亏 > 花费 50%%**（明摆着该停）: %d 条' % len(bad3))
for t in bad3:
    r = real[t['广告']]
    say('     %s（花费 $%.2f / 收入 $%.2f）' % (t['广告'], r['spend'], r['rev']))
say()

say('=== 自检：可见数据有没有内部矛盾 ===')
say('  购物次数=0 但收入>0 的广告: %d' % sum(1 for v in real.values() if v['pur'] == 0 and v['rev'] > 0.01))
say('  购物次数>0 但收入=0 的广告: %d' % sum(1 for v in real.values() if v['pur'] > 0 and v['rev'] <= 0.01))
say()
n_bad = len(bad1) + len(bad2) + len(bad3)
say('标记为「对 AI 不公平」的题共 %d / %d 条（%.1f%%）' % (n_bad, len(truth), n_bad / len(truth) * 100))

with io.open(os.path.join(D, '_v11_fairness_out.txt'), 'w', encoding='utf-8') as fp:
    fp.write('\n'.join(L))
print('\n'.join(L))
