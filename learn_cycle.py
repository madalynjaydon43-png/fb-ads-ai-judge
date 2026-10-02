# -*- coding: utf-8 -*-
"""learn_cycle.py —— 飞轮的 CLI 扳手。

    python learn_cycle.py status                       # 现在攒到哪一步了
    python learn_cycle.py backfill --mode sim          # 模拟数据回填（反事实真值表）
    python learn_cycle.py backfill --mode real --csv X # 真实导出回填（无反事实）
    python learn_cycle.py backtest                     # 判卷：模型 vs 规则回退
    python learn_cycle.py backtest --from-sim          # 不落盘，直接拿模拟数据判卷

设计约束：
  · 只依赖标准库 + table_judger（判卷时才需要 sklearn）；
  · **任何输出都不写 token / API key** —— 本脚本不读任何含凭据的配置文件；
  · 判卷口径与仓库其它脚本一致：钱 = Σ 后续窗口净，动作三分类准确率 + 各类召回。
"""
import argparse
import json
import os
import sys

D = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, D)

import flywheel
import table_judger as tj_mod
from table_judger import TableJudger, rule_action, truth_payoff

P_STORE = os.path.join(D, 'learning_store.jsonl')
P_DATA = os.path.join(D, '模拟广告数据-100广告10天-v5.csv')
P_TRUTH = os.path.join(D, '真值表-v5.csv')

CN = {'pause': '暂停', 'increase_budget': '加预算', 'observe': '观察'}


# ==================== status ====================

def cmd_status(args):
    st = flywheel.stats(args.store, args.min_labels)
    print('=' * 66)
    print('飞轮状态（learning_store.jsonl）')
    print('=' * 66)
    exists = os.path.exists(st['path'])
    print('存储文件      : %s %s' % (st['path'], '' if exists else '（尚不存在）'))
    print('快照总数      : %d' % st['snapshots'])
    print('已标注        : %d' % st['labeled'])
    print('未标注        : %d' % st['unlabeled'])
    labels = st['labels']
    if labels:
        order = ['observe', 'increase_budget', 'pause']
        parts = ['%s %d' % (CN.get(k, k), labels[k])
                 for k in order if labels.get(k)] + \
                ['%s %d' % (CN.get(k, k), v) for k, v in labels.items() if k not in order]
        print('标签分布      : %s' % ' / '.join(parts))
    else:
        print('标签分布      : （还没有带标签样本）')
    print('上岗线        : %d 条' % st['min_labels'])
    if st['ready']:
        print('距上岗        : 已达线 —— 可以拟合模型并判卷了')
    else:
        print('距上岗        : 还差 %d 条' % st['shortfall'])
    print()
    print('下一步：')
    if not exists or st['snapshots'] == 0:
        print('  先攒快照：跑一轮判断（flywheel.record_batch），或')
        print('  直接回填：python learn_cycle.py backfill --mode sim')
    elif st['labeled'] < st['min_labels']:
        print('  继续攒：python learn_cycle.py backfill --mode sim')
    else:
        print('  判卷复核：python learn_cycle.py backtest')
    return 0


# ==================== backfill ====================

def cmd_backfill(args):
    if args.mode == 'sim':
        added, updated = flywheel.backfill_from_truth(
            args.data or P_DATA, args.truth or P_TRUTH, args.store)
        print('模拟模式回填：新增 %d 条 / 更新（补 outcome） %d 条' % (added, updated))
        print('  数据来源假设：反事实式生成（停/不动/加 三版结局都存在）counterfactual=True')
    else:
        if not args.csv:
            print('错误：--mode real 必须给 --csv（Ads Manager 导出的逐日 CSV）', file=sys.stderr)
            return 2
        added, updated = flywheel.backfill_from_window(
            args.csv, args.store, args.head_days, args.split)
        print('真实模式回填：新增 %d 条 / 更新 %d 条' % (added, updated))
        print('  ⚠️ 数据来源假设：真实轨迹只有一条，net_hold = net_up = 真实净，')
        print('     counterfactual=False —— 模型学不到「加预算能不能救回来」。')
    st = flywheel.stats(args.store, args.min_labels)
    print('现在：快照 %d / 已标注 %d / 距上岗还差 %d' %
          (st['snapshots'], st['labeled'], st['shortfall']))
    return 0


# ==================== backtest ====================

def _samples(args):
    if args.from_sim:
        recs = flywheel.build_records_from_truth(
            args.data or P_DATA, args.truth or P_TRUTH, getattr(args, 'hid', None))
        return flywheel.records_to_samples(recs)
    return flywheel.records_to_samples(flywheel.load_store(args.store))


def _load_truth_for_names(samples, truth_path):
    if not truth_path or not os.path.exists(truth_path):
        return None
    truth = tj_mod.load_truth_v5(truth_path)
    names = {s.get('ad_name') for s in samples}
    if not (names & set(truth)):
        return None
    return truth


def _rule_baseline(samples, truth):
    """规则回退基线的成绩（同批样本、同判卷口径），用于并排对照。"""
    by_name = {}
    for s in samples:
        f = s['features']
        by_name[s.get('ad_name')] = rule_action(
            net=f.get('收入', 0.0) - f.get('花费', 0.0),
            fill_rate=_fill_from_feat(f), freq_last=f.get('频次末'),
        )
    if truth:
        names = [n for n in truth if n in by_name]
        return tj_mod.score_actions(
            names, lambda n: by_name.get(n), truth_payoff, truth)
    return None


def _fill_from_feat(f):
    """规则回退用的顶格率：特征里没有直接的顶格率，用花费率近似（≥0.95 视为顶格）。"""
    fr = f.get('花费率')
    try:
        return 1.0 if float(fr) >= 0.95 else 0.0
    except (TypeError, ValueError):
        return None


def _print_report(title, res, ref=None):
    print('=' * 92)
    print(title)
    print('=' * 92)
    print('  %-24s %8s %8s %8s %9s %8s %10s %10s %11s' %
          ('方案', '准确率', '关停召回', '关停精度', '加预算召回', '观察召回',
           '漏杀$', '误杀$', '后段总净'))
    print('  ' + '-' * 110)
    for label, r in ([('表格模型（5 折折外）', res)] + ([('规则回退基线', ref)] if ref else [])):
        print('  %-24s %7.1f%% %7.0f%% %7.0f%% %9.1f%% %7.0f%% %10.2f %10.2f %+11.2f'
              % (label, r['accuracy'], r['stop_recall'], r['stop_precision'],
                 r['up_recall'], r.get('observe_recall', 0.0),
                 r['miss_kill_net'], r['over_kill_net'], r['total_net']))
    print()
    print('  样本 %d 条（5 折折外预测；置信度 <%.2f 的折用规则回退，共 %d 条）'
          % (res['n'], tj_mod.MIN_CONFIDENCE, res.get('abstain', 0)))
    if res.get('label_counts'):
        print('  训练集标签分布：%s'
              % ' / '.join('%s %d' % (CN.get(k, k), v) for k, v in res['label_counts'].items()))
    print('  口径：准确率 = 三分类命中 / 样本数；钱 = Σ 后续窗口净（停→0，不动/加→真值表两版）')


def cmd_backtest(args):
    samples = _samples(args)
    if not samples:
        print('没有可用样本。先跑：python learn_cycle.py backfill --mode sim', file=sys.stderr)
        return 2
    # 判卷口径统一到真值表（与 LLM / 规则 / 可解性验证并排可比）：
    # 训练标签用 derive_label（可观测版），判卷用真值表「正确动作」（标准答案）。
    truth = _load_truth_for_names(samples, args.truth or P_TRUTH)
    judge = TableJudger(min_labels=args.min_labels, config={})
    try:
        res = judge.backtest(samples, truth=truth, folds=args.folds)
    except ValueError as e:
        print('判卷被拒绝：%s' % e, file=sys.stderr)
        print('（这是设计行为：样本太少时的成绩没有意义，宁可不给）', file=sys.stderr)
        return 3
    ref = _rule_baseline(samples, truth)
    src = '模拟数据（反事实真值表）' if args.from_sim else 'learning_store.jsonl'
    _print_report('TableJudger 判卷成绩单 · 数据来源：%s · 真值口径：%s'
                  % (src, '真值表-v5.csv（仓库金额口径）' if truth else 'outcome 净额'),
                  res, ref)
    if args.json:
        with open(args.json, 'w', encoding='utf-8') as f:
            json.dump({'model': res, 'rule_baseline': ref}, f,
                      ensure_ascii=False, indent=2)
        print('\n（成绩单已写入 %s）' % args.json)
    return 0


# ==================== main ====================

def build_parser():
    p = argparse.ArgumentParser(
        prog='learn_cycle.py',
        description='fb-ads-ai-judge 的学习飞轮扳手（status / backfill / backtest）')
    p.add_argument('--store', default=P_STORE, help='learning_store.jsonl 路径')
    p.add_argument('--min-labels', type=int, default=80, help='上岗线（默认 80 条）')
    sub = p.add_subparsers(dest='cmd')

    s = sub.add_parser('status', help='查看飞轮进度')
    s.add_argument('--store', default=P_STORE)
    s.add_argument('--min-labels', type=int, default=80)
    s.set_defaults(func=cmd_status)

    b = sub.add_parser('backfill', help='回填标签')
    b.add_argument('--mode', choices=['sim', 'real'], default='sim')
    b.add_argument('--store', default=P_STORE)
    b.add_argument('--min-labels', type=int, default=80)
    b.add_argument('--data', default=None, help='模拟模式：投放数据 CSV')
    b.add_argument('--truth', default=None, help='模拟模式：真值表 CSV')
    b.add_argument('--hid', default=None, help='模拟模式：逐日真值表 CSV（算 outcome 用）')
    b.add_argument('--csv', default=None, help='真实模式：Ads Manager 导出 CSV')
    b.add_argument('--split', default=None, help='真实模式：判断时点 YYYY-MM-DD')
    b.add_argument('--head-days', type=int, default=5, help='可见窗口天数（默认 5）')
    b.set_defaults(func=cmd_backfill)

    t = sub.add_parser('backtest', help='判卷')
    t.add_argument('--store', default=P_STORE)
    t.add_argument('--min-labels', type=int, default=80)
    t.add_argument('--from-sim', action='store_true', help='不落盘，直接拿模拟数据判卷')
    t.add_argument('--data', default=None)
    t.add_argument('--truth', default=None)
    t.add_argument('--hid', default=None, help='逐日真值表 CSV（换种子时指到新种子那份）')
    t.add_argument('--head-days', type=int, default=5)
    t.add_argument('--folds', type=int, default=5)
    t.add_argument('--json', default=None, help='把成绩单写成 json')
    t.set_defaults(func=cmd_backtest)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    if not getattr(args, 'func', None):
        build_parser().print_help()
        return 1
    return args.func(args)


if __name__ == '__main__':
    sys.exit(main())
