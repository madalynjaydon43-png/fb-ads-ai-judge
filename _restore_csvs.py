# -*- coding: utf-8 -*-
"""从落盘 JSON 还原逐条结论 CSV（口径写死，不再依赖哪一轮覆盖了哪个文件名）。

背景：`feed_v11_twophase.py` 每次都把结论写到 `AI逐条结论-v11.csv`，所以
150007 那轮把 132814 那轮（**纯 AI 基线 57%**）的 CSV 覆盖了 —— 而文档引用的正是 57% 那版。
这份脚本按 JSON 的修改时间把两轮分别还原成不同文件名，避免以后再看错基准。

  AI逐条结论-v11.csv       ← 132814（旧 prompt、无护栏）  AI 原判
  AI逐条结论-v12.csv       ← 150007（新 prompt 含 net_view）AI 原判（用 ai_action 还原）
  AI逐条结论-v12_护栏后.csv ← 150007 过了关停护栏后的最终动作
"""
import csv
import io
import json
import os

D = os.path.dirname(os.path.abspath(__file__))
OUTDIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '_out')
P_TRUTH = os.path.join(D, '真值表-v5.csv')

with io.open(P_TRUTH, encoding='utf-8-sig') as f:
    truth = {r['广告']: r for r in csv.DictReader(f)}

CN2 = {'pause': '暂停', 'increase_budget': '加预算',
       'decrease_budget': '减预算', 'observe': '观察'}

JOBS = [
    ('测试结果-20261002-132814.json', 'AI逐条结论-v11.csv', 'raw'),
    ('测试结果-20261002-150007.json', 'AI逐条结论-v12.csv', 'raw'),
    ('测试结果-20261002-150007.json', 'AI逐条结论-v12_护栏后.csv', 'guarded'),
]

L = []
for src, dst, mode in JOBS:
    p = os.path.join(OUTDIR, src)
    if not os.path.exists(p):
        L.append('[缺] %s' % src)
        continue
    with io.open(p, encoding='utf-8') as f:
        sg = json.load(f)['suggestions']
    out = []
    n_chg = 0
    for s in sg:
        name = str(s['campaign_id'])
        t = truth.get(name) or {}
        raw = s.get('ai_action') or s['action']
        act = s['action'] if mode == 'guarded' else raw
        if s.get('guardrail'):
            n_chg += 1
        out.append({
            '对象': name,
            '目标': t.get('目标', ''),
            '花费': t.get('总花费', ''),
            'ROAS': '',
            '真值': t.get('正确动作', ''),
            'AI动作': act,
            '一致': '✓' if CN2.get(act) == t.get('正确动作') else '✗',
            '调幅%': s.get('budget_change_pct', 0) or 0,
            '理由': s.get('reason', ''),
            '_护栏': s.get('guardrail', ''),
        })
    cols = ['对象', '目标', '花费', 'ROAS', '真值', 'AI动作', '一致', '调幅%', '理由']
    with io.open(os.path.join(D, dst), 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction='ignore')
        w.writeheader()
        w.writerows(out)
    L.append('%-28s <- %s  %d 条（其中带护栏标记 %d 条）' % (dst, src, len(out), n_chg))

txt = '\n'.join(L)
with io.open(os.path.join(D, '_restore_csvs_out.txt'), 'w', encoding='utf-8') as f:
    f.write(txt)
print(txt)
