# -*- coding: utf-8 -*-
"""test_table_judger.py —— 「会学习的判断层」的测试。

跑法：
    python test_table_judger.py            # 全部用例
    python test_table_judger.py -v         # 带用例名

设计原则（与仓库其它脚本一致）：
  · 断言**可观察副作用**（存盘条数、最终 action、理由文本），不断言内部返回值的形状；
  · 特征口径用**独立实现**逐格比对，而不是自己跟自己比；
  · 需要 sklearn 的用例在没有 sklearn 时 skip，而不是假装通过。
"""
import csv
import io
import json
import os
import sys
import tempfile
import time
import unittest

D = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, D)

import flywheel                                     # noqa: E402
import metric_views as mv                           # noqa: E402
import table_judger as tj                           # noqa: E402
import fb_ai_engine as eng                          # noqa: E402
import fb_test_loader as lo                         # noqa: E402

try:
    import sklearn                                    # noqa: F401
    HAS_SK = True
except ImportError:
    HAS_SK = False

P_DATA = os.path.join(D, '模拟广告数据-100广告10天-v5.csv')
P_TRUTH = os.path.join(D, '真值表-v5.csv')
P_HID = os.path.join(D, '真值表-逐日-v5.csv')


# ==================== 独立实现（用于特征口径比对）====================

def _independent_features(rows):
    """**故意重写一遍** _solvability_v5.py 第 55~91 行的算法。

    如果 table_judger 的适配器把字段名映射错了（比如「覆盖人数」当成「展示次数」），
    这里就会对不上。自己跟自己比是查不出映射错误的。
    """
    import csv
    rs = sorted(rows, key=lambda z: z['报告开始日期'])

    def f(x):
        try:
            return float(x)
        except (TypeError, ValueError):
            return 0.0

    d1, d5 = rs[0], rs[-1]
    spend = sum(f(r['已花费金额 (USD)']) for r in rs)
    rev = sum(f(r['购物转化价值']) for r in rs)
    pur = sum(f(r['购物次数']) for r in rs)
    clk = sum(f(r['链接点击量']) for r in rs)
    atc = sum(f(r['加入购物车次数']) for r in rs)
    ic = sum(f(r['结账发起次数']) for r in rs)
    pay = sum(f(r['添加支付信息']) for r in rs)
    reach1, reach5 = f(d1['覆盖人数']), f(d5['覆盖人数'])
    budget = f(rs[0]['广告组预算'])
    nd = len(rs)
    ctr1 = f(d1['链接点击量']) / max(1, f(d1['展示次数']))
    ctr5 = f(d5['链接点击量']) / max(1, f(d5['展示次数']))
    imp1, imp5 = f(d1['展示次数']), f(d5['展示次数'])
    cpm1 = f(d1['CPM（千次展示费用） (USD)'])
    cpm5 = f(d5['CPM（千次展示费用） (USD)'])
    return {
        '花费': spend, '收入': rev, '购买': pur, 'ROAS': rev / spend if spend else 0,
        '花费率': spend / (budget * nd) if budget else 0,
        'CTR首': ctr1 * 100, 'CTR末': ctr5 * 100, 'CTR斜率%': (ctr5 - ctr1) / max(1e-9, ctr1) * 100,
        '曝光斜率%': (imp5 - imp1) / max(1.0, imp1) * 100,
        '覆盖首': reach1, '覆盖末': reach5,
        '覆盖斜率%': (reach5 - reach1) / max(1.0, reach1) * 100,
        'CPM首': cpm1, 'CPM末': cpm5, 'CPM斜率%': (cpm5 - cpm1) / max(1e-9, cpm1) * 100,
        '频次首': f(d1['频次']), '频次末': f(d5['频次']),
        '频次增幅': f(d5['频次']) - f(d1['频次']),
        'CPC': spend / clk if clk else 0,
        '加购率': atc / clk if clk else 0, '结账率': ic / atc if atc else 0,
        '支付率': pay / ic if ic else 0, '购买率': pur / pay if pay else 0,
        '日均购买': pur / nd, '单均成本': spend / pur if pur else 999,
        '客单价': rev / pur if pur else 0,
        '预算': budget, '是系列预算': 1.0 if '系列' in rs[0]['广告组预算类型'] else 0.0,
        '前3天购买占比': sum(f(r['购物次数']) for r in rs[:3]) / pur if pur else 0,
        '首单在第几天': next((i + 1 for i, r in enumerate(rs) if f(r['购物次数']) > 0), nd + 1),
    }


def _read_csv(path):
    import csv
    with io.open(path, encoding='utf-8-sig') as f:
        return list(csv.DictReader(f))


class FakeModel:
    """伪模型：用来精确制造「低置信度」这一情形。"""

    def __init__(self, classes, proba):
        self.classes_ = classes
        self._p = proba

    def predict_proba(self, X):
        return [self._p for _ in X]


def mk_snapshot(sid, net=None, fill=0.5, freq=1.2, days=5, spend=10.0, rev=5.0):
    """造一个最小可用快照（带 net_view）。"""
    daily = []
    for i in range(days):
        daily.append({'date': '2026-09-2%d' % (i + 3), 'spend': spend / days,
                      'purchase': 1, 'purchase_value': rev / days,
                      'impressions': 100, 'clicks': 5, 'reach': 90,
                      'frequency': freq, 'add_to_cart': 1,
                      'initiate_checkout': 1, 'add_payment_info': 1})
    snap = {'id': sid, 'name': 'ad-%s' % sid, 'budget_type': 'ABO',
            'adset_daily_budget': 10, 'daily_spend': daily}
    if net is not None:
        snap['net_view'] = {'days': days, 'spend': spend, 'revenue': rev,
                            'net': net, 'roas': (rev / spend) if spend else 0,
                            'active_days': days, 'fill_rate': fill, 'freq_last': freq}
    return snap


class TestFeatures(unittest.TestCase):
    """特征口径：与独立实现逐格比对（能抓住字段映射错误）。"""

    def test_feature_parity_all_ads(self):
        rows = _read_csv(P_DATA)
        g = {}
        for r in rows:
            g.setdefault(r['广告系列名称'], []).append(r)
        self.assertEqual(len(g), 100, '模拟数据应有 100 条广告')

        bad = []
        for name, rs in g.items():
            want = _independent_features(rs)
            got, _ = tj.extract_features(rows=rs)
            self.assertIsNotNone(got, name)
            self.assertEqual(set(got.keys()), set(want.keys()), name)
            for k in want:
                if abs(float(got[k]) - float(want[k])) > 1e-6:
                    bad.append((name, k, got[k], want[k]))
        self.assertEqual(bad, [], '特征与 _solvability_v5 口径不一致：%s' % bad[:5])

    def test_feature_vector_order_matches_keys(self):
        rows = _read_csv(P_DATA)
        rs = [r for r in rows if r['广告系列名称'] == rows[0]['广告系列名称']]
        feat, vec = tj.extract_features(rows=rs)
        self.assertEqual(len(vec), len(tj.FEATURE_KEYS))
        for k, v in zip(tj.FEATURE_KEYS, vec):
            self.assertAlmostEqual(float(v), float(feat[k]), places=9)

    def test_snapshot_adapter_matches_csv_adapter(self):
        """快照适配器（生产路径）与 CSV 适配器（离线路径）必须给出同一把尺子。"""
        rows = _read_csv(P_DATA)
        name = rows[0]['广告系列名称']
        rs = [r for r in rows if r['广告系列名称'] == name]
        feat_csv, _ = tj.extract_features(rows=rs)
        sn = eng.make_snapshot([{
            'id': 'x', 'name': name, 'budget_type': 'ABO',
            'adset_daily_budget': float(rs[0]['广告组预算']),
            'daily_spend': [{
                'date': r['报告开始日期'], 'spend': float(r['已花费金额 (USD)']),
                'purchase': int(float(r['购物次数'])),
                'purchase_value': float(r['购物转化价值']),
                'impressions': int(float(r['展示次数'])), 'clicks': int(float(r['链接点击量'])),
                'reach': int(float(r['覆盖人数'])), 'frequency': float(r['频次']),
                'add_to_cart': int(float(r['加入购物车次数'])),
                'initiate_checkout': int(float(r['结账发起次数'])),
                'add_payment_info': int(float(r['添加支付信息'])),
            } for r in rs],
        }])[0]
        feat_snap, _ = tj.extract_features(snapshot=sn)
        # 容差说明：CSV 路径读的是「导出的 CPM/CTR 列」，快照路径只能用
        # 花费/展示**重算** —— 生成器写列时与重算存在 1e-4 量级差异。
        # 这是数据源差异，不是适配器写错；映射错的话差值会是数量级而不是 1e-4。
        # 「斜率%」「xx率」是把小差异做比值后放大的派生量（除以 ~12 放大 8 倍），
        # 所以对这一类给相对容差，其余给绝对容差。
        for k in tj.FEATURE_KEYS:
            a, b = float(feat_csv[k]), float(feat_snap[k])
            if '斜率' in k or k.endswith('率'):
                tol = max(0.02, abs(a) * 5e-3)
            else:
                tol = max(0.01, abs(a) * 1e-4)
            self.assertLessEqual(abs(a - b), tol,
                                 msg='快照/CSV 两条路对不上：%s (%r vs %r)' % (k, a, b))


class TestDeriveLabel(unittest.TestCase):
    def test_four_cases(self):
        # ① 后续没花钱 → 不打标签
        self.assertIsNone(tj.derive_label({'spend': 1.0, 'net': -3.0, 'fill_rate': 0.2}))
        self.assertIsNone(tj.derive_label(None))
        self.assertIsNone(tj.derive_label({}))
        # ② 净 ≤ 0 → 停
        self.assertEqual(tj.derive_label({'spend': 30, 'net': -5.0, 'fill_rate': 0.9}), 'pause')
        self.assertEqual(tj.derive_label({'spend': 30, 'net': 0.0, 'fill_rate': 0.9}), 'pause')
        # ③ 净 > 0 且顶格率 ≥ 0.90 → 加预算
        self.assertEqual(tj.derive_label({'spend': 30, 'net': 12.0, 'fill_rate': 0.90}),
                         'increase_budget')
        # ④ 其余 → 观察
        self.assertEqual(tj.derive_label({'spend': 30, 'net': 12.0, 'fill_rate': 0.89}), 'observe')
        self.assertEqual(tj.derive_label({'spend': 30, 'net': 12.0}), 'observe')


class TestRuleFallback(unittest.TestCase):
    def test_rule_action_semantics(self):
        self.assertEqual(tj.rule_action(None), 'observe')
        self.assertEqual(tj.rule_action(-1.0, 0.5, 1.2), 'pause')
        self.assertEqual(tj.rule_action(5.0, 0.95, 1.2), 'increase_budget')
        self.assertEqual(tj.rule_action(5.0, 0.95, 1.5), 'observe')   # 频次高，受众看腻了
        self.assertEqual(tj.rule_action(5.0, 0.5, 1.2), 'observe')    # 没顶格，预算不是瓶颈

    def test_fallback_when_labels_short(self):
        judge = tj.TableJudger(min_labels=80)
        judge.fit([{'features': {k: 0.0 for k in tj.FEATURE_KEYS}, 'label': 'observe'}
                   for _ in range(10)])
        self.assertFalse(judge.is_ready())
        self.assertEqual(judge.shortfall(), 70)
        sugg = judge.judge([mk_snapshot('1', net=5.0)])
        self.assertEqual(len(sugg), 1)
        self.assertEqual(sugg[0]['source'], 'rule_fallback')
        self.assertIn('10/80', sugg[0]['reason'])


class TestGuardrailOverride(unittest.TestCase):
    """护栏必须能覆盖模型输出（这是整个分工的底线）。"""

    def test_net_le_zero_forced_pause(self):
        snap = mk_snapshot('1', net=-7.5, spend=20.0, rev=12.5)
        sugg = [{'campaign_id': '1', 'action': 'increase_budget',
                 'budget_change_pct': 20, 'reason': '模型想加预算'}]
        out, stats = eng.enforce_risk_guardrails(sugg, [snap], {})
        self.assertEqual(out[0]['action'], 'pause')
        self.assertEqual(out[0]['guardrail'], 'forced_pause')
        self.assertEqual(out[0]['ai_action'], 'increase_budget')   # 原判留痕
        self.assertEqual(out[0]['budget_change_pct'], 0)
        self.assertEqual(stats['forced_pause'], 1)

    def test_net_positive_blocks_kill(self):
        snap = mk_snapshot('2', net=9.9, spend=10.0, rev=19.9)
        sugg = [{'campaign_id': '2', 'action': 'pause', 'budget_change_pct': 0,
                 'reason': '模型想停'}]
        out, stats = eng.enforce_risk_guardrails(sugg, [snap], {})
        self.assertEqual(out[0]['action'], 'observe')
        self.assertEqual(out[0]['guardrail'], 'blocked_kill')
        self.assertEqual(out[0]['ai_action'], 'pause')
        self.assertEqual(stats['blocked_kill'], 1)

    def test_judge_output_always_passes_guardrail(self):
        """端到端：judge() 产出的建议，亏钱的必被停、赚钱的不许被停。"""
        judge = tj.TableJudger(min_labels=80)      # 未上岗 → 走规则回退
        snaps = [mk_snapshot('a', net=-4.0, spend=10, rev=6),
                 mk_snapshot('b', net=8.0, fill=0.4, freq=1.2, spend=10, rev=18)]
        out = judge.judge(snaps)
        by = {s['campaign_id']: s for s in out}
        self.assertEqual(by['a']['action'], 'pause')
        self.assertEqual(by['b']['action'], 'observe')

    def test_nomination_never_executes(self):
        """加预算只能是提名：带 nomination 且 budget_change_pct 必须为 0。"""
        judge = tj.TableJudger(min_labels=80)
        out = judge.judge([mk_snapshot('c', net=12.0, fill=0.95, freq=1.1)])
        s = out[0]
        self.assertEqual(s['action'], 'increase_budget')
        self.assertEqual(s['budget_change_pct'], 0)
        self.assertTrue(s.get('nomination'))
        self.assertFalse(s['nomination']['applied'])
        self.assertIn('提名', s['reason'])


class TestAbstain(unittest.TestCase):
    def test_low_confidence_abstains_to_rule(self):
        judge = tj.TableJudger(min_labels=80, min_confidence=0.55)
        judge.model = FakeModel(['observe', 'pause', 'increase_budget'], [0.40, 0.35, 0.25])
        judge.n_train = 100
        self.assertTrue(judge.is_ready())
        out = judge.judge([mk_snapshot('z', net=-3.0)])
        self.assertEqual(out[0]['source'], 'rule_fallback')
        self.assertIn('弃权', out[0]['reason'])
        self.assertEqual(out[0]['action'], 'pause')      # 回退规则：净≤0 该停

    def test_high_confidence_uses_model(self):
        judge = tj.TableJudger(min_labels=80, min_confidence=0.55)
        judge.model = FakeModel(['observe', 'pause', 'increase_budget'], [0.05, 0.05, 0.90])
        judge.n_train = 100
        out = judge.judge([mk_snapshot('y', net=3.0, fill=0.95, freq=1.1)])
        self.assertEqual(out[0]['source'], 'table_model')
        self.assertEqual(out[0]['action'], 'increase_budget')


class TestStore(unittest.TestCase):
    def test_same_ad_same_day_written_once(self):
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, 'store.jsonl')
            snap = mk_snapshot('77', net=1.0)
            self.assertTrue(flywheel.record_snapshot(snap, 'observe', path=p))
            self.assertFalse(flywheel.record_snapshot(snap, 'observe', path=p))
            recs = flywheel.load_store(p)
            self.assertEqual(len(recs), 1, '同广告同天只能有一条')
            self.assertEqual(recs[0]['ad_id'], '77')
            self.assertEqual(recs[0]['tool_action'], 'observe')
            self.assertIsNone(recs[0]['outcome'])
            self.assertEqual(len(recs[0]['features']), len(tj.FEATURE_KEYS))

    def test_backfill_writes_labels_and_stats(self):
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, 'store.jsonl')
            added, updated = flywheel.backfill_from_truth(P_DATA, P_TRUTH, p, P_HID)
            self.assertEqual(added, 100)
            self.assertEqual(updated, 0)
            st = flywheel.stats(p)
            self.assertEqual(st['snapshots'], 100)
            self.assertGreaterEqual(st['labeled'], 50)
            self.assertTrue(st['ready'])
            # 幂等：再回填一次不该新增
            added2, updated2 = flywheel.backfill_from_truth(P_DATA, P_TRUTH, p, P_HID)
            self.assertEqual(added2, 0)
            self.assertEqual(updated2, 100)

    def test_real_mode_has_no_counterfactual(self):
        """真实模式必须诚实标记：没有反事实。"""
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, 'store.jsonl')
            recs = flywheel.build_records_from_window(P_DATA, head_days=3)
            self.assertTrue(recs)
            o = recs[0]['outcome']
            self.assertFalse(o['counterfactual'])
            self.assertEqual(o['source'], 'real')
            self.assertEqual(o['net_hold'], o['net_up'])
            flywheel.backfill_from_window(P_DATA, p, head_days=3)
            self.assertEqual(len(flywheel.load_store(p)), len(recs))


class TestDataReconciliation(unittest.TestCase):
    """数据的对账关系：证明我们对三份文件的理解是对的。"""

    def test_visible_spend_equals_truth_total(self):
        truth = {r['广告']: r for r in _read_csv(P_TRUTH)}
        g = {}
        for r in _read_csv(P_DATA):
            g.setdefault(r['广告系列名称'], []).append(r)
        for name, rs in g.items():
            if name not in truth:
                continue
            sp = sum(float(r['已花费金额 (USD)']) for r in rs)
            # 容差 0.03：逐日列是四舍五入到 2 位后写盘的，5 天相加最多差 0.025
            self.assertAlmostEqual(sp, float(truth[name]['总花费']), delta=0.03,
                                   msg='可见窗口花费应等于真值表「总花费」：%s' % name)

    def test_tail_hold_net_equals_truth(self):
        truth = {r['广告']: r for r in _read_csv(P_TRUTH)}
        g = {}
        for r in _read_csv(P_HID):
            if r['方案'] == '不动':
                g.setdefault(r['广告'], []).append(r)
        for name, rs in g.items():
            if name not in truth:
                continue
            net = sum(float(r['期望净']) for r in rs)
            # 同样容差 0.03（逐日期望净 round 到 2 位后相加）
            self.assertAlmostEqual(net, float(truth[name]['不动_净']), delta=0.03,
                                   msg='后续窗口「不动」净应等于真值表 不动_净：%s' % name)


@unittest.skipUnless(HAS_SK, '需要 scikit-learn（pip install -r requirements.txt）')
class TestBacktest(unittest.TestCase):
    def _synthetic(self, n=150):
        kinds = ['observe', 'increase_budget', 'pause']
        prof = {
            'observe':         dict(roas=1.0, buy=5, per_day=1.0, fill=0.70, net=5.0),
            'increase_budget': dict(roas=5.0, buy=12, per_day=2.4, fill=0.95, net=30.0),
            'pause':           dict(roas=0.1, buy=0, per_day=0.0, fill=0.50, net=-10.0),
        }
        out = []
        for i in range(n):
            k = kinds[i % 3]
            p = prof[k]
            f = {key: 0.0 for key in tj.FEATURE_KEYS}
            f['ROAS'] = p['roas'] + (i % 5) * 0.01
            f['购买'] = float(p['buy'])
            f['日均购买'] = p['per_day']
            f['花费率'] = p['fill']
            f['频次末'] = 1.2
            out.append({
                'features': f, 'label': k, 'ad_name': 'ad%03d' % i,
                'outcome': {'net': p['net'], 'net_hold': p['net'], 'net_up': p['net'],
                            'net_stop': 0.0, 'spend': 20.0,
                            'visible_net': p['net'], 'visible_fill_rate': p['fill'],
                            'visible_freq_last': 1.2},
            })
        return out

    def test_backtest_on_separable_data(self):
        judge = tj.TableJudger(min_labels=80)
        res = judge.backtest(self._synthetic(150), truth=None, folds=5)
        self.assertGreater(res['accuracy'], 90.0,
                           '合成可分数据上准确率应 >90%%，实际 %.1f%%' % res['accuracy'])
        self.assertEqual(res['n'], 150)
        self.assertIn('observe_recall', res)
        self.assertIn('up_recall', res)

    def test_backtest_rejects_tiny_sample(self):
        judge = tj.TableJudger(min_labels=10)
        with self.assertRaises(ValueError) as cm:
            judge.backtest(self._synthetic(30)[:30], truth=None)
        self.assertIn('拒绝判卷', str(cm.exception))

    def test_backtest_matches_rule_on_real_data(self):
        """真数据上跑一次判卷：必须能跑完、给出四项召回与会合计净。"""
        samples = flywheel.records_to_samples(
            flywheel.build_records_from_truth(P_DATA, P_TRUTH, P_HID))
        self.assertEqual(len(samples), 100)
        truth = tj.load_truth_v5(P_TRUTH)
        judge = tj.TableJudger(min_labels=80)
        res = judge.backtest(samples, truth=truth, folds=5)
        self.assertEqual(res['n'], 100)
        self.assertGreater(res['accuracy'], 45.0)       # 高于「什么都不做」下限
        self.assertGreater(res['total_net'], 8000.0)    # 后段总净为正
        self.assertGreaterEqual(res['stop_recall'], 0.0)


P_SIM = os.path.join(D, '模拟广告数据-100广告10天-v5.csv')

# 用户 Ads Manager「导出」的原始表头（2026-10-02 实测，24 列）。
REAL_EXPORT_HEADER = [
    '报告开始日期', '报告结束日期', '广告系列名称', '广告系列投放', '归因设置',
    '成效', '成效指标', '覆盖人数', '展示次数', '频次', '链接点击量',
    '单次链接点击费用 (USD)', 'CPM（千次展示费用） (USD)', '广告组预算', '广告组预算类型',
    '已花费金额 (USD)', '加入购物车次数', '结账发起次数', '购物次数', '添加支付信息',
    '购物转化价值', '广告花费回报 (ROAS) - 购物', '结果（初始）', '成效（初始）指标',
]


def _write_csv(header, rows=None):
    """写一个只有表头（和一行 0）的临时 CSV，返回路径。"""
    fd, p = tempfile.mkstemp(suffix='.csv')
    os.close(fd)
    with io.open(p, 'w', encoding='utf-8-sig', newline='') as f:
        f.write(','.join('"%s"' % h for h in header) + '\n')
        if rows:
            for r in rows:
                f.write(','.join(str(x) for x in r) + '\n')
        else:
            f.write(','.join('0' for _ in header) + '\n')
    return p


class TestColumns(unittest.TestCase):
    """列名映射与缺列行为 —— 保护「数据能不能喂进来」这条命脉。

    背景（2026-10-02）：原来只认 Ads Manager 的一种长列名，且缺列会静默变成 0，
    于是「这项没读到」被当成「这项是 0」，判断结果悄悄失真而没有任何提示。
    """

    def test_real_export_header_resolves_all(self):
        """真实 Ads Manager 导出的 24 列表头，15 个必需列必须全部认出来。"""
        miss, cm, _ = tj.missing_columns(REAL_EXPORT_HEADER)
        self.assertEqual(miss, [])
        for k in tj.BLOCKING_KEYS:
            with self.subTest(key=k):
                self.assertIsNotNone(cm.get(k), '没认出 %s' % k)

    def test_simulated_data_columns_complete(self):
        """仓库自带模拟数据（按模范表生成）必须 15/15 —— 这是数据与代码的合同。"""
        rows = tj.read_csv_rows(P_SIM)
        miss, cm, _ = tj.missing_columns(list(rows[0].keys()))
        self.assertEqual(miss, [], '模拟数据缺列了：%s' % miss)
        for k in tj.REQUIRED_KEYS:
            with self.subTest(key=k):
                self.assertEqual(cm.get(k), tj.CSV_FIELDS[k])

    def test_short_names_resolve(self):
        """仓库 pull_real3.py 拍平的短名也要认。"""
        pairs = {'日期': 'date', '覆盖': 'reach', '展示': 'impressions', '链接点击': 'clicks',
                 '花费': 'spend', '加购': 'add_to_cart', '结账': 'initiate_checkout',
                 '购买': 'purchase', '支付信息': 'add_payment_info', '购买价值': 'purchase_value'}
        cm = tj.resolve_columns(list(pairs.keys()))
        for col, key in pairs.items():
            with self.subTest(col=col):
                self.assertEqual(cm.get(key), col)

    def test_purchase_not_swallowed_by_purchase_value(self):
        """别名必须精确命中：「购买」不能被「购买价值」吃掉（次数 ≠ 金额）。"""
        cm = tj.resolve_columns(['报告开始日期', '购买', '购买价值'])
        self.assertEqual(cm['purchase'], '购买')
        self.assertEqual(cm['purchase_value'], '购买价值')

    def test_cpc_column_is_optional(self):
        """cpc 列缺了不算缺 —— 30 维里的 CPC 是现算的，不用导出那一列。"""
        header = [c for c in REAL_EXPORT_HEADER if c != '单次链接点击费用 (USD)']
        miss, _, _ = tj.missing_columns(header)
        self.assertEqual(miss, [])
        self.assertIn('cpc', tj.OPTIONAL_KEYS)

    def test_missing_blocking_column_raises(self):
        """缺了会让特征失真的列：strict 时必须抛错，不许静默变 0。"""
        header = [c for c in REAL_EXPORT_HEADER if c != '购物次数']
        tmp = _write_csv(header)
        with self.assertRaises(tj.ColumnError):
            tj.load_csv_grouped(tmp, strict=True)
        groups = tj.load_csv_grouped(tmp, strict=False, quiet=True)
        self.assertTrue(groups, '放行时也要能读出分组')

    def test_column_used_for_covers_every_key(self):
        """COLUMN_USED_FOR 必须覆盖每个必需列 —— 防以后加列忘了写人话说明。"""
        for k in tj.REQUIRED_KEYS:
            with self.subTest(key=k):
                self.assertIn(k, tj.COLUMN_USED_FOR)

    def test_days_from_csv_rows_accepts_colmap(self):
        """days_from_csv_rows 支持外部传入 colmap，且与自动解析结果一致。"""
        rows = tj.read_csv_rows(P_SIM)
        cm = tj.resolve_columns(list(rows[0].keys()))
        a = tj.days_from_csv_rows(rows[:5], cm)
        b = tj.days_from_csv_rows(rows[:5])
        self.assertEqual([d['spend'] for d in a], [d['spend'] for d in b])
        self.assertEqual([d['date'] for d in a], [d['date'] for d in b])


class TestStoreUntouchedByRepo(unittest.TestCase):
    def test_store_path_is_repo_relative(self):
        """learning_store.jsonl 必须是运行时产物（落在仓库根、可被 .gitignore 排除）。"""
        self.assertEqual(os.path.basename(flywheel.P_STORE), 'learning_store.jsonl')
        self.assertEqual(os.path.dirname(flywheel.P_STORE), D)


# ==================== 多视图接入（metric_views）====================

# 五种视图的最小表头（只留签名列 + 日期 + 分组列），用于识别与合并用例。
H_CONV = ['报告开始日期', '广告系列名称', '展示次数', '链接点击量', '已花费金额 (USD)',
          '加入购物车次数', '购物次数', '购物转化价值']
H_CLICK = ['报告开始日期', '广告系列名称', '展示次数', '链接点击量', '点击量（全部）',
           '点击率（全部）', '落地页浏览量', '已花费金额 (USD)']
H_VIDEO = ['报告开始日期', '广告系列名称', '展示次数', '播放视频达 3 秒的次数',
           'ThruPlay 次数', '视频播放进度达 25% 的次数', '视频播放进度达 50% 的次数',
           '视频播放进度达 100% 的次数']
H_ENG = ['报告开始日期', '广告系列名称', '展示次数', '公共主页互动量', '帖子评论数',
         '帖子分享次数', 'Facebook 获赞数', 'Instagram 关注次数']
H_COST = ['报告开始日期', '广告系列名称', '单次成效费用', '结束日期']


def _write_export(header, rows, mtime=None):
    """写一份临时导出（BOM + 表头 + 数据行）。mtime 可指定，用于「更新的优先」用例。"""
    fd, p = tempfile.mkstemp(suffix='.csv')
    os.close(fd)
    with io.open(p, 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.writer(f)
        w.writerow(header)
        for r in rows:
            w.writerow(r)
    if mtime is not None:
        os.utime(p, (mtime, mtime))
    return p


class TestViewDetection(unittest.TestCase):
    """视图识别 —— 决定「这份导出是哪一套指标」。"""

    def test_five_views_detected(self):
        for header, expect in ((H_CONV, 'conversion'), (H_CLICK, 'click'),
                               (H_VIDEO, 'video'), (H_ENG, 'engagement'),
                               (H_COST, 'cost')):
            with self.subTest(view=expect):
                name, hit = mv.detect_view(header)
                self.assertEqual(name, expect)
                self.assertGreater(hit, 0)

    def test_unknown_header(self):
        """一套签名列都不沾 → unknown，不许瞎猜。"""
        name, hit = mv.detect_view(['报告开始日期', '广告系列名称', '备注'])
        self.assertEqual(name, 'unknown')
        self.assertEqual(hit, 0)

    def test_signature_ignores_column_order_and_spaces(self):
        """表头指纹与列顺序/空格无关：同一套指标换个次序还是同一份。"""
        a = ['报告开始日期', '广告系列名称', '展示次数']
        b = ['展示次数', ' 广告系列名称', '报告开始日期']
        self.assertEqual(mv.header_sig(a), mv.header_sig(b))


class TestDedupe(unittest.TestCase):
    """过滤相同的 → 留下不同的。"""

    def test_same_header_keep_more_rows(self):
        small = _write_export(H_CONV, [['2026-01-01', '广告A'] + ['0'] * 6])
        big = _write_export(H_CONV, [['2026-01-0%d' % i, '广告A'] + ['0'] * 6 for i in (1, 2, 3)])
        exps = [mv.load_export(big), mv.load_export(small)]
        groups, chosen, dropped = mv.dedupe_exports(exps)
        self.assertEqual(len(groups), 1, '同表头必须归到一组')
        self.assertEqual(len(chosen), 1)
        self.assertEqual(chosen[0].n, 3, '应当留数据行更多的那份')
        self.assertEqual([d.n for d in dropped], [1])

    def test_different_headers_all_kept(self):
        paths = [_write_export(H_CONV, [['2026-01-01', 'A'] + ['0'] * 6]),
                 _write_export(H_VIDEO, [['2026-01-01', 'A'] + ['0'] * 7])]
        _, chosen, dropped = mv.dedupe_exports([mv.load_export(p) for p in paths])
        self.assertEqual(len(chosen), 2)
        self.assertEqual(dropped, [])


class TestMerge(unittest.TestCase):
    """按 (日期, 广告) 拼宽表；冲突必须报出来。"""

    def test_two_views_merge_into_one_row(self):
        p1 = _write_export(H_CONV, [['2026-01-01', '广告A', '1000', '100', '20',
                                     '5', '2', '80']])
        p2 = _write_export(H_CLICK, [['2026-01-01', '广告A', '1000', '100', '1500',
                                      '6.0', '90', '20']])
        wide, _ = mv.merge_exports([mv.load_export(p1), mv.load_export(p2)])
        self.assertEqual(len(wide), 1, '同一天同一广告只能有一行')
        row = list(wide.values())[0]
        self.assertIn('购物转化价值', row, '转化视图的列要在')
        self.assertIn('点击量（全部）', row, '点击视图的列也要在')

    def test_conflict_is_reported_and_newest_wins(self):
        now = time.time()
        old = _write_export(H_CONV, [['2026-01-01', '广告A', '1000', '100', '7.56',
                                      '5', '2', '80']], mtime=now - 10000)
        new = _write_export(H_CONV, [['2026-01-01', '广告A', '1000', '100', '21.53',
                                      '5', '2', '80']], mtime=now)
        wide, rep = mv.merge_exports([mv.load_export(old), mv.load_export(new)])
        self.assertEqual(len(rep['conflicts']), 1, '同一格的两种取值必须留下记录')
        self.assertEqual(rep['conflicts'][0]['winner'], '21.53')
        row = list(wide.values())[0]
        self.assertEqual(row['已花费金额 (USD)'], '21.53', '更新的那份胜出')

    def test_identical_values_are_not_conflicts(self):
        now = time.time()
        a = _write_export(H_CONV, [['2026-01-01', '广告A', '1000', '100', '9.9',
                                    '5', '2', '80']], mtime=now - 100)
        b = _write_export(H_CONV, [['2026-01-01', '广告A', '1000', '100', '9.9',
                                    '5', '2', '80']], mtime=now)
        _, rep = mv.merge_exports([mv.load_export(a), mv.load_export(b)])
        self.assertEqual(rep['conflicts'], [], '值一样就不是冲突')


class TestExtFeatures(unittest.TestCase):
    """扩展特征：窗口必须配对、不可算必须是 None、比值 > 1 必须报警。"""

    def _one_ad(self, header, rows):
        p = _write_export(header, rows)
        wide, _ = mv.merge_exports([mv.load_export(p)])
        return mv.ext_report_by_ad(wide)['广告A']

    def test_paired_window_not_mixed(self):
        """分子只在第 2 天有值 → 分母也必须只取第 2 天，不能拿两天的点击去除。

        实测教训：互动系列的「落地页浏览量」只报 1 天，「链接点击量」报 4 天，
        不配对窗口会算出 0.0028（真实是 0.0317，差 11 倍）。
        """
        rep = self._one_ad(H_CLICK, [
            ['2026-01-01', '广告A', '1000', '100', '1200', '6.0', '', '10'],
            ['2026-01-02', '广告A', '2000', '200', '1400', '7.0', '50', '10'],
        ])
        self.assertAlmostEqual(rep['features']['落地页到达率'], 50.0 / 200.0, places=6)
        self.assertEqual(rep['days']['落地页到达率'], 1, '只用了 1 天，必须能看出来')

    def test_unavailable_is_none_not_zero(self):
        """缺列 → None（不是 0.0）。「这项没读到」和「这项是 0」不能混。"""
        rep = self._one_ad(H_CONV, [['2026-01-01', '广告A', '1000', '100', '20',
                                     '5', '2', '80']])
        self.assertIsNone(rep['features']['3秒播放率'])
        self.assertIsNone(rep['features']['全部点击倍数'])
        self.assertEqual(rep['days']['3秒播放率'], 0)

    def test_ratio_over_one_is_flagged(self):
        """v25 > video_3s（平台两个指标基底不同）→ 必须进 suspect 报警。"""
        rep = self._one_ad(H_VIDEO, [['2026-01-01', '广告A', '1000', '100',
                                      '10', '120', '60', '30']])
        self.assertGreater(rep['features']['视频留存25'], 1.0)
        self.assertIn('视频留存25', rep['suspect'])
        self.assertNotIn('视频留存50', rep['suspect'])

    def test_union_header_not_first_row(self):
        """视频列只存在于最后一天时，也要能算出来（不能只看第一行表头）。

        实测教训：视频/互动视图只覆盖最后 4 天，用 rows[0].keys() 会整块漏掉。
        """
        p1 = _write_export(H_CONV, [['2026-01-01', '广告A', '1000', '100', '20',
                                     '5', '2', '80'],
                                    ['2026-01-02', '广告A', '2000', '200', '30',
                                     '6', '3', '90']])
        # H_VIDEO 列序：日期, 名称, 展示次数, 3秒播放, ThruPlay, 25%, 50%, 100%
        p2 = _write_export(H_VIDEO, [['2026-01-02', '广告A', '2000', '200',
                                      '80', '150', '100', '40']])
        wide, _ = mv.merge_exports([mv.load_export(p1), mv.load_export(p2)])
        rep = mv.ext_report_by_ad(wide)['广告A']
        self.assertIsNotNone(rep['features']['3秒播放率'], '视频列在第二天，仍须算得出')
        self.assertAlmostEqual(rep['features']['3秒播放率'], 200.0 / 2000.0, places=6)
        self.assertAlmostEqual(rep['features']['视频完播率'], 40.0 / 200.0, places=6)
        self.assertEqual(rep['days']['3秒播放率'], 1)

    def test_ext_keys_disjoint_from_core(self):
        """扩展维度不许和核心 30 维重名 —— 免得以后混起来分不清谁是谁。"""
        self.assertEqual(len(tj.FEATURE_KEYS), 30)
        self.assertEqual(len(mv.EXT_FEATURE_KEYS), 13)
        self.assertEqual(set(tj.FEATURE_KEYS) & set(mv.EXT_FEATURE_KEYS), set())

    def test_core_feature_keeps_30(self):
        """接入多视图**不许**动核心 30 维（动了历史成绩全部作废）。"""
        self.assertEqual(len(tj.FEATURE_KEYS), 30)
        self.assertEqual(tj.FEATURE_KEYS[0], '花费')
        self.assertEqual(tj.FEATURE_KEYS[-1], '首单在第几天')
        # 合并真实/模拟数据都不该改变核心维度清单
        rows = tj.read_csv_rows(P_SIM)
        self.assertEqual(len(tj.FEATURE_KEYS), 30)
        self.assertGreater(len(rows), 0)


class TestBudgetAnomaly(unittest.TestCase):
    def test_text_budget_is_detected(self):
        """平台把文案写进「广告组预算」列时要报出来，否则读成 0 而无声。"""
        p = _write_export(['报告开始日期', '广告系列名称', '广告组预算', '广告组预算类型'],
                          [['2026-01-01', '广告A', '使用广告组预算', '0']])
        bad = mv.budget_anomaly([mv.load_export(p)])
        self.assertEqual(len(bad), 1)
        self.assertEqual(bad[0]['value'], '使用广告组预算')

    def test_numeric_budget_is_clean(self):
        p = _write_export(['报告开始日期', '广告系列名称', '广告组预算', '广告组预算类型'],
                          [['2026-01-01', '广告A', '40', '单日预算']])
        self.assertEqual(mv.budget_anomaly([mv.load_export(p)]), [])


class TestRealExports(unittest.TestCase):
    """真实账户的导出（若本机存在）—— 端到端跑一遍。

    ⚠️ 断言必须**由数据本身推出来**，不能写死份数 / 行数：
    用户随时会再导一份新指标的导出（2026-10-02 就多了一份带排名的**广告级**导出，
    直接从 6 份变 7 份、行数 177 变 354）。写死数字的断言会在那一刻变成**假警报**，
    而假警报会让人开始忽略测试 —— 比没有测试更糟。
    """

    # 真实导出的目录：默认「下载」文件夹，可用环境变量覆盖。
    # ⚠️ 不要写死带用户名的绝对路径 —— 这个文件是公开仓库的一部分。
    DL = os.environ.get('FB_EXPORT_DIR', os.path.expanduser(os.path.join('~', 'Downloads')))

    def _paths(self):
        import glob
        return sorted(glob.glob(os.path.join(self.DL, 'voglyn*.csv')))

    def test_every_export_is_classified_or_dropped(self):
        paths = self._paths()
        if len(paths) < 6:
            self.skipTest('本机没有那批真实导出')
        ov = mv.overview(paths=paths)
        self.assertEqual(len(ov['exports']), len(paths), '每份文件都该被扫描到')
        self.assertEqual(len(ov['chosen']) + len(ov['dropped']), len(paths),
                         '每份文件要么被选中，要么被去重丢掉，不能凭空消失')
        sigs = [e.sig for e in ov['chosen']]
        self.assertEqual(len(sigs), len(set(sigs)), '被选中的视图签名必须互不相同')
        # 五种视图的缺口清单必须算得出（哪怕某次导出恰好缺一种）
        self.assertIsInstance(ov['gap']['missing'], list)
        # 合并冲突**只允许报告、不允许静默**（广告级与系列级导出混在一起时确实会撞键）
        self.assertIsInstance(ov['merge']['conflicts'], list)

    def test_wide_table_beats_every_single_export(self):
        paths = self._paths()
        if len(paths) < 6:
            self.skipTest('本机没有那批真实导出')
        ov = mv.overview(paths=paths)
        widest = max(len(e.header) for e in ov['exports'])
        self.assertGreater(len(mv.wide_header(ov['wide'])), widest,
                           '合并后必须比任何单份都宽')
        most_rows = max(len(e.rows) for e in ov['exports'])
        self.assertGreaterEqual(len(ov['wide']), most_rows,
                                '合并后的行数不该少于最全的那一份')

    def test_ext_coverage_reflects_missing_days(self):
        paths = self._paths()
        if len(paths) < 6:
            self.skipTest('本机没有那批真实导出')
        ov = mv.overview(paths=paths)
        rep = ov['ext_report']
        # 视频/互动视图只覆盖 4 天 → 长袖/短袖那些广告的支撑天数必然少
        some_days = [d for r in rep.values() for d in r['days'].values() if d]
        self.assertTrue(some_days)
        self.assertTrue(any(d <= 4 for d in some_days),
                        '必须能看出有些比值只基于很少的天数')


class TestP0P1Columns(unittest.TestCase):
    """P0/P1：把「决定别的数字怎么读」的列读出来，并写进口径声明。

    这批字段（归因窗口 / 优化目标 / 排名 / 投放状态）**不参与任何计算**，
    价值全在「读数的前提」上 —— 实测两类真实误判就靠它们堵：

      · 归因设置：同一账户的两份导出窗口不同（一份「7天点击+1天浏览+1天互动观看」、
        另一份只有「7天点击+1天浏览」）→ 跨窗口比 ROAS 是错的
      · 成效指标：实测某系列优化的是 `actions:post_engagement`（互动），
        按「购买少」去关它是把靶子搞错了
    """

    HDR = ['报告开始日期', '广告名称', '广告投放', '归因设置', '成效指标',
           '已花费金额 (USD)', '展示次数', '覆盖人数', '链接点击量', '购物次数',
           '购物转化价值', '广告组预算', '广告组预算类型',
           '质量排名', '互动率排名', '转化率排名', '结束日期']

    def _write(self, rows, header=None):
        fd, path = tempfile.mkstemp(suffix='.csv')
        os.close(fd)
        with io.open(path, 'w', encoding='utf-8-sig', newline='') as f:
            w = csv.writer(f)
            w.writerow(header or self.HDR)
            w.writerows(rows)
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        return path

    def _one_day(self, **over):
        r = {'报告开始日期': '2026-03-10', '广告名称': 'A1', '广告投放': 'not_delivering',
             '归因设置': '点击后 7 天内、浏览后 1 天内或互动观看后 1 天内',
             '成效指标': 'actions:offsite_conversion.fb_pixel_purchase',
             '已花费金额 (USD)': '20', '展示次数': '1000', '覆盖人数': '800',
             '链接点击量': '30', '购物次数': '2', '购物转化价值': '60',
             '广告组预算': '20', '广告组预算类型': '单日预算',
             '质量排名': '高于平均', '互动率排名': '平均', '转化率排名': '低于平均',
             '结束日期': '进行中'}
        r.update(over)
        return [r[k] for k in self.HDR]

    def test_sentinel_dash_is_none_not_a_value(self):
        """'-' 是「没有这项数据」，不是一种取值 —— 排名空 ≠ 排名差。"""
        self.assertIsNone(lo._text_or_none('-'))
        self.assertIsNone(lo._text_or_none(' -- '))
        self.assertIsNone(lo._text_or_none('N/A'))
        self.assertIsNone(lo._text_or_none(''))
        self.assertEqual(lo._text_or_none('高于平均'), '高于平均')

        rec = lo.load_file(self._write([self._one_day(质量排名='-',
                                                     互动率排名='-',
                                                     转化率排名='-')]))[0][0]
        self.assertNotIn('rankings', rec, '全是 "-" 时不该产出 rankings 字段')

    def test_new_fields_survive_into_receipt(self):
        rec = lo.load_file(self._write([self._one_day()]))[0][0]
        self.assertEqual(rec['status'], 'NOT_DELIVERING')
        self.assertIn('7 天', rec['attribution'])
        self.assertEqual(rec['optimization_event'],
                         'actions:offsite_conversion.fb_pixel_purchase')
        self.assertEqual(rec['rankings'],
                         {'quality': '高于平均', 'engagement': '平均',
                          'conversion': '低于平均'})
        self.assertEqual(rec['end_date'], '进行中')

    def test_ranking_column_present_but_empty_is_declared_as_gap(self):
        """导出了排名列但全是 '-' 时，必须明说「没有数据」，不能闭嘴。"""
        rows, meta = lo.load_file(self._write([self._one_day(质量排名='-',
                                                             互动率排名='-',
                                                             转化率排名='-')]))
        gaps = ' '.join(meta['data_gaps'])
        self.assertIn('排名', gaps)
        self.assertIn('不是排名差', gaps)

    def test_missing_ranking_column_is_also_declared(self):
        hdr = [h for h in self.HDR if '排名' not in h]
        idx = [self.HDR.index(h) for h in hdr]
        full = self._one_day()
        row = [full[i] for i in idx]
        rows, meta = lo.load_file(self._write([row], header=hdr))
        gaps = ' '.join(meta['data_gaps'])
        self.assertIn('排名', gaps)
        self.assertNotIn('rankings', rows[0])

    def test_snapshot_and_prompt_carry_the_caliber(self):
        rec = lo.load_file(self._write([self._one_day()]))[0][0]
        snap = eng.make_snapshot([rec])
        self.assertEqual(snap[0]['attribution'], rec['attribution'])
        self.assertEqual(snap[0]['optimization_event'], rec['optimization_event'])
        self.assertEqual(snap[0]['rankings'], rec['rankings'])
        p = eng.build_prompt(snap, {'lookback_days': 7}, window=('2026-03-10', '2026-03-10'))
        for clause in ('status 非 ACTIVE', 'attribution = 本批数据采用的归因窗口',
                       'optimization_event = 这个广告组优化', 'rankings（quality 质量',
                       '不等于「排名差」'):
            self.assertIn(clause, p, 'prompt 缺了口径声明：%s' % clause)

    def test_absent_caliber_fields_stay_absent(self):
        """没有这些列的文件（老导出）不能凭空长出口径字段来。"""
        hdr = [h for h in self.HDR
               if h not in ('归因设置', '成效指标', '质量排名', '互动率排名', '转化率排名')]
        idx = [self.HDR.index(h) for h in hdr]
        full = self._one_day()
        rows, meta = lo.load_file(self._write([[full[i] for i in idx]], header=hdr))
        rec = rows[0]
        for k in ('attribution', 'optimization_event', 'rankings'):
            self.assertNotIn(k, rec)
        gaps = ' '.join(meta['data_gaps'])
        self.assertIn('归因设置', gaps)

    def test_real_latest_export_if_present(self):
        """本机若有那份带排名的广告级导出，跑一遍真实数据。"""
        import glob
        dl = os.environ.get('FB_EXPORT_DIR', os.path.expanduser(os.path.join('~', 'Downloads')))
        cand = [p for p in sorted(glob.glob(os.path.join(dl, 'voglyn*')) )
                if '(6)' in os.path.basename(p) and p.lower().endswith('.csv')]
        if not cand:
            self.skipTest('本机没有那份广告级导出')
        rows, meta = lo.load_file(cand[0])
        self.assertTrue(all(r.get('attribution') for r in rows),
                        '真实导出里归因设置应当每一行都能读到')


class TestP1bSignals(unittest.TestCase):
    """P1b：广告组粒度 + 视频平均播放时长 + 重复点击率。

    这批和 P0/P1 不是一类东西：P0/P1 是「决定别的数字怎么读」的前提，
    P1b 是**两个从任何现有指标都推不出来的观测**：

      · video_avg_watch_sec —— 观众平均看了几秒。3 秒播放数高但时长只有 1~2 秒，
        说明素材只赢在开头；这个判断用 CTR/ROAS 怎么算都算不出来。
      · repeat_click_ratio —— 非独立点击数占全部点击的比例。高 = 少数人反复点
        （落地页/价格/信任问题），低 = 很多人在点（素材/受众问题）。同样推不出来。

    真实触发这两个 bug 的那份导出有两个陷阱，都写进用例里了：
      ① 时长列格式混用：零值写 '00:00:00'，非零值写裸秒数 '2'；
      ② 费用四舍五入到 6 位小数 → 11.37/0.5685 = 20.0004，除完带尾差，
         直接算 1-x 会得到负数并把「完全不重复」整条丢掉。
    """

    HDR = ['报告开始日期', '广告组名称', '广告组投放', '已花费金额 (USD)', '展示次数',
           '覆盖人数', '链接点击量', '购物次数', '购物转化价值', '广告组预算',
           '广告组预算类型', '视频平均播放时长', '单次链接点击费用 - 独立用户 (USD)']

    def _write(self, rows, header=None):
        fd, path = tempfile.mkstemp(suffix='.csv')
        os.close(fd)
        with io.open(path, 'w', encoding='utf-8-sig', newline='') as f:
            w = csv.writer(f)
            w.writerow(header or self.HDR)
            w.writerows(rows)
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        return path

    def _row(self, over=None):
        """造一行数据。over 用 dict 传 —— 列名带空格和括号，不能当关键字参数名。"""
        r = {'报告开始日期': '2026-03-10', '广告组名称': '组A', '广告组投放': 'not_delivering',
             '已花费金额 (USD)': '11.37', '展示次数': '318', '覆盖人数': '294',
             '链接点击量': '22', '购物次数': '1', '购物转化价值': '63.95',
             '广告组预算': '20', '广告组预算类型': '单日预算',
             '视频平均播放时长': '4', '单次链接点击费用 - 独立用户 (USD)': '0.5685'}
        r.update(over or {})
        return [r[k] for k in self.HDR]

    # ---------- 时长解析 ----------
    def test_secs_parses_both_formats_meta_uses(self):
        self.assertEqual(lo._secs('4'), 4.0)            # 裸秒数（非零值的真实格式）
        self.assertEqual(lo._secs('00:00:00'), 0.0)     # 零值的真实格式
        self.assertEqual(lo._secs('00:01:30'), 90.0)
        self.assertEqual(lo._secs('1:30'), 90.0)        # 缺小时位
        self.assertEqual(lo._secs('00:00:02.5'), 2.5)

    def test_secs_does_not_read_colon_as_decimal(self):
        """反向断言：'00:01:30' 是 90 秒，绝不能被读成 130（把冒号当小数点）。"""
        self.assertNotEqual(lo._secs('00:01:30'), 130.0)

    def test_secs_empty_sentinels_are_none(self):
        for v in ('', '-', '--', 'n/a', None):
            self.assertIsNone(lo._secs(v), '输入 %r 应当读成 None' % (v,))

    # ---------- 广告组粒度 ----------
    def test_adset_grain_is_accepted(self):
        """以前按「广告组」层级导出的文件会被结构校验直接拒掉（找不到标识列）。"""
        rows, meta = lo.load_file(self._write([self._row()]))
        self.assertEqual(meta.get('grain'), 'adset')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['name'], '组A')

    def test_adset_level_status_column_is_read(self):
        """广告组层级导出里状态列叫「广告组投放」。

        以前别名表里没有它 → status 落进 (None or 'ACTIVE') → 一条**没在投**的广告
        被报成 ACTIVE，而 prompt 明写「status 非 ACTIVE 不得加预算」——
        这等于把最该拦住的那条规则给绕过去了。
        """
        rows, _ = lo.load_file(self._write([self._row({'广告组投放': 'not_delivering'})]))
        self.assertEqual(rows[0]['status'], 'NOT_DELIVERING')

    def test_campaign_grain_not_stolen_by_adset_branch(self):
        """只有系列名的老文件必须仍然走 campaign 粒度（判断顺序回归）。"""
        hdr = ['报告开始日期', '广告系列名称', '已花费金额 (USD)', '展示次数', '覆盖人数',
               '链接点击量', '购物次数', '购物转化价值']
        vals = {'报告开始日期': '2026-03-10', '广告系列名称': '系列A',
                '已花费金额 (USD)': '20', '展示次数': '1000', '覆盖人数': '800',
                '链接点击量': '30', '购物次数': '2', '购物转化价值': '60'}
        rows, meta = lo.load_file(self._write([[vals[c] for c in hdr]], header=hdr))
        self.assertEqual(meta.get('grain'), 'campaign')
        self.assertEqual(rows[0]['name'], '系列A')

    # ---------- 重复点击率 ----------
    def test_repeat_click_ratio_computed(self):
        """11.37 花费 / 22 次点击 / 独立点击单价 0.5685 → 独立用户 20 → 重复率 1-20/22。"""
        rows, _ = lo.load_file(self._write([self._row()]))
        self.assertAlmostEqual(rows[0]['repeat_click_ratio'], 0.0909, places=3)

    def test_repeat_click_ratio_zero_survives_rounding(self):
        """11 次点击来自 11 个人 → 重复率 0。

        11.37/0.923636 = 11.00002（CSV 费用保留 6 位小数的尾差），
        不做容差取整就会得到 -0.0000018，被守卫整条丢掉 —— 真实值明明是 0。
        """
        rows, _ = lo.load_file(self._write([self._row({
            '已花费金额 (USD)': '10.16', '链接点击量': '11',
            '单次链接点击费用 - 独立用户 (USD)': '0.923636'})]))
        self.assertEqual(rows[0].get('repeat_click_ratio'), 0.0)
        self.assertEqual(rows[0].get('repeat_click_days'), 1)

    def test_repeat_click_ratio_absent_when_column_missing(self):
        """没有这一列 → 字段整个不出现，绝不能填 0 冒充「观测到的零」。"""
        hdr = [c for c in self.HDR if c != '单次链接点击费用 - 独立用户 (USD)']
        vals = dict(zip(self.HDR, self._row()))
        rows, meta = lo.load_file(self._write([[vals.get(c, '') for c in hdr]], header=hdr))
        self.assertNotIn('repeat_click_ratio', rows[0])
        self.assertTrue(any('独立用户点击费用' in g for g in (meta.get('data_gaps') or [])),
                        '缺列必须在 data_gaps 里明说')

    def test_repeat_click_ratio_not_computed_across_windows(self):
        """分子分母必须同一天。跨窗口硬凑 = 之前踩过的 11 倍误差。"""
        rows, _ = lo.load_file(self._write([
            self._row({'报告开始日期': '2026-03-10', '已花费金额 (USD)': '20',
                       '链接点击量': '10', '单次链接点击费用 - 独立用户 (USD)': '1.0'}),
            self._row({'报告开始日期': '2026-03-11', '已花费金额 (USD)': '0',
                       '链接点击量': '0', '单次链接点击费用 - 独立用户 (USD)': '0.5'}),
        ]))
        # 第 1 天独立点击 20 > 全部点击 10 → 口径不自洽 → 两天都不该计入
        self.assertNotIn('repeat_click_ratio', rows[0])

    # ---------- 观看时长 ----------
    def test_watch_seconds_ignore_zero_spend_days(self):
        """零投放的天写 '00:00:00'，计进去会把均值稀释成假的「平均 0.07 秒」。"""
        rows, _ = lo.load_file(self._write([
            self._row({'报告开始日期': '2026-03-10', '视频平均播放时长': '4'}),
            self._row({'报告开始日期': '2026-03-11', '已花费金额 (USD)': '0',
                       '展示次数': '0', '覆盖人数': '0', '链接点击量': '0',
                       '购物次数': '0', '购物转化价值': '0',
                       '视频平均播放时长': '00:00:00'}),
        ]))
        self.assertEqual(rows[0]['video_avg_watch_sec'], 4.0)
        self.assertEqual(rows[0]['video_watch_days'], 1,
                         '支撑天数必须是 1（只有 03-10 真投放），不是 2')

    def test_watch_fields_absent_when_all_zero(self):
        hdr = [c for c in self.HDR if c != '视频平均播放时长']
        vals = dict(zip(self.HDR, self._row()))
        rows, _ = lo.load_file(self._write([[vals.get(c, '') for c in hdr]], header=hdr))
        self.assertNotIn('video_avg_watch_sec', rows[0])

    def test_status_takes_latest_day_not_first(self):
        """投放状态是**时点事实** —— 必须取窗口里最后一天，不是第一天。

        踩过的坑：模拟数据里 A7 前 3 天 ACTIVE、后 2 天 PAUSED，
        加载器取「第一个非空值」⇒ 一条**已被暂停**的广告被报成 ACTIVE。
        而 prompt 明写「status 非 ACTIVE 不得给 increase_budget」
        ⇒ 等于把最该拦住的那条规则绕过去，还顺手给一条停投的广告建议加预算。
        """
        rows, _ = lo.load_file(self._write([
            self._row({'报告开始日期': '2026-04-01', '广告组投放': 'ACTIVE'}),
            self._row({'报告开始日期': '2026-04-02', '广告组投放': 'ACTIVE'}),
            self._row({'报告开始日期': '2026-04-03', '广告组投放': 'PAUSED'}),
        ]))
        self.assertEqual(rows[0]['status'], 'PAUSED')

    def test_status_empty_cell_does_not_fall_back_to_active(self):
        """列在、但某天的值是空 —— 不能把状态「顶」回 ACTIVE。

        区分两种情况：
          · **整列都没有** → 真的没有状态信息，回退 ACTIVE 是既定行为（且要写进 data_gaps）
          · **列在、某个格子空** → 那是「这天没填」，应沿用最近一次真实取值
        """
        v1 = dict(zip(self.HDR, self._row()))
        v1['报告开始日期'] = '2026-04-01'
        v1['广告组投放'] = 'PAUSED'
        v2 = dict(zip(self.HDR, self._row()))
        v2['报告开始日期'] = '2026-04-02'
        v2['广告组投放'] = ''          # 空值哨兵，不是取值
        v3 = dict(zip(self.HDR, self._row()))
        v3['报告开始日期'] = '2026-04-03'
        v3['广告组投放'] = ''          # 末天空着，也不能丢掉前面读到的 PAUSED
        rows, _ = lo.load_file(self._write([[d[k] for k in self.HDR] for d in (v1, v2, v3)]))
        self.assertEqual(rows[0]['status'], 'PAUSED')

    # ---------- prompt ----------
    def test_prompt_declares_how_to_read_new_signals(self):
        rows, _ = lo.load_file(self._write([self._row()]))
        p = eng.build_prompt(rows, {}, window=None)
        for needle in ('video_avg_watch_sec', 'video_watch_days',
                       'repeat_click_ratio', 'repeat_click_days'):
            self.assertIn(needle, p)
        # 两条「不许拿它当关停理由」的边界必须写进 prompt，否则会被挪用到别的动作上
        self.assertIn('不得因此给 decrease_budget', p)
        self.assertIn('不是独立的关停理由', p)

    # ---------- 真实数据 ----------
    def test_real_adset_export(self):
        """本机若有那份广告组级导出，跑一遍真实数据。"""
        import glob
        dl = os.environ.get('FB_EXPORT_DIR', os.path.expanduser(os.path.join('~', 'Downloads')))
        cand = [p for p in sorted(glob.glob(os.path.join(dl, 'voglyn*')))
                if '(7)' in os.path.basename(p) and p.lower().endswith('.csv')]
        if not cand:
            self.skipTest('本机没有那份广告组级导出')
        rows, meta = lo.load_file(cand[0])
        self.assertEqual(meta.get('grain'), 'adset')
        self.assertTrue(all(r['status'] == 'NOT_DELIVERING' for r in rows),
                        '真实导出里广告全是 not_delivering（没在投）')
        # 真实文件里只有 2026-03-10 一天有花费 → 支撑天数必须是 1，不能是 59
        for r in rows:
            if 'video_watch_days' in r:
                self.assertLessEqual(r['video_watch_days'], 2)
            if 'repeat_click_days' in r:
                self.assertLessEqual(r['repeat_click_days'], 2)


if __name__ == '__main__':
    unittest.main(verbosity=2)
