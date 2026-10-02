# -*- coding: utf-8 -*-
"""
把「后 5 天」摊开给用户核对，并检验用户的两条直觉：
  直觉1（卫衣）：连续 2 天零单 → 不加预算
  直觉2（内衣）：连续几天零单且在亏 → 及早停
关键是算清一件事：在每天只有几十个点击的量级下，「连续两天零单」到底是信号还是噪声。
"""
import csv
import io
import os
import math
from collections import defaultdict

D = os.path.dirname(os.path.abspath(__file__))
P_DATA = os.path.join(D, '模拟广告数据-100广告10天-v5.csv')
P_TRUTH = os.path.join(D, '真值表-v5.csv')
P_HID = os.path.join(D, '真值表-逐日-v5.csv')
P_OUT = os.path.join(D, '_userstrategy_out.txt')

L = []


def say(s=''):
    L.append(s)


def num(x, d=0.0):
    try:
        return float(str(x).strip())
    except Exception:
        return d


# ---------- 读三份表 ----------
days = defaultdict(dict)          # 广告 -> {日期: 行}
with io.open(P_DATA, encoding='utf-8-sig') as f:
    for r in csv.DictReader(f):
        days[r['广告系列名称']][r['报告开始日期']] = r

truth = {}
with io.open(P_TRUTH, encoding='utf-8-sig') as f:
    for r in csv.DictReader(f):
        truth[r['广告']] = r

hid = defaultdict(lambda: defaultdict(list))   # 广告 -> 方案 -> [行]
with io.open(P_HID, encoding='utf-8-sig') as f:
    for r in csv.DictReader(f):
        hid[r['广告']][r['方案']].append(r)


def vis_days(name):
    """返回可见 5 天按日期排序的行。"""
    ds = sorted(days[name].keys())
    return [days[name][d] for d in ds]


def row_of(r):
    """把一行原始数据翻译成人看得懂的指标。"""
    spend = num(r['已花费金额 (USD)'])
    rev = num(r['购物转化价值'])
    pur = num(r['购物次数'])
    clk = num(r['链接点击量'])
    return dict(date=r['报告开始日期'], spend=spend, imp=num(r['展示次数']),
                freq=num(r['频次']), clk=clk, atc=num(r['加入购物车次数']),
                ic=num(r['结账发起次数']), pur=pur, rev=rev,
                roas=(rev / spend if spend > 0 else 0.0),
                cpc=(spend / clk if clk > 0 else 0.0))


def zero_run_end(rows):
    """结尾连续零单天数。"""
    n = 0
    for r in reversed(rows):
        if num(r['购物次数']) == 0:
            n += 1
        else:
            break
    return n


def p_zero_two_days(rows):
    """
    自然概率：按这 5 天的整体转化率（购买/点击），
    连续两天零单本来就会发生的概率 = (1-p)^(2×日均点击)。
    概率越高 ⇒ 这个「零单」越说明不了问题。
    """
    clk = sum(num(r['链接点击量']) for r in rows)
    pur = sum(num(r['购物次数']) for r in rows)
    if clk <= 0:
        return None, None
    p = min(0.95, pur / clk)
    n2 = 2.0 * clk / len(rows)
    return p, (1.0 - p) ** n2


# ==================== A. 卫衣完整时间线 ====================
TARGET = '新销量广告系列-0923-卫衣1-1-新 - 广告副本'
say('=' * 78)
say('A. 个案时间线：%s' % TARGET)
say('=' * 78)
say()

v = vis_days(TARGET)
say('【喂给 AI 的 5 天】（它只能看到这些）')
say('  %-10s %8s %7s %6s %6s %6s %9s %7s' %
    ('日期', '花费', '展示', '频次', '点击', '购买', '收入', 'ROAS'))
for r in v:
    x = row_of(r)
    say('  %-10s %8.2f %7.0f %6.3f %6.0f %6.0f %9.2f %7.2f' %
        (x['date'], x['spend'], x['imp'], x['freq'], x['clk'], x['pur'], x['rev'], x['roas']))
sp5 = sum(row_of(r)['spend'] for r in v)
rv5 = sum(row_of(r)['rev'] for r in v)
pu5 = sum(row_of(r)['pur'] for r in v)
ck5 = sum(row_of(r)['clk'] for r in v)
say('  %-10s %8.2f %7s %6s %6.0f %6.0f %9.2f %7.2f' %
    ('合计', sp5, '', '', ck5, pu5, rv5, rv5 / sp5))
say('  可见 5 天净 = 收入 %.2f − 花费 %.2f = %+.2f' % (rv5, sp5, rv5 - sp5))
say()

T = truth[TARGET]
say('【你没看到的 5 天 · 逐日】')
say('  %-6s | %-28s | %-28s' % ('第几天', '走法一：什么都不动', '走法二：预算 +20%'))
say('  %-6s | %9s %9s %9s | %9s %9s %9s' %
    ('', '花费', '收入', '净', '花费', '收入', '净'))
A = {int(r['第几天']): r for r in hid[TARGET]['不动']}
B = {int(r['第几天']): r for r in hid[TARGET]['加预算']}
for d in sorted(A):
    a, b = A[d], B[d]
    say('  %-6d | %9.2f %9.2f %9.2f | %9.2f %9.2f %9.2f' %
        (d, num(a['花费']), num(a['期望收入']), num(a['期望净']),
         num(b['花费']), num(b['期望收入']), num(b['期望净'])))
say('  %-6s | %9.2f %9.2f %9.2f | %9.2f %9.2f %9.2f' %
    ('合计', num(T['不动_花费']), num(T['不动_收入']), num(T['不动_净']),
     num(T['加预算_花费']), num(T['加预算_收入']), num(T['加预算_净'])))
say('  加预算多赚 = %+.2f' % num(T['差额']))
say('  真值判定  = %s' % T['正确动作'])
say()

p_, p0 = p_zero_two_days(v)
say('【关键算一笔】这条广告连着两天（09-26、09-27）零单。')
say('  可见 5 天：%d 次点击 → %d 单，整体转化率 %.3f%%' % (ck5, pu5, p_ * 100))
say('  日均点击 %.1f → 两天共 %.0f 次点击' % (ck5 / 5, 2 * ck5 / 5))
say('  按这个转化率，出现「连续两天零单」的自然概率 = %.1f%%' % (p0 * 100))
say('  → %s' % ('也就是说：这种零单本来就常发生，它不能说明广告变差了。'
                if p0 >= 0.20 else '这个概率不高，零单更可能是真信号。'))
say()

# ==================== B. 全库检验「连续两天零单就不加」 ====================
say('=' * 78)
say('B. 全库检验：「连续 2 天零单 就不加预算」这条规则对不对')
say('=' * 78)
say()

n_all = len(truth)
zero2 = []           # 第5天收盘时，末2天零单
for name in truth:
    rows = vis_days(name)
    if len(rows) < 5:
        continue
    if zero_run_end(rows) >= 2:
        zero2.append(name)

grp = defaultdict(list)
for name in zero2:
    grp[truth[name]['正确动作']].append(name)

say('  全库 %d 条，第 5 天收盘时「末 2 天零单」的有 %d 条：' % (n_all, len(zero2)))
for act in ('加预算', '观察', '暂停'):
    say('    真值 = %-4s : %2d 条' % (act, len(grp[act])))
say()

missed = grp['加预算']
lost = sum(num(truth[n]['差额']) for n in missed)
say('  ⚠️ 被这条规则挡掉、但真值其实「该加」的：%d 条' % len(missed))
if missed:
    say('     挡掉的总代价 = 这些广告加预算本可多赚 %.2f（合计）' % lost)
    say('     平均每条错过 %.2f' % (lost / len(missed)))
say()

# 这条规则救对了什么？它只说了"不加"，没说"停"，所以对"该停/观察"只是没帮倒忙
say('  这条规则对「该停/该观察」的 %d 条：它的作用只是"不加"（并没让它们停），' %
    (len(grp['暂停']) + len(grp['观察'])))
say('     不影响止损，因为止损要靠另一条纪律。')
say()

# 对照：末2天零单 vs 末2天有单，分别有多少该加
withorder = []
for name in truth:
    rows = vis_days(name)
    if len(rows) < 5:
        continue
    if zero_run_end(rows) < 2:
        withorder.append(name)
add_w = sum(1 for n in withorder if truth[n]['正确动作'] == '加预算')
say('  对照：末 2 天「有出单」的 %d 条里，真值该加的有 %d 条（%.0f%%）' %
    (len(withorder), add_w, add_w / len(withorder) * 100 if withorder else 0))
say('        末 2 天「零单」的 %d 条里，真值该加的有 %d 条（%.0f%%）' %
    (len(zero2), len(missed), len(missed) / len(zero2) * 100 if zero2 else 0))
say()

# ==================== C. 零单分型 ====================
say('=' * 78)
say('C. 「零单」分型：什么时候零单是噪声，什么时候是真信号')
say('=' * 78)
say()
say('  对「末 2 天零单」的每条广告，算它"零单本来就该发生"的概率 P0：')
say('    P0 高 ⇒ 零单是正常波动；P0 低 ⇒ 零单反常，该警惕')
say('  P0 中位数按真值分组：')
for act in ('加预算', '观察', '暂停'):
    ps = []
    for n in grp[act]:
        _, p0x = p_zero_two_days(vis_days(n))
        if p0x is not None:
            ps.append(p0x)
    if ps:
        ps.sort()
        say('    真值 %-4s : 中位 P0 = %.1f%%   （%d 条）' % (act, ps[len(ps) // 2] * 100, len(ps)))
say()
say('  解读：P0 越高说明这个零单越可能是噪声，越不该据此停止加钱。')
say()

# ==================== D / E. 名单 ====================
say('=' * 78)
say('D. 「末 2 天零单」但真值该加 的名单（规则会误伤）')
say('=' * 78)
say('  %-42s %8s %8s %8s %7s %7s %8s' %
    ('广告', '可见净', '频次5', '顶格率', 'P0', '差额', '真值'))
for n in sorted(missed, key=lambda z: -num(truth[z]['差额'])):
    rows = vis_days(n)
    _, p0x = p_zero_two_days(rows)
    t = truth[n]
    say('  %-42s %8.2f %8.3f %8.3f %6.1f%% %8.2f %8s' %
        (n[:42], num(t['前5天净']), num(t['_频次5']), num(t['_顶格率5']),
         (p0x or 0) * 100, num(t['差额']), t['正确动作']))
say()

say('=' * 78)
say('E. 「末 2 天零单」且真值该停 的名单（规则挡错方向了，这帮人该停）')
say('=' * 78)
say('  %-42s %8s %8s %8s %7s %8s' %
    ('广告', '可见净', '频次5', '顶格率', 'P0', '真值'))
for n in sorted(grp['暂停'], key=lambda z: num(truth[z]['前5天净'])):
    rows = vis_days(n)
    _, p0x = p_zero_two_days(rows)
    t = truth[n]
    say('  %-42s %8.2f %8.3f %8.3f %6.1f%% %8s' %
        (n[:42], num(t['前5天净']), num(t['_频次5']), num(t['_顶格率5']),
         (p0x or 0) * 100, t['正确动作']))
say()

# ==================== F. 若真按用户规则做，全库省/亏多少 ====================
say('=' * 78)
say('F. 如果全库都按「连续 2 天零单就不加」，账面差多少')
say('=' * 78)
say()
gain_avoid = 0.0    # 规则挡掉的该加 —— 损失
for n in missed:
    gain_avoid += num(truth[n]['差额'])
say('  挡掉 %d 条该加的，少赚 %.2f' % (len(missed), gain_avoid))
say('  但换来的是：这 %d 条里有 %d 条真值本来就不该加（观察 %d / 暂停 %d），' %
    (len(zero2), len(zero2) - len(missed), len(grp['观察']), len(grp['暂停'])))
say('    规则替你把「不该加」的全挡住了，代价是误伤 %d 条该加的。' % len(missed))
say('  准确率：规则判"不该加"命中率 = %d/%d = %.0f%%' %
    (len(zero2) - len(missed), len(zero2),
     (len(zero2) - len(missed)) / len(zero2) * 100 if zero2 else 0))
say()

with io.open(P_OUT, 'w', encoding='utf-8') as f:
    f.write('\n'.join(L))
print('OK', P_OUT)
