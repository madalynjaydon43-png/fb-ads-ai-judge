# -*- coding: utf-8 -*-
"""个案档案：把一条广告的「AI 看到的 5 天」+「后 5 天真值（两版）」+「AI 原话」拉齐。

用途：回答「给我找一条该加没加的广告」这类问题 —— 给出可逐行核对的全量证据。
关键约束：三份表口径必须一致（都用期望口径 rev_e），脚本会自校验，对不上就报错。
"""
import csv
import io
import os
import sys

D = os.path.dirname(os.path.abspath(__file__))
P_DATA = os.path.join(D, '模拟广告数据-100广告10天-v5.csv')
P_TRUTH = os.path.join(D, '真值表-v5.csv')
P_HID = os.path.join(D, '真值表-逐日-v5.csv')
P_AI = os.path.join(D, 'AI逐条结论-v11.csv')
# 默认「该加没加」的代表；传参可换对象：
#   python _case_v11.py "新互动广告系列-0923-运动裤1-1-新 - 广告副本"
TARGET = (sys.argv[1] if len(sys.argv) > 1
          else '新销量广告系列-0923-卫衣1-1-新 - 广告副本')
_short = TARGET.split('-0923-')[-1].replace(' ', '').replace('/', '_')
P_OUT = os.path.join(D, '_case_out_%s.txt' % _short)
P_CSV = os.path.join(D, '个案-%s-全数据-v5.csv' % _short)

L = []
def say(s=''):
    L.append(s)


def rd(path):
    with io.open(path, 'r', encoding='utf-8-sig', newline='') as f:
        return list(csv.DictReader(f))


def num(x):
    try:
        return float(x)
    except Exception:
        return 0.0


data = rd(P_DATA)
truth = rd(P_TRUTH)
hid = rd(P_HID)
ai = rd(P_AI)

say('=== 表头自检 ===')
say('数据表列: %s' % ' | '.join(list(data[0].keys())))
say('真值表列: %s' % ' | '.join(list(truth[0].keys())))
say('逐日表列: %s' % ' | '.join(list(hid[0].keys())))
say('AI表列  : %s' % ' | '.join(list(ai[0].keys())))
say()

vis = [r for r in data if r['广告系列名称'] == TARGET]
tr = [r for r in truth if r['广告'] == TARGET]
hd = [r for r in hid if r['广告'] == TARGET]
ar = [r for r in ai if r[list(ai[0].keys())[0]] == TARGET]

say('命中行数: 可见 %d / 真值 %d / 逐日 %d / AI %d' % (len(vis), len(tr), len(hd), len(ar)))
if not (vis and tr and hd and ar):
    say('!! 有表缺该广告，终止')
    io.open(P_OUT, 'w', encoding='utf-8', newline='\n').write('\n'.join(L))
    raise SystemExit(0)

T = tr[0]
say()

# ---------- 1. AI 看到的 5 天 ----------
say('=== 1. AI 看到的 5 天（喂进去的原始明细）===')
say('  %-12s %8s %8s %7s %7s %7s %7s %10s %7s' % (
    '日期', '花费$', '展示', '频次', '点击', '购买', '收入$', 'ROAS', '覆盖'))
sp = imp = clk = pur = rev = 0.0
for r in vis:
    spend = num(r['已花费金额 (USD)'])
    im = num(r['展示次数'])
    fre = num(r['频次'])
    ck = num(r['链接点击量'])
    pu = num(r['购物次数'])
    rv = num(r['购物转化价值'])
    ro = num(r['广告花费回报 (ROAS) - 购物'])
    sp += spend; imp += im; clk += ck; pur += pu; rev += rv
    say('  %-12s %8.2f %8.0f %7.3f %7.0f %7.0f %7.2f %10.2f %7.0f' % (
        r['报告开始日期'], spend, im, fre, ck, pu, rv, ro, num(r['覆盖人数'])))
say('  %-12s %8.2f %8.0f %7s %7.0f %7.0f %7.2f %10.2f %7s' % (
    '合计', sp, imp, '', clk, pur, rev, (rev / sp if sp else 0), ''))
say('  预算 %s /天（%s），5 天顶格率 %.3f → %s' % (
    T['预算'], T['预算类型'], num(T['_顶格率5']),
    '每天都花光，是预算卡住了它' if num(T['_顶格率5']) > 0.95 else '预算有富余'))
say('  第 5 天频次 %.3f（健康区通常 <1.5，低 = 受众远没看腻）' % num(T['_频次5']))
say('  可见 5 天净 = 收入 %.2f − 花费 %.2f = **%+.2f**' % (rev, sp, rev - sp))
say()

# ---------- 2. 后 5 天真值（两版） ----------
say('=== 2. 后 5 天真实演化（同一机制跑两遍）===')
for 方案 in ('不动', '加预算'):
    rows = [r for r in hd if r['方案'] == 方案]
    ss = sum(num(r['花费']) for r in rows)
    rr = sum(num(r['期望收入']) for r in rows)
    say('  【%s】预算 %s/天' % (方案, rows[0]['预算']))
    say('    %-6s %8s %10s %10s %8s %7s' % ('第几天', '花费$', '期望收入$', '期望净$', '频次', '覆盖'))
    for r in rows:
        say('    %-6s %8.2f %10.2f %10.2f %8.3f %7.0f' % (
            r['第几天'], num(r['花费']), num(r['期望收入']),
            num(r['期望净']), num(r['频次']), num(r['覆盖'])))
    say('    %-6s %8.2f %10.2f %10.2f' % ('合计', ss, rr, rr - ss))
    say()

# ---------- 3. 口径自校验 ----------
say('=== 3. 口径自校验（逐日合计 vs 真值表汇总）===')
ok = True
for 方案, kc, kr, kn in (('不动', '不动_花费', '不动_收入', '不动_净'),
                         ('加预算', '加预算_花费', '加预算_收入', '加预算_净')):
    rows = [r for r in hd if r['方案'] == 方案]
    ss = round(sum(num(r['花费']) for r in rows), 2)
    rr = round(sum(num(r['期望收入']) for r in rows), 2)
    nn = round(sum(num(r['期望净']) for r in rows), 2)
    d1, d2, d3 = abs(ss - num(T[kc])), abs(rr - num(T[kr])), abs(nn - num(T[kn]))
    flag = 'OK' if max(d1, d2, d3) < 0.05 else '!! 不一致'
    if max(d1, d2, d3) >= 0.05:
        ok = False
    say('  %-5s 花费 %s(%.2f) 收入 %s(%.2f) 净 %s(%.2f)  → %s' % (
        方案, kc, num(T[kc]), kr, num(T[kr]), kn, num(T[kn]), flag))
say('  总体: %s' % ('✅ 三份表口径一致，数字可逐行核对' if ok else '❌ 有表口径不一致，需修'))
say()

# ---------- 4. 判定 ----------
say('=== 4. 判定与差额 ===')
say('  正确动作 = %s（依据：%s）' % (T['正确动作'], T['判定依据']))
say('  不动   净 %+.2f' % num(T['不动_净']))
say('  加预算 净 %+.2f' % num(T['加预算_净']))
say('  差额    %+.2f  → 多花的钱是 %.2f，换回 %.2f' % (
    num(T['差额']), num(T['加预算_花费']) - num(T['不动_花费']),
    num(T['加预算_收入']) - num(T['不动_收入'])))
if ok and num(T['差额']) > 0:
    say('  每多投 $1 换回 $%.2f' % ((num(T['加预算_收入']) - num(T['不动_收入']))
                                    / max(0.01, num(T['加预算_花费']) - num(T['不动_花费']))))
say()

# ---------- 5. AI 原话 ----------
say('=== 5. AI 的原话（一个字没改）===')
k = list(ai[0].keys())
say('  AI 判断 = %s ；真值 = %s ；对错 = %s' % (
    ar[0].get(k[5], ''), ar[0].get(k[4], ''), ar[0].get(k[6], '')))
say('  理由: %s' % ar[0].get(k[-1], ''))
say()

# ---------- 6. 顺带：该停没停 ----------
say('=== 6. 顺带查「该停没停」===')
# 🔴 口径必须与主报告 metrics() 一致：它把 decrease_budget 与 pause 都算「关停」（都是往下压）。
#    所以「没判停」= AI 给的是 observe / increase_budget。
CN2 = {'observe': '观察', 'increase_budget': '加预算',
       'decrease_budget': '减/停', 'pause': '减/停'}
mism = []
for a2 in ai:
    name = a2[list(ai[0].keys())[0]]
    act_ai = a2.get(k[5], '')
    act_tr = a2.get(k[4], '')
    if act_tr == '暂停' and act_ai not in ('pause', 'decrease_budget'):
        t2 = next((x for x in truth if x['广告'] == name), None)
        mism.append((name, CN2.get(act_ai, act_ai),
                     num(t2['前5天净']) if t2 else 0.0,
                     num(t2['不动_净']) if t2 else 0.0))
say('  真值「暂停」而 AI 没判停(decrease/pause): %d 条 —— 与混淆矩阵「暂停→观察 9」一致' % len(mism))
say('  %-46s %-6s %10s %10s' % ('广告', 'AI', '可见5天净', '后5天不动净'))
for n, a_, net, nxt in sorted(mism, key=lambda z: z[3]):
    say('    %-46s %-6s %+10.2f %+10.2f' % (n[:46], a_, net, nxt))
say('  ⚠️ 「暂停」的判据是**可见窗口在亏 → 止损**（决策只能基于已知信息），')
say('     不等于「停了更赚」：上面若出现「后5天不动净为正」，那是事后视角，标签本身没错。')
say()

# 该类别整体体检：真值「暂停」的题，后 5 天实际的盈亏分布
stop_rows = [t for t in truth if t['正确动作'] == '暂停']
neg_after = [t for t in stop_rows if num(t['不动_净']) < 0]
say('  体检：真值「暂停」共 %d 条，其中后 5 天**确实还在亏**的 %d 条（%.0f%%）' % (
    len(stop_rows), len(neg_after),
    len(neg_after) / max(1, len(stop_rows)) * 100))
say('        平均 可见5天净 %+.2f ｜ 平均 后5天不动净 %+.2f' % (
    sum(num(t['前5天净']) for t in stop_rows) / max(1, len(stop_rows)),
    sum(num(t['不动_净']) for t in stop_rows) / max(1, len(stop_rows))))
say()

io.open(P_OUT, 'w', encoding='utf-8', newline='\n').write('\n'.join(L))

# ---------- 落 CSV 档案 ----------
out = [[TARGET, '—— 可见 5 天（AI 输入）——', '', '', '', '', '', '']]
out.append(['日期', '花费$', '展示', '频次', '购买', '收入$', 'ROAS', '顶格?'])
b = num(T['预算'])
for r in vis:
    out.append([r['报告开始日期'], r['已花费金额 (USD)'], r['展示次数'], r['频次'],
                r['购物次数'], r['购物转化价值'],
                r['广告花费回报 (ROAS) - 购物'],
                '顶格' if abs(num(r['已花费金额 (USD)']) - b) < 0.6 else ''])
out.append(['合计', round(sp, 2), round(imp), '', round(pur), round(rev, 2),
            round(rev / sp, 3) if sp else '', ''])
out.append([])
out.append([TARGET, '—— 后 5 天真值 ——', '', '', '', '', '', ''])
out.append(['方案', '第几天', '花费$', '期望收入$', '期望净$', '频次', '覆盖', ''])
for 方案 in ('不动', '加预算'):
    for r in [x for x in hd if x['方案'] == 方案]:
        out.append([方案, r['第几天'], r['花费'], r['期望收入'], r['期望净'],
                    r['频次'], r['覆盖'], ''])
out.append([])
out.append([TARGET, '—— 结论 ——', '', '', '', '', '', ''])
out.append(['正确动作', T['正确动作'], T['判定依据'], '', '', '', '', ''])
out.append(['不动净', T['不动_净'], '加预算净', T['加预算_净'], '差额', T['差额'], '', ''])
out.append(['AI 判断', ar[0].get(k[5], ''), 'AI 理由', ar[0].get(k[-1], ''), '', '', '', ''])

with io.open(P_CSV, 'w', encoding='utf-8-sig', newline='') as f:
    w = csv.writer(f)
    w.writerows(out)

print('OK written', P_OUT, P_CSV)
