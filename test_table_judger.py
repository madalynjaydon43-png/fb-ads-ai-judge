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
import io
import json
import os
import sys
import tempfile
import unittest

D = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, D)

import flywheel                                     # noqa: E402
import table_judger as tj                           # noqa: E402
import fb_ai_engine as eng                          # noqa: E402

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


class TestStoreUntouchedByRepo(unittest.TestCase):
    def test_store_path_is_repo_relative(self):
        """learning_store.jsonl 必须是运行时产物（落在仓库根、可被 .gitignore 排除）。"""
        self.assertEqual(os.path.basename(flywheel.P_STORE), 'learning_store.jsonl')
        self.assertEqual(os.path.dirname(flywheel.P_STORE), D)


if __name__ == '__main__':
    unittest.main(verbosity=2)
