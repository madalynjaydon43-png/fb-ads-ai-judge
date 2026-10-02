# -*- coding: utf-8 -*-
"""learn_cycle.py —— 飞轮的 CLI 扳手。

    python learn_cycle.py status                       # 现在攒到哪一步了
    python learn_cycle.py check --csv X                # 这份导出能不能喂进来（列自检）
    python learn_cycle.py views --paths A.csv B.csv     # 几份不同列的导出 → 去重 + 合并 + 缺口
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
import metric_views as mv
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


# ==================== check（列自检）====================

def cmd_check(args):
    """自检一份导出能不能喂进来：认出哪些列、缺哪些列、缺了哪几维会失真。"""
    path = args.csv or P_DATA
    print('=' * 78)
    print('列自检 · %s' % path)
    print('=' * 78)
    if not os.path.exists(path):
        print('文件不存在。', file=sys.stderr)
        return 2
    rows = tj_mod.read_csv_rows(path)
    if not rows:
        print('文件是空的。', file=sys.stderr)
        return 2

    header = list(rows[0].keys())
    miss, cm, unknown = tj_mod.missing_columns(header)
    ok = [k for k in tj_mod.BLOCKING_KEYS if cm.get(k)]
    print('表头 %d 列 · 数据 %d 行' % (len(header), len(rows)))
    print('必须的指标列：认出 %d / %d' % (len(ok), len(tj_mod.BLOCKING_KEYS)))
    try:
        g, meta = tj_mod.read_grouped_rows(path, strict=True)
        print('分组列：%s   日期列：%s   %d 组'
              % (meta['group_col'], meta['date_col'], len(g)))
    except tj_mod.ColumnError as e:
        print('分组失败：%s' % e, file=sys.stderr)
    print()
    print('  %-20s %-28s %s' % ('内部字段', '认到的列名', '用来算哪些特征'))
    print('  ' + '-' * 74)
    for k in tj_mod.REQUIRED_KEYS:
        col = cm.get(k) or '【缺】'
        mark = '' if cm.get(k) else ('  ← 可选' if k in tj_mod.OPTIONAL_KEYS else '  ← 缺列')
        print('  %-20s %-28s %s%s' % (k, col, tj_mod.COLUMN_USED_FOR.get(k, ''), mark))
    if unknown:
        print()
        print('  没被用上的列（正常，导出通常带一堆不需要的）：%s' % '、'.join(unknown[:12]))
        # P0/P1（2026-10-02）：有几列**不进 30 维模型、但要写进 prompt 当口径**。
        # 不解释的话，它们会混在「没被用上」里，让人以为白导了。
        CALIBER_COLS = {
            '归因设置': '归因窗口 —— 跨窗口的 ROAS/购买数不可比',
            '成效指标': '广告组到底在优化什么 —— 优化加购的不因「购买少」被关',
            '质量排名': '竞争排名 —— 唯一的外部对照（空 = Meta 没给，≠ 差）',
            '互动率排名': '竞争排名 —— 同上',
            '转化率排名': '竞争排名 —— 同上',
            '广告投放': '投放状态 —— 非「投放中」不给花钱动作',
            '广告组投放': '投放状态（广告组层级导出）—— 同上',
            '广告系列投放': '投放状态（广告系列层级导出）—— 同上',
            '结束日期': '排期 —— 已结束的投放不再给加预算',
            '视频平均播放时长': '观看深度 —— 3 秒播放高但时长极短 = 只被开头钩住（换素材，不是调预算）',
            '单次链接点击费用 - 独立用户': '重复点击 —— 独立用户比全部用户便宜 = 少数人反复点（查落地页，不是查曝光）',
            '单次 ThruPlay 费用': '视频成本 —— 看完的边际成本',
        }
        # ⚠️ 必须「每个表头只归到**最长**匹配的那一项」：
        #   '广告投放' 是 '广告组投放' 的子串，'单次链接点击费用' 是
        #   '单次链接点击费用 - 独立用户 (USD)' 的子串。按 dict 顺序逐个判会重复命中，
        #   同一列被解释两遍 —— 看着像两份证据，其实是一份。
        hit = {}
        for h in unknown:
            hs = str(h)
            best = None
            for k in CALIBER_COLS:
                if k in hs and (best is None or len(k) > len(best)):
                    best = k
            if best and best not in hit:
                hit[best] = CALIBER_COLS[best]
        if hit:
            print('  其中这几列**不进 30 维特征、但会写进 AI 的口径声明**（不是白导）：')
            for k, v in hit.items():
                print('    · %-22s %s' % (k, v))
            print('  （30 维特征是不变式，改动会作废历史打分，所以口径类字段走 prompt 而不进模型。）')
    print()
    if not miss:
        print('结论：列齐全，可以直接喂。')
        return 0
    print('结论：缺 %d 个必需列 —— %s' % (len(miss), '、'.join(tj_mod.COL_ALIASES[k][0] for k in miss)))
    print('      它们会被当成 0（= 「这项没读到」被当成「这项是 0」），判断结果不可信。')
    print('      会失真的特征：')
    for k in miss:
        print('        · %s  →  %s' % (tj_mod.COL_ALIASES[k][0], tj_mod.COLUMN_USED_FOR.get(k, '')))
    print()
    print('怎么补：Ads Manager → 报告 → 自定义列，勾上上面这些指标，再重新导出 CSV。')
    return 1


# ==================== views（多视图接入）====================

def _fmt(v, digits=4):
    if v is None:
        return '—'
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    if f == 0:
        return '0'
    if abs(f) >= 100:
        return '%.1f' % f
    return ('%.' + str(digits) + 'f') % f


def cmd_views(args):
    """把同一账户的多份不同列导出 → 去重 → 合并 → 报告能多看到什么。"""
    paths = list(args.paths or [])
    if args.dir:
        paths += mv.scan_dir(args.pattern or '*.csv', args.dir)
    if not paths:
        print('错误：至少给一个 --paths，或用 --dir 指一个目录。', file=sys.stderr)
        return 2

    print('=' * 88)
    print('多视图接入 · 共 %d 份文件' % len(paths))
    print('=' * 88)

    ov = mv.overview(paths=paths)
    exps = ov['exports']
    if not exps:
        print('没有可读的数据行。', file=sys.stderr)
        return 2

    # ---- 1. 每份是什么视图 ----
    print()
    print('【1】每份文件的视图识别')
    print('  %-42s %-12s %4s %5s %5s %5s' % ('文件', '视图', '列', '行', '广告', '天数'))
    print('  ' + '-' * 84)
    for e in exps:
        days = len(set(k[0] for k in e.keys()))
        print('  %-42s %-12s %4d %5d %5d %5d'
              % (e.name[:42], mv.view_cn(e.view), len(e.header), e.n, len(e.ads()), days))

    # ---- 2. 去重 ----
    print()
    print('【2】过滤相同的 → 留下不同的（按表头指纹分组）')
    for i, g in enumerate(ov['groups'], 1):
        same = [m.name for m in g['members']]
        print('  指纹%d（%s，%d 列）' % (i, mv.view_cn(g['view']), len(g['sig'])))
        print('      留用：%s（%d 行）' % (g['kept'].name, g['kept'].n))
        for d in g['dropped']:
            reason = '行数更少' if d.n < g['kept'].n else '同表头且更旧'
            print('      丢弃：%s（%d 行）—— 同一套指标，%s' % (d.name, d.n, reason))
        if len(same) == 1:
            print('      （这套指标只有一份）')
    print()
    print('  去重结果：%d 份 → %d 种不同的指标视图'
          % (len(exps), len(ov['chosen'])))

    # ---- 3. 合并 ----
    wide = ov['wide']
    mrep = ov['merge']
    cols = mv.wide_header(wide)
    print()
    print('【3】按 (日期, 广告) 合并')
    print('  宽表：%d 行 × %d 列（合并前最大的一份只有 %d 列）'
          % (len(wide), len(cols), max(len(e.header) for e in exps)))
    core_cols = 15
    print('  其中 %d 列来自核心指标，另有 %d 列是**多视图才凑得出来的新信息**'
          % (core_cols, max(0, len(cols) - core_cols)))
    if mrep['conflicts']:
        print('  ⚠️ 冲突 %d 处（同一(日期,广告)同一列，两份导出给了不同值）——'
              % len(mrep['conflicts']))
        print('     取文件更新的那份，但下面是全部明细，别当没看见：')
        for c in mrep['conflicts'][:8]:
            print('       %s | %s | %s: %s(%s) vs %s(%s)'
                  % (c['key'][0], c['key'][1][:16], c['col'][:18],
                     c['winner'][:14], c['winner_file'][:18],
                     c['loser'][:14], c['loser_file'][:18]))
        if len(mrep['conflicts']) > 8:
            print('       …另有 %d 处' % (len(mrep['conflicts']) - 8))
    else:
        print('  ✅ 零冲突：重叠行逐格一致，证明各份是同一账户事实的不同列预设。')

    # ---- 4. 缺口购物清单 ----
    gap = ov['gap']
    print()
    print('【4】还缺哪些视图（这就是「该再导哪几份」）')
    print('  已有：%s' % ('、'.join(mv.view_cn(v) for v in gap['present']) or '（无）'))
    if gap['unknown']:
        print('  没认出来：%s' % '、'.join(e.name for e in gap['unknown']))
    if not gap['missing']:
        print('  ✅ 五种视图齐了 —— 能看到的维度已经到头了。')
    else:
        for v, loss in gap['loss']:
            print('  · 缺 %-11s → 失去：%s' % (mv.view_cn(v), loss))

    # ---- 5. 预算列异常 ----
    if ov['budget_anomaly']:
        print()
        print('【5】⚠️ 「广告组预算」列里有非数字（平台在某些广告组上填的是文案）')
        for b in ov['budget_anomaly'][:6]:
            print('  · %s → %r（%s）' % (b['ad'][:26], b['value'], b['file'][:22]))
        print('  后果：读成 0 ⇒ 花费率、预算档这两个特征静默失效。'
              '要用这两个特征，得先确认这些广告组的真实预算。')

    # ---- 6. 扩展特征实算 ----
    cov, n_ad = mv.ext_coverage(wide)
    days_by_ad = ov['ext_days_by_ad']
    print()
    print('【6】扩展特征（核心 30 维之外，共 %d 维）· 实算覆盖率' % len(mv.EXT_FEATURE_KEYS))
    if not n_ad:
        print('  没有广告，跳过。')
    else:
        print('  %-20s %-10s %s' % ('特征', '算得出来', '说明'))
        print('  ' + '-' * 82)
        for k in mv.EXT_FEATURE_KEYS:
            c = cov.get(k, 0)
            mark = '✅' if c == n_ad else ('部分' if c else '✗ 缺列')
            print('  %-20s %-10s %s'
                  % (k, '%d/%d %s' % (c, n_ad, mark), mv.EXT_FEATURE_SPECS[k][0]))
        print()
        print('  逐条广告的实算值。括号里是**实际用了几天**——'
              '分子分母必须同窗口，天数少 = 结论脆弱，别当结论用。')
        keys = mv.EXT_FEATURE_KEYS
        head = '  %-24s' % '广告'
        for k in keys:
            head += ' %11s' % k[:11]
        print(head)
        for ad, f in ov['ext_by_ad'].items():
            line = '  %-24s' % ad[:24]
            for k in keys:
                v = f.get(k)
                if v is None:
                    cell = '—'
                else:
                    d = days_by_ad.get(ad, {}).get(k, 0)
                    cell = '%s(%dd)' % (_fmt(v), d)
                line += ' %11s' % cell[:11]
            print(line)
        suspects = []
        for ad, r in ov['ext_report'].items():
            for k in r.get('suspect', []):
                suspects.append((ad, k, r['features'].get(k)))
        if suspects:
            print()
            print('  ⚠️ 比值 > 1 的项（理论上不该 > 1）—— **不是算错，是平台两个指标分母不同**：')
            for ad, k, v in suspects[:6]:
                print('     %s · %s = %s' % (ad[:22], k, _fmt(v)))
            print('     例：「视频播放进度达 25% 的次数」可以大于「播放视频达 3 秒的次数」，'
                  '两个计数基底不同。')
            print('     这类比值只能做同口径横向比较，别当绝对留存率读。')
    print()
    print('说明：扩展特征目前**不喂给模型**（核心 30 维一个字没动）。'
          '要先有能验证它的数据，再谈要不要进模型。')
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

    c = sub.add_parser('check', help='列自检：这份导出能不能喂进来')
    c.add_argument('--csv', default=None, help='要检查的 CSV（默认检查仓库自带模拟数据）')
    c.set_defaults(func=cmd_check)

    v = sub.add_parser('views', help='多视图接入：几份不同列的导出 → 去重 + 合并 + 报告')
    v.add_argument('--paths', nargs='+', default=None, help='一份或多份导出 CSV')
    v.add_argument('--dir', default=None, help='扫描目录（配合 --pattern）')
    v.add_argument('--pattern', default='*.csv', help='目录里的文件名通配（默认 *.csv）')
    v.set_defaults(func=cmd_views)

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
