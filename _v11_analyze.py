# -*- coding: utf-8 -*-
"""_v11_analyze.py —— 对 v11（v5 真实版数据）结果做混淆矩阵与归因。

回答一个问题：**换成频次真实的数据后，AI「不敢加预算」这个病还在不在？病因变了吗？**
输出：_v11_analyze_out.txt
"""
import csv
import io
import os
from collections import Counter, defaultdict

D = os.path.dirname(os.path.abspath(__file__))
P_TRUTH = os.path.join(D, '真值表-v5.csv')
P_SUG = os.path.join(D, 'AI逐条结论-v11.csv')
P_SUG_NOTE = os.path.join(D, 'AI逐条结论-v11_note.csv')
P_DATA = os.path.join(D, '模拟广告数据-100广告10天-v5.csv')

L = []
def say(s=''):
    L.append(s)


def f(x):
    try:
        return float(x or 0)
    except Exception:
        return 0.0


def load_sug(p):
    if not os.path.exists(p):
        return None
    out = {}
    for r in csv.DictReader(io.StringIO(open(p, encoding='utf-8-sig').read())):
        a = r['AI动作']
        out[r['对象']] = '暂停' if a in ('pause', 'decrease_budget') else (
            '加预算' if a == 'increase_budget' else '观察')
    return out


truth = {r['广告']: r for r in csv.DictReader(io.StringIO(open(P_TRUTH, encoding='utf-8-sig').read()))}
rows = list(csv.DictReader(io.StringIO(open(P_DATA, encoding='utf-8-sig').read())))

# 每条的可见特征
vis = defaultdict(dict)
for r in rows:
    vis[r['广告系列名称']][r['报告开始日期']] = r


def visfeat(name):
    d = vis[name]
    ds = sorted(d)
    r1, r5 = d[ds[0]], d[ds[-1]]
    reach1, reach5 = f(r1['覆盖人数']), f(r5['覆盖人数'])
    return dict(
        顶格率=sum(f(d[k]['已花费金额 (USD)']) for k in ds) / max(1e-9, f(r1['广告组预算']) * len(ds)),
        频次首=f(r1['频次']), 频次末=f(r5['频次']),
        频次增幅=f(r5['频次']) - f(r1['频次']),
        覆盖斜率=(reach5 - reach1) / max(1.0, reach1) * 100,
        ROAS=sum(f(d[k]['购物转化价值']) for k in ds) / max(1e-9, sum(f(d[k]['已花费金额 (USD)']) for k in ds)),
    )


run = load_sug(P_SUG)
run_note = load_sug(P_SUG_NOTE)

say('=== v5（受众池真实版）· AI 混淆矩阵 ===')
say()
cls = ['加预算', '观察', '暂停']
hdr = '真值\\AI    ' + ''.join('%9s' % c for c in cls) + '%9s' % '合计'
say(hdr)
say('-' * len(hdr))
for t in cls:
    line = '%-10s' % t
    tot = 0
    for a in cls:
        n = sum(1 for k in truth if truth[k]['正确动作'] == t and run.get(k) == a)
        tot += n
        line += '%9d' % n
    line += '%9d' % tot
    say(line)
say('-' * len(hdr))
line = '%-10s' % '合计'
for a in cls:
    line += '%9d' % sum(1 for k in truth if run.get(k) == a)
say(line + '%9d' % len(truth))
say()

acc = sum(1 for k in truth if truth[k]['正确动作'] == run.get(k))
n_up = sum(1 for k in truth if truth[k]['正确动作'] == '加预算')
n_hold = sum(1 for k in truth if truth[k]['正确动作'] == '观察')
n_kill = sum(1 for k in truth if truth[k]['正确动作'] == '暂停')
say('  准确率            %d/%d = %.1f%%   （基线：恒判暂停 35%%、恒判加预算 35%%、恒判观察 30%%、模型天花板 79%%）'
    % (acc, len(truth), acc / len(truth) * 100))
for c in cls:
    tp = sum(1 for k in truth if truth[k]['正确动作'] == c and run.get(k) == c)
    say('  %-6s 召回 %5.1f%%  (%d/%d)' % (c, tp / max(1, sum(1 for k in truth if truth[k]['正确动作'] == c)) * 100,
                                          tp, sum(1 for k in truth if truth[k]['正确动作'] == c)))
say()
say('  加预算 误报（AI 说加、真值不该加）: %d 条' % sum(
    1 for k in truth if run.get(k) == '加预算' and truth[k]['正确动作'] != '加预算'))
say('  暂停   误杀（AI 说停、真值该加）  : %d 条，这些条真值差额合计 +$%.0f' % (
    sum(1 for k in truth if run.get(k) == '暂停' and truth[k]['正确动作'] == '加预算'),
    sum(f(truth[k]['差额']) for k in truth if run.get(k) == '暂停' and truth[k]['正确动作'] == '加预算')))
say()

# ---------- 归因：该加的为什么没加 ----------
miss_up = [k for k in truth if truth[k]['正确动作'] == '加预算' and run.get(k) != '加预算']
hit_up = [k for k in truth if truth[k]['正确动作'] == '加预算' and run.get(k) == '加预算']


def med(xs):
    xs = sorted(xs)
    return xs[len(xs) // 2] if xs else 0


say('=== 归因一：真值「加预算」的两组，可见信号差在哪 ===')
say('  %-22s %10s %10s' % ('', '没加(%d条)' % len(miss_up), '加对(%d条)' % len(hit_up)))
for key in ('顶格率', '频次首', '频次末', '频次增幅', '覆盖斜率', 'ROAS'):
    a = med([visfeat(k)[key] for k in miss_up])
    b = med([visfeat(k)[key] for k in hit_up])
    say('  %-22s %10.3f %10.3f' % (key, a, b))
say('  %-22s %10d %10d' % ('隐藏受众池 中位', med([int(f(truth[k]['_受众池'])) for k in miss_up]),
                            med([int(f(truth[k]['_受众池'])) for k in hit_up])))
say('  %-22s %10.3f %10.3f' % ('隐藏 alpha 中位(越小越好)',
                                med([f(truth[k]['_放量掉转化alpha']) for k in miss_up]),
                                med([f(truth[k]['_放量掉转化alpha']) for k in hit_up])))
say('  %-22s %10.1f %10.1f' % ('真值差额中位 $',
                                med([f(truth[k]['差额']) for k in miss_up]),
                                med([f(truth[k]['差额']) for k in hit_up])))
say()

# ---------- 归因二：AI 的理由里提没提频次 ----------
say('=== 归因二：AI 判断时到底看什么词（出现频次）===')
reasons = []
for k in truth:
    if run.get(k):
        reasons.append((k, truth[k]['正确动作'], run[k]))
kw = ['频次', '覆盖', '饱和度', '受众', 'ROAS', 'CTR', '顶格', '花费', '加购', '结账', '支付', '购买']
sugraw = {r['对象']: r['理由'] for r in csv.DictReader(
    io.StringIO(open(P_SUG, encoding='utf-8-sig').read()))}
c = Counter()
for k, t, a in reasons:
    txt = sugraw.get(k, '')
    for w in kw:
        if w in txt:
            c[w] += 1
for w in kw:
    say('  提到「%s」: %d / %d 条' % (w, c[w], len(reasons)))
say()

say('=== 归因三：该加却没加 —— 前 12 条明细 ===')
say('  %-34s %6s %7s %7s %7s %7s %8s %8s' % ('广告', 'AI判', '顶格率', '频次首', '频次末', '覆盖斜率', 'ROAS', '差额$'))
for k in sorted(miss_up, key=lambda z: -f(truth[z]['差额']))[:12]:
    v = visfeat(k)
    say('  %-34s %6s %7.2f %7.2f %7.2f %7.1f%% %8.2f %8.1f' % (
        k[:34], run[k], v['顶格率'], v['频次首'], v['频次末'], v['覆盖斜率'], v['ROAS'], f(truth[k]['差额'])))
say()

if run_note is not None:
    acc2 = sum(1 for k in truth if truth[k]['正确动作'] == run_note.get(k))
    up2 = sum(1 for k in truth if truth[k]['正确动作'] == '加预算' and run_note.get(k) == '加预算')
    say('=== 对照：带判断指引那一轮 ===')
    say('  准确率 %.1f%%（裸奔 %.1f%%） / 加预算召回 %.1f%%（裸奔 %.1f%%）' % (
        acc2 / len(truth) * 100, acc / len(truth) * 100,
        up2 / max(1, n_up) * 100, len(hit_up) / max(1, n_up) * 100))

with io.open(os.path.join(D, '_v11_analyze_out.txt'), 'w', encoding='utf-8') as fp:
    fp.write('\n'.join(L))
print('\n'.join(L))
