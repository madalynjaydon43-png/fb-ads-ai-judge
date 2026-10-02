# -*- coding: utf-8 -*-
"""feed_v11_twophase.py —— 用 **v5 数据**重跑两段式验证（受众池真实版）。

与 v10 的差别只有数据源：
  v10 用的是 v4 数据 —— 那份数据的**频次是凭空编的**（起点随机抽到 2.2、每天固定 ×1.08），
  用户实测指出「真实广告跑几个月频次还停在 1.4 左右」，据此判定 v4 不真实。
  v5 改成受众池驱动（reach curve），频次中位 1.21、最大 1.68，才像真的。

链路本身完全没动：仍然是
  ① fb_test_loader.load_file（GUI「上传文件」按钮用的同一个加载器）
  ② fb_ai_scheduler 分批喂 AI（测试通道，不碰生产历史）
  ③ 判卷对 `真值表-v5.csv`
"""
import csv
import io
import json
import os
import sys
import time
from collections import Counter
from datetime import datetime

D = os.path.dirname(os.path.abspath(__file__))
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

from fb_test_loader import load_file                      # noqa: E402
from fb_ai_scheduler import AdsAiScheduler                # noqa: E402

CSV_PATH = os.path.join(D, '模拟广告数据-100广告10天-v5.csv')
P_TRUTH = os.path.join(D, '真值表-v5.csv')
OUTDIR_AD = os.path.join(os.path.dirname(os.path.abspath(__file__)), '_out')
# 想让这一轮附带一句判断指引，就建 `_note_v11.txt`（**不改生产 prompt**）。
P_NOTE = os.path.join(D, '_note_v11.txt')
EXTRA = io.open(P_NOTE, encoding='utf-8').read().strip() if os.path.exists(P_NOTE) else ''
_sfx = '_note' if EXTRA else ''
P_REPORT = os.path.join(D, '两段式验证报告-v11%s.md' % _sfx)
P_CSV = os.path.join(D, 'AI逐条结论-v11%s.csv' % _sfx)

LOG = []


def log_fn(msg, level='INFO'):
    LOG.append('[%s] %s' % (level, msg))


rows, meta = load_file(CSV_PATH)
n_days = meta['n_ad_days']

ARGV = sys.argv[1:]
REUSE = None
if '--reuse' in ARGV:
    i = ARGV.index('--reuse')
    REUSE = ARGV[i + 1] if i + 1 < len(ARGV) else None

os.makedirs(OUTDIR_AD, exist_ok=True)
with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'ai_config.example.json'), encoding='utf-8') as _f:
    cfg = json.load(_f)

if REUSE:
    with open(REUSE, encoding='utf-8') as f:
        _d = json.load(f)
    sugg = _d['suggestions']
    T = _d.get('timing') or {}
    meta = _d.get('meta') or meta
    base = os.path.splitext(REUSE)[0]
    ts = os.path.basename(base).replace('测试结果-', '')
    wall = float(T.get('total', 0) or 0)
    print('复用落盘结果：%s（%d 条建议，未调用模型）' % (REUSE, len(sugg)))
else:
    ts = datetime.now().strftime('%Y%m%d-%H%M%S')
    sch = AdsAiScheduler(
        get_insights_fn=lambda: rows,
        log_fn=log_fn,
        history_path=os.path.join(_HERE, 'ai_test_history.json'),
        zero_spend_path=os.path.join(_HERE, 'ai_test_zero_spend_skipped.json'),
        config_override={'lookback_days': meta['days'],
                         'data_note': '\n'.join('- ' + g for g in meta['data_gaps'])
                                      + (('\n\n' + EXTRA) if EXTRA else '')},
        window=meta['window'],
    )
    wall0 = time.time()
    sugg = sch.run_once()
    wall = time.time() - wall0
    T = sch.last_timing or {}

    base = os.path.join(OUTDIR_AD, '测试结果-%s' % ts)
    with open(base + '.json', 'w', encoding='utf-8') as f:
        json.dump({'file': meta['file'], 'path': meta['path'], 'meta': meta,
                   'timing': T, 'suggestions': sugg}, f, ensure_ascii=False, indent=2)
    with open(base + '.csv', 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.DictWriter(f, fieldnames=['对象', '动作', '预算变化%', '理由'])
        w.writeheader()
        for s in sugg:
            w.writerow({'对象': s.get('campaign_id', ''), '动作': s.get('action', ''),
                        '预算变化%': s.get('budget_change_pct', ''), '理由': s.get('reason', '')})

# ---------- 判卷 ----------
truth = list(csv.DictReader(io.StringIO(open(P_TRUTH, encoding='utf-8-sig').read())))
tv = {t['广告']: t for t in truth}


def f(x):
    try:
        return float(x or 0)
    except Exception:
        return 0.0


CN = {'observe': '观察', 'increase_budget': '加预算',
      'decrease_budget': '暂停', 'pause': '暂停'}

n_kill_truth = sum(1 for t in tv.values() if t['正确动作'] in ('暂停', '减预算'))
n_up_truth = sum(1 for t in tv.values() if t['正确动作'] == '加预算')
n_hold_truth = sum(1 for t in tv.values() if t['正确动作'] == '观察')


def metrics(sg):
    kill = {str(s['campaign_id']) for s in sg if s.get('action') in ('pause', 'decrease_budget')}
    hit_ = [i for i in kill if tv.get(i, {}).get('正确动作') in ('暂停', '减预算')]
    mis_ = [i for i in kill if tv.get(i, {}).get('正确动作') == '加预算']
    over_ = [i for i in kill if tv.get(i, {}).get('正确动作') == '观察']
    miss_ = [i for i in {r['id'] for r in rows}
             if tv.get(i, {}).get('正确动作') == '暂停' and i not in kill]
    ex_ = [s for s in sg if tv.get(str(s['campaign_id']), {}).get('正确动作') == CN.get(s.get('action'))]
    up_ = [s for s in sg if tv.get(str(s['campaign_id']), {}).get('正确动作') == '加预算'
           and s.get('action') == 'increase_budget']
    return {
        'n': len(sg), 'exact': len(ex_), 'acc': len(ex_) / max(1, len(sg)) * 100,
        'kill': len(kill), 'hit': len(hit_),
        'hitrate': len(hit_) / max(1, len(kill)) * 100,
        'recall': len(hit_) / max(1, n_kill_truth) * 100,
        'miskill': len(mis_), 'loss': sum(f(tv[i]['总收入']) for i in mis_ if i in tv),
        'misskip': len(miss_), 'waste': sum(f(tv[i]['总花费']) - f(tv[i]['总收入'])
                                            for i in miss_ if i in tv),
        'spared': n_kill_truth - len(hit_),
        'overkill': len(over_), 'scale': len(up_),
        'scale_recall': len(up_) / max(1, n_up_truth) * 100,
        'up_misplaced': len([s for s in sg
                             if s.get('action') == 'increase_budget'
                             and tv.get(str(s['campaign_id']), {}).get('正确动作') != '加预算']),
    }


sid = {str(s.get('campaign_id')) for s in sugg}
missing_ids = {r['id'] for r in rows} - sid
bumper = [s for s in sugg if '自动兜底' in str(s.get('reason', ''))]
act = Counter(s.get('action') for s in sugg)
M = metrics(sugg)

R = []


def say(s=''):
    R.append(s)


say('# 两段式验证 · 报告 v11（受众池真实版数据）')
say()
say('## 0. 为什么又做一遍')
say()
say('v10 的结论建立在 v4 数据上，而 v4 数据的**频次是凭空编的**：')
say()
say('```python')
say('freq0 = RNG.uniform(1.00, 2.20)        # 频次起点直接随机抽，最高 2.2')
say('freq_d = a[\'freq0\'] * (1 + 0.080 * d) # 每天固定 ×1.08 往上涨')
say('reach  = imp / freq_d                  # 于是覆盖天天跌、展示天天一样')
say('```')
say()
say('用户实测经验：**真实广告跑几个月，频次还在 1.4 左右**，不可能 5 天就从 1.1 冲到 2.5。')
say('据此判定 v4 不真实，重做 v5：频次不再是自变量，而是**受众池被消耗的副产物**')
say('（经典 reach curve：`R(I) = A·(1 − e^(−I/(A·f0)))`）。')
say()
say('| | v4（旧） | v5（新） |')
say('|---|---:|---:|')
say('| 第 5 天频次 中位 | 1.71 | **1.19** |')
say('| 第 5 天频次 最大 | 2.87 | **1.83** |')
say('| 花费 5 天完全不变的广告 | 100/100 | **0/100** |')
say('| 展示 5 天完全不变的广告 | 100/100 | **0/100** |')
say('| 「购物次数=0 却有转化价值」的行 | 有 | **0/500** |')
say()
say('## 1. 这份报告是什么（两段式验证）')
say()
say('1. 只把**前 5 天**喂给 AI（`%s`）→ 让它给「关停 / 加预算 / 观察」的建议' % meta['file'])
say('2. 用**后 5 天**验算：同一条广告按原样再跑 5 天 vs 加预算 20% 再跑 5 天，')
say('   谁净收益高，「正确答案」就定谁。**AI 完全没见过后 5 天。**')
say()
say('| 参考线（同一份 v5 数据上实测） | 分数 |')
say('|---|---:|')
say('| 随机瞎猜（3 类） | 33.3% |')
say('| 基线：恒定判「暂停」 | 35.0% |')
say('| 基线：恒定判「加预算」 | 35.0% |')
say('| 基线：恒定判「观察」 | 30.0% |')
say('| 简单树模型天花板（只用可见列，三分类） | 79.0% |')
say('| 人工规则「ROAS>1.8 且频次增幅<0.25→加」 | 66.0% |')
say('| **「该加 vs 不该加」二分类上限**（逻辑回归 AUC） | **0.84** |')
say()
say('| 项 | 值 |')
say('|---|---|')
say('| 文件 | `%s`（%d 行）|' % (meta['file'], meta['n_rows_in']))
say('| 加载器 | `fb_test_loader.py`（本仓库根目录，**与 GUI 用的是同一个**）|')
say('| 规模 | **%d 条 × %d 天 = %d 个「对象-日」** |' % (meta['n_ads'], meta['days'], n_days))
say('| 时间窗 | %s ~ %s |' % (meta['window'][0], meta['window'][1]))
say('| 合计 | 花费 $%.2f / 购买 %d / 购买价值 $%.2f |'
    % (meta['total_spend'], meta['total_purchase'], sum(r['purchase_value'] for r in rows)))
say('| 模型 | `%s` @ `%s` |' % (cfg.get('model'), cfg.get('base_url')))
say('| 分批 | 每批 %s 条 / %s 路并发 |' % (cfg.get('batch_size'), cfg.get('concurrent_workers')))
say('| 执行时刻 | %s |' % ts)
say()
say('## 2. 耗时 / 覆盖率')
say()
say('| 项 | 值 |')
say('|---|---:|')
say('| 墙钟（脚本视角） | **%.1f s** |' % wall)
say('| 模型调用（分批累加） | %s s |' % T.get('llm', 0))
say('| 请求数 / 重试 / 失败批 | %s / %s / %s |'
    % (T.get('llm_calls', 0), T.get('retries', 0), T.get('failed_batches', 0)))
say('| 送进 AI | **%d** |' % len(rows))
say('| 产出结论 | **%d** |' % len(sugg))
say('| 覆盖率 | **%.1f%%** |' % (len(sugg) / max(1, len(rows)) * 100))
say('| 程序兜底 observe | %d 条 |' % len(bumper))
say('| 模型完全没提到的 id | %d 条 |' % len(missing_ids))
say()
say('## 3. 动作分布（AI 给了什么）')
say()
say('| 动作 | 条数 | 占比 | 真值里的条数 |')
say('|---|---:|---:|---:|')
for k, v in act.most_common():
    tv_n = {'observe': n_hold_truth, 'increase_budget': n_up_truth,
            'pause': n_kill_truth, 'decrease_budget': 0}.get(k, 0)
    say('| %s（%s）| %d | %.1f%% | %d |' % (CN.get(k, '?'), k, v, v / max(1, len(sugg)) * 100, tv_n))
say()
say('## 4. 判卷（真值 `真值表-v5.csv`）')
say()
say('| 指标 | v11 |')
say('|---|---:|')
for label, tpl, keys in [
    ('**动作与真值完全一致**', '**%d/%d = %.1f%%**', ('exact', 'n', 'acc')),
    ('真该关的总数（真值）', '%d', None),
    ('AI 判死（暂停）条数', '%d', ('kill',)),
    ('其中真该关（命中）', '%d', ('hit',)),
    ('关停 判对率', '%.1f%%', ('hitrate',)),
    ('关停 召回率', '**%.1f%%**', ('recall',)),
    ('关停 误杀（该留被砍）', '%d 条 / 收入损失 $%.0f', ('miskill', 'loss')),
    ('关停 漏杀（该关没关）', '%d 条', ('misskip',)),
    ('「只需观察」被判成动', '%d 条', ('overkill',)),
    ('**加预算 召回率**', '**%.1f%%**', ('scale_recall',)),
    ('加预算 误加（不该加却加）', '%d 条', ('up_misplaced',)),
]:
    if keys is None:
        say('| %s | %d |' % (label, n_kill_truth))
        continue
    say('| %s | %s |' % (label, tpl % tuple(M[k] for k in keys)))
say()
say('## 5. 逐条（全部 %d 条）' % len(sugg))
say()
say('| # | 对象 | 目标 | 花费$ | ROAS | 真值 | AI 动作 | 一致 | 理由 |')
say('|---:|---|---|---:|---:|---|---|---|---|')
csv_rows = []
for i, r in enumerate(rows, 1):
    s = next((x for x in sugg if str(x.get('campaign_id')) == r['id']), None)
    if not s:
        continue
    roas = (f(r['purchase_value']) / f(r['spend'])) if f(r['spend']) else 0
    t = tv.get(r['id'], {})
    mark = '✓' if t.get('正确动作') == CN.get(s.get('action')) else '✗'
    rs = str(s.get('reason', '')).replace('\n', ' ').replace('|', '/')[:60]
    say('| %d | %s | %s | %.0f | %.2f | %s | %s | %s | %s |'
        % (i, r['id'][:26], r['objective'][-8:], r['spend'], roas,
           t.get('正确动作', '?'), s.get('action'), mark, rs))
    csv_rows.append({'对象': r['id'], '目标': r['objective'], '花费': round(r['spend'], 2),
                     'ROAS': round(roas, 2), '真值': t.get('正确动作', '?'),
                     'AI动作': s.get('action'), '一致': mark,
                     '调幅%': s.get('budget_change_pct', ''), '理由': s.get('reason', '')})
say()
say('## 6. 落盘位置')
say()
say('- `%s.csv`（逐条结论）' % base)
say('- `%s.json`（文件画像 + 耗时 + 全量建议）' % base)
say('- 测试通道：生产 `ai_history.json` 未被触碰')

with open(P_REPORT, 'w', encoding='utf-8') as fp:
    fp.write('\n'.join(R))
with open(P_CSV, 'w', encoding='utf-8-sig', newline='') as fp:
    w = csv.DictWriter(fp, fieldnames=['对象', '目标', '花费', 'ROAS', '真值', 'AI动作',
                                       '一致', '调幅%', '理由'])
    w.writeheader()
    w.writerows(csv_rows)

print('覆盖 %d/%d | 准确率 %.1f%% | 关停判对 %.1f%% 召回 %.1f%% | 加预算召回 %.1f%% | 误杀损失 $%.0f | 墙钟 %.1fs'
      % (len(sugg), len(rows), M['acc'], M['hitrate'], M['recall'], M['scale_recall'], M['loss'], wall))
print('report -> %s' % P_REPORT)
