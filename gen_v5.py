# -*- coding: utf-8 -*-
"""gen_v5 —— 两段式模拟数据（**受众池真实版**）

【为什么从 v4 再重做一次】
用户实测反馈（真实经验，必须照做）：
  「频次不可能 5 天就从 1.1 拉到 1.5，更不可能拉到 2.5。
    我很多广告跑几个月频次还在 1.4 左右。」
v4 的错（全是拍脑袋）：
  第114行 freq0 = uniform(1.00, 2.20)   ← 频次起点随机抽到 2.2，离谱
  第147行 freq_d = freq0 * (1 + 0.08*d) ← 频次每天固定涨 8%，等效指数膨胀
  第148行 reach  = imp / freq_d         ← 于是覆盖天天跌，展示天天一样
  另外 imp = 花费/CPM 且 CPM 每条固定 ⇒ 展示次数 2839/2839/2839… 一个不差，
  花费 40.00 一天不差 —— 这是纯合成数据的指纹。

v5 的建法（从**受众池**这个物理量长出来，不再直接编频次）：
  每条广告有真实受众池 A（人）。曝光累计 I 与触达人数 R 走**经典 reach curve**：
        R(I) = A * (1 - exp( -I / (A * f0) ))
  · 受众池 A 很大 → R 几乎线性增长 → 频次恒在 1.0~1.2，几个月不动 ✅
  · 受众池 A 很小 → R 迅速饱和 → 频次才慢慢爬到 1.5~1.9（真实的小池子就是这样）
  → **频次不再是自变量，而是受众池被消耗的副产物。** 这才对。
另外 CPM / 花费 / 交付能力都带日噪声，展示次数不再恒定。

【真值仍然是"算出来的"】
  同一广告跑两遍：A=预算不动 / B=第 6 天起预算 +20%，噪声向量完全相同
  （控制变量，否则差值全是运气）。B 版本的转化率按 α 衰减：
        α = 0.02 + 0.55 × 第10天饱和度 + 噪声
  饱和度越高（池子越小），放量越伤转化 —— 这就是"加了钱不一定更赚"的物理来源。
  正确动作 = 两版净收益比大小算出来的，不是写死的。

【产出】
  模拟广告数据-100广告10天-v5.csv   只含**前 5 天**（喂 AI 的输入）
  真值表-v5.csv                      正确动作 + 隐藏机制 + 两版净收益（判卷用）
  真值表-逐日-v5.csv                 后 5 天逐日（两版），人工查验用
  gen_v5_out.txt                     生成日志 + 真实性自检
"""
import csv
import io
import math
import os
import random
from datetime import date, timedelta

RNG = random.Random(20261003)
D = os.path.dirname(os.path.abspath(__file__))

DAYS_VIS = 5
DAYS_HID = 5
DAYS_ALL = DAYS_VIS + DAYS_HID
START = date(2026, 9, 23)
N_PER_CAT = 5
BUDGET_POOL = [10, 15, 20, 20, 20, 25, 25, 30, 30, 40, 50]

# 品类: 名称, CPM基线, CTR基线%, AOV基线, 点击->加购, 加购->结账, 结账->支付, 支付->购买
CATS = [
    ('短袖T恤',  13.5, 2.60, 42, 0.088, 0.50, 0.55, 0.72),
    ('卫衣',     15.0, 2.30, 58, 0.082, 0.49, 0.55, 0.70),
    ('长裤',     14.5, 2.10, 56, 0.080, 0.49, 0.54, 0.71),
    ('帽子',     11.5, 2.50, 29, 0.090, 0.52, 0.57, 0.74),
    ('连衣裙',   16.5, 2.70, 70, 0.078, 0.48, 0.53, 0.70),
    ('夹克',     17.5, 1.90, 86, 0.064, 0.46, 0.52, 0.68),
    ('衬衫',     14.0, 2.05, 48, 0.074, 0.49, 0.55, 0.71),
    ('短裤',     12.5, 2.25, 36, 0.084, 0.51, 0.56, 0.73),
    ('鞋',       19.0, 2.20, 90, 0.060, 0.45, 0.51, 0.67),
    ('袜子',     10.0, 2.90, 19, 0.100, 0.54, 0.60, 0.76),
    ('内衣',     13.5, 2.00, 43, 0.076, 0.50, 0.55, 0.72),
    ('毛衣',     16.0, 1.90, 65, 0.068, 0.47, 0.53, 0.69),
    ('羽绒服',   19.5, 1.70, 133, 0.052, 0.43, 0.50, 0.66),
    ('西装',     20.5, 1.60, 116, 0.048, 0.42, 0.49, 0.65),
    ('风衣',     18.0, 1.75, 97, 0.056, 0.44, 0.50, 0.67),
    ('半身裙',   14.5, 2.25, 53, 0.080, 0.49, 0.54, 0.71),
    ('运动裤',   13.5, 2.05, 50, 0.076, 0.50, 0.55, 0.72),
    ('背心',     11.0, 2.30, 26, 0.092, 0.52, 0.58, 0.74),
    ('围巾',     11.0, 1.95, 31, 0.084, 0.51, 0.56, 0.73),
    ('手表',     21.0, 1.65, 156, 0.044, 0.41, 0.48, 0.64),
]

OBJ_MIX = [('OUTCOME_SALES', 0.65), ('OUTCOME_ENGAGEMENT', 0.20), ('OUTCOME_TRAFFIC', 0.15)]
OBJ_CN = {'OUTCOME_SALES': '销量', 'OUTCOME_ENGAGEMENT': '互动', 'OUTCOME_TRAFFIC': '流量'}
OBJ_METRIC = {'OUTCOME_SALES': 'actions:offsite_conversion.fb_pixel_purchase',
              'OUTCOME_ENGAGEMENT': 'actions:post_engagement',
              'OUTCOME_TRAFFIC': 'actions:link_click'}

F_IC, F_PAY, F_PUR, F_AOV, F_CPM = 0.90, 1.05, 1.12, 1.35, 0.86

# 调参旋钮（校准真实性与可解性时只动这几个）
P_NARROW = 0.30          # 小受众池广告占比（只有它们才会显出频次爬升）
ALPHA_SD = 0.055         # 放量掉转化的「运气噪声」，越大题越难
SAT_CTR = 0.45           # 饱和度对 CTR 的惩罚系数
SAT_ALPHA = 0.50         # 饱和度转成"放量掉转化"的系数

COL = ['报告开始日期', '报告结束日期', '广告系列名称', '广告系列投放', '归因设置', '成效', '成效指标',
       '覆盖人数', '展示次数', '频次', '链接点击量', '单次链接点击费用 (USD)', 'CPM（千次展示费用） (USD)',
       '广告组预算', '广告组预算类型', '已花费金额 (USD)', '加入购物车次数', '结账发起次数', '购物次数',
       '添加支付信息', '购物转化价值', '广告花费回报 (ROAS) - 购物', '结果（初始）', '成效（初始）指标',
       '广告系列目标', '广告系列 ID']


def pick_obj():
    r, acc = RNG.random(), 0.0
    for name, w in OBJ_MIX:
        acc += w
        if r <= acc:
            return name
    return OBJ_MIX[-1][0]


def clamp(x, lo, hi):
    return max(lo, min(hi, x))


def intsamp(x, u):
    """把「期望次数」变成整数：整数部分 + 小数部分按概率进位。

    为什么要这一步（2026-10-02 用户/AI 联合查出的 bug）：
      原来 rev = pur * aov 用的是**小数**单量，而写进 CSV 的「购物次数」是 round(pur)。
      于是一条日均 0.4 单的广告，5 天「购物次数=0」却有「购物转化价值=$154」——
      数据自相矛盾（AI 都照着这份矛盾数据写了「5天零购买，ROAS 3.0」）。
      真实 Facebook 数据的购买次数是整数，价值 = 整数单量 × 客单价。
    这个抽样**保均值**（Bernoulli），不会改变广告的经济性，只让它变整数、变真实。
    """
    if x <= 0:
        return 0
    b = math.floor(x)
    return int(b) + (1 if (x - b) > u else 0)


# ==================== 1. 建模：受众池 + 机制参数 ====================
ads = []
for ci, (cname, cpm0, ctr0, aov0, atc0, ic0, pay0, pur0) in enumerate(CATS):
    for k in range(1, N_PER_CAT + 1):
        idx = ci * N_PER_CAT + k
        objective = pick_obj()
        oc = OBJ_CN[objective]
        suffix = {1: '', 2: ' - 广告副本', 3: '', 4: ' - 广告副本', 5: ''}[k]
        win = {1: '1-1-新', 2: '1-1-新', 3: '2-1-新', 4: '2-1-新', 5: '3-1-新'}[k]
        if suffix:
            name = '新%s广告系列-0923-%s%s%s' % (oc, cname, win, suffix)
        else:
            name = '新%s广告系列-0923-%s%s' % (oc, cname, win)

        budget = RNG.choice(BUDGET_POOL)
        budget_type = '使用广告组预算' if RNG.random() < 0.70 else '使用广告系列预算'

        # —— 受众池 A：频次会不会爬，全看这个 ——
        if RNG.random() < P_NARROW:
            A_pool = math.exp(RNG.gauss(math.log(60000), 0.50))     # 小池子 30k~12万
        else:
            A_pool = math.exp(RNG.gauss(math.log(900000), 0.75))    # 大池子 40万~200万
        f0 = RNG.uniform(1.02, 1.30)          # reach curve 的低饱和人均曝光
        cpm_drift = RNG.uniform(-0.004, 0.018)  # CPM 每天的自然漂移

        # —— 交付能力：预算能不能花得完（花不完 ⇒ 加预算没用）——
        if RNG.random() < 0.26:
            cap_ratio = RNG.uniform(0.32, 0.90)
        else:
            cap_ratio = RNG.uniform(1.05, 1.65)

        q = math.exp(RNG.gauss(0.25, 0.42))     # 转化能力
        fatigue = RNG.uniform(0.000, 0.025)     # CTR 日衰减

        atc_r = atc0 * RNG.uniform(0.80, 1.20)
        pur_r = pur0 * F_PUR * RNG.uniform(0.85, 1.15)
        if objective == 'OUTCOME_ENGAGEMENT':
            atc_r *= 0.62
            pur_r *= 0.55
        elif objective == 'OUTCOME_TRAFFIC':
            atc_r *= 0.70
            pur_r *= 0.62

        # —— 日噪声：预先抽好，A/B 两个版本**共用同一串**，差值才不是运气 ——
        noise = [dict(cpm=math.exp(RNG.gauss(0, 0.055)),
                      spend=1.0 + RNG.gauss(0, 0.035),
                      cap=1.0 + RNG.gauss(0, 0.04),
                      u=RNG.random(),
                      aov=math.exp(RNG.gauss(0, 0.12)))
                 for _ in range(DAYS_ALL)]

        ads.append(dict(
            idx=idx, name=name, cat=cname, objective=objective,
            budget=budget, budget_type=budget_type,
            cpm=cpm0 * F_CPM * RNG.uniform(0.90, 1.10),
            ctr=ctr0 / 100.0 * RNG.uniform(0.82, 1.22),
            atc_r=atc_r, ic_r=ic0 * F_IC * RNG.uniform(0.88, 1.12),
            pay_r=pay0 * F_PAY * RNG.uniform(0.88, 1.12), pur_r=pur_r,
            aov=aov0 * F_AOV * math.exp(RNG.gauss(0, 0.14)),
            A_pool=A_pool, f0=f0, cpm_drift=cpm_drift, cap_ratio=cap_ratio,
            q=q, fatigue=fatigue, noise=noise,
            ad_id=1202110000137 + idx,
        ))


# ==================== 2. 逐日推演（reach curve 驱动） ====================
def simulate(a, plan, conv_mult=None):
    """plan[d] = 第 d 天的日预算。conv_mult(d) = 第 d 天转化乘子（放量惩罚用）。

    两个口径都保留，各有用处：
      · 整数（实际发生）口径 atc/ic/pay/pur/rev —— 写进 CSV，真实 FB 数据的购买次数就是整数，
        而且不会出现「购物次数 0 却有购物转化价值」这种自相矛盾。
      · 平滑（期望）口径 *_e —— **判卷用**。判断「该不该加钱」是在判**期望效果**；
        若拿单次整数化的结果去比 A/B，买 1 单和 0 单的差别（一个客单价）会盖过 alpha 的真实影响，
        等于用运气冒充判断力。
    """
    I_cum = 0.0
    R_prev = 0.0
    out = []
    for d in range(DAYS_ALL):
        nz = a['noise'][d]
        b = plan[d]
        daily_cap = a['cap_ratio'] * a['budget'] * nz['cap']
        spend = min(b * nz['spend'], daily_cap)
        cpm = a['cpm'] * (1.0 + a['cpm_drift'] * d) * nz['cpm']
        imp = spend / cpm * 1000.0

        I_cum += imp
        R_cum = a['A_pool'] * (1.0 - math.exp(-I_cum / (a['A_pool'] * a['f0'])))
        reach = max(1.0, R_cum - R_prev)
        R_prev = R_cum
        sat = R_cum / a['A_pool']

        ctr = (a['ctr'] * max(0.25, 1.0 - SAT_CTR * sat)
               * max(0.30, 1.0 - a['fatigue'] * d))
        clicks = imp * ctr
        atc_e = clicks * a['atc_r']
        ic_e = atc_e * a['ic_r']
        pay_e = ic_e * a['pay_r']
        cm = 1.0 if conv_mult is None else conv_mult(d)
        pur_e = pay_e * a['pur_r'] * a['q'] * cm
        aov = a['aov'] * nz['aov']

        u = nz['u']
        atc = intsamp(atc_e, u)
        ic = min(atc, intsamp(ic_e, u))
        pay = min(ic, intsamp(pay_e, u))
        pur = min(pay, intsamp(pur_e, u))

        out.append(dict(d=d, spend=spend, imp=imp, reach=reach, freq=imp / reach,
                        clicks=clicks, aov=aov,
                        atc=atc, ic=ic, pay=pay, pur=pur, rev=pur * aov,
                        atc_e=atc_e, ic_e=ic_e, pay_e=pay_e, pur_e=pur_e, rev_e=pur_e * aov,
                        sat=sat, cpm=cpm))
    return out


# ==================== 3. 生成 ====================
rows_out, truth, audit = [], [], []
hid_rows = []

for a in ads:
    b = a['budget']
    vis = simulate(a, [b] * DAYS_ALL)                      # 实际发生的那一条路径
    sat_end_A = vis[-1]['sat']
    # 放量掉转化的程度：由受众饱和度决定 + 一层前 5 天看不见的运气
    # 允许为负：真实里小幅加预算（<20%）经常因为算法拿到更多数据而效率微升
    alpha = clamp(-0.02 + SAT_ALPHA * sat_end_A + RNG.gauss(0, ALPHA_SD), -0.12, 0.45)
    a['alpha_'] = alpha        # 滚动决策段要复用（存下来不消耗随机数，主数据一字不变）

    visA = vis[:DAYS_VIS]
    hidA = vis[DAYS_VIS:]

    # ---------- 动作一：关停 —— 由**可见窗口**决定（公平：可见在亏才叫停）----------
    spend_vis = sum(x['spend'] for x in visA)
    net_vis = sum(x['rev'] for x in visA) - spend_vis
    neg = max(5.0, 0.06 * spend_vis)

    # ---------- 动作二：加预算 / 观察 —— 由**未来推断**决定 ----------
    # 用期望口径推未来，避免"整数化那次抽签"的运气盖过 alpha 的真实影响
    spendA = sum(x['spend'] for x in hidA)
    revA = sum(x['rev_e'] for x in hidA)
    spendB, revB = 0.0, 0.0
    hidB_calc = []
    for x in hidA:
        nz = a['noise'][x['d']]
        capB = a['cap_ratio'] * b * nz['cap']
        spend_t = min(b * 1.20 * nz['spend'], capB)
        ratio = spend_t / max(1e-9, x['spend'])
        rev_t = x['rev_e'] * ratio * (1.0 - alpha)
        spendB += spend_t
        revB += rev_t
        pur_t = x['pur_e'] * ratio * (1.0 - alpha)
        hidB_calc.append(dict(d=x['d'], spend=spend_t, rev=rev_t, rev_e=rev_t,
                              pur=pur_t, pur_e=pur_t,
                              freq=x['freq'], reach=x['reach'], sat=x['sat']))
    netA, netB = revA - spendA, revB - spendB
    delta = netB - netA

    thresh = max(2.0, 0.02 * abs(netA))
    if net_vis < -neg:
        act = '暂停'          # 可见窗口就在亏钱 → 停
        why = '可见亏损'
    elif delta > thresh:
        act = '加预算'         # 不亏 + 往后放量净增 → 加
        why = '未来净增'
    else:
        act = '观察'          # 其余一律不动
        why = '未来无净增'

    freq5 = visA[-1]['freq']
    fill5 = sum(x['spend'] for x in visA) / (b * DAYS_VIS)

    for x in visA:
        rows_out.append(dict(
            _date=x['d'], _name=a['name'], _obj=a['objective'], _budget=b,
            _btype=a['budget_type'], _metric=OBJ_METRIC[a['objective']],
            _cpm=a['cpm'], _ad_id=a['ad_id'], **x))

    for tag, hd, bud in (('不动', hidA, b), ('加预算', hidB_calc, round(b * 1.2, 2))):
        for x in hd:
            # 🔴 口径必须与真值表一致：真值表用 rev_e（期望），这里也必须用 rev_e。
            #    早前这里导出 rev（实际整数化后的值），导致「逐日合计 ≠ 真值表汇总」，
            #    拿两份表对账的人会算出两个不同的净收益 —— 同一件事不能有两把尺子。
            re_ = x.get('rev_e', x['rev'])
            pe_ = x.get('pur_e', x['pur'])
            hid_rows.append(dict(广告=a['name'], 方案=tag, 预算=bud,
                                 第几天=x['d'] - DAYS_VIS + 1,
                                 花费=round(x['spend'], 2), 期望收入=round(re_, 2),
                                 期望净=round(re_ - x['spend'], 2),
                                 期望购买=round(pe_, 2), 频次=round(x['freq'], 3),
                                 覆盖=round(x['reach']), 饱和度=round(x['sat'], 3)))

    truth.append(dict(
        广告=a['name'], 品类=a['cat'], 目标=a['objective'], 预算=b, 预算类型=a['budget_type'],
        正确动作=act, 判定依据=why,
        前5天净=round(net_vis, 2),
        总花费=round(sum(x['spend'] for x in visA), 2),
        总收入=round(sum(x['rev'] for x in visA), 2),
        总购买=sum(round(x['pur']) for x in visA),
        _受众池=round(a['A_pool']), _机制q=round(a['q'], 3), _机制疲劳=round(a['fatigue'], 4),
        _交付比=round(a['cap_ratio'], 3), _放量掉转化alpha=round(alpha, 3),
        _频次5=round(freq5, 3), _顶格率5=round(fill5, 3), _饱和5=round(sat_end_A, 3),
        _覆盖首日=round(visA[0]['reach']), _覆盖末日=round(visA[-1]['reach']),
        不动_花费=round(spendA, 2), 不动_收入=round(revA, 2), 不动_净=round(netA, 2),
        加预算_花费=round(spendB, 2), 加预算_收入=round(revB, 2), 加预算_净=round(netB, 2),
        差额=round(delta, 2),
    ))
    audit.append((a['name'], act, a['A_pool'], freq5, fill5, sat_end_A, alpha, netA, netB))


# ==================== 3.5 滚动决策真值（每天都判一次） ====================
# 【为什么补这一段】2026-10-02 用户的两句质疑，成立且指向同一件事：
#   「近三天1单ROAS1.41，是我也不加了。如果加就该在24号出单立即加，后面27跑完不加正常」
#   「内衣三天没出单花了60刀还没停？我第二天中午就停了」
#   他说的是**滚动决策**：真实投手每天都在看盘，看到苗头就动手，不是等 5 天跑完才看一次。
#   而上面那份 truth 的决策点**钉死在「第 5 天收盘」**，比较的是第 6~10 天「加钱 vs 不动」，
#   回答的是「这段跑完，往后加钱值不值」，**回答不了「跑到一半该不该动手」**。
#   → 所以补这一段：对每条广告、每个决策日 d（= 第 d 天收盘，0-indexed），
#     只用「第 0..d 天」的可见数据，比较三条路在**接下来 3 天**的净收益：
#         不动 / 加预算 20% / 暂停
#   口径与上面**完全相同**（期望值、同一串噪声、同一套 cap 与 alpha 惩罚），
#   只是把起点从「第 5 天」改成「第 d 天」—— 同一件事只用一把尺子。
ROLL_DAYS = 3          # 决策后观察 3 天（"加钱后看 3 天"是投放里最常见的观察窗）
roll_rows = []

for a in ads:
    b = a['budget']
    base = simulate(a, [b] * DAYS_ALL)      # 全程不动的基准路径（= 上面那条 vis，逐日一致）
    alpha = a['alpha_']
    for d in range(DAYS_VIS):
        hid = base[d + 1: d + 1 + ROLL_DAYS]
        if not hid:
            continue
        seen = base[:d + 1]

        # 路一：不动
        net_hold = sum(x['rev_e'] - x['spend'] for x in hid)
        # 路二：加预算 20%（第 d+1 天起生效；花不出去的不变，转化率吃 alpha 惩罚）
        net_up = 0.0
        for x in hid:
            nz = a['noise'][x['d']]
            capB = a['cap_ratio'] * b * nz['cap']
            spend_t = min(b * 1.20 * nz['spend'], capB)
            ratio = spend_t / max(1e-9, x['spend'])
            net_up += x['rev_e'] * ratio * (1.0 - alpha) - spend_t
        # 路三：暂停（不花钱也不赚钱 → 净 0）
        net_stop = 0.0

        cost = sum(x['spend'] for x in hid)
        thresh = max(2.0, 0.02 * abs(net_hold))
        neg = max(3.0, 0.06 * cost)
        if net_hold < -neg:
            act = '暂停'
        elif net_up - net_hold > thresh:
            act = '加预算'
        else:
            act = '观察'

        cum_spend = sum(x['spend'] for x in seen)
        roll_rows.append(dict(
            广告=a['name'], 决策日=d + 1, 已跑天数=d + 1,
            累计花费=round(cum_spend, 2),
            累计收入=round(sum(x['rev'] for x in seen), 2),
            累计净=round(sum(x['rev'] - x['spend'] for x in seen), 2),
            累计购买=sum(round(x['pur']) for x in seen),
            末日频次=round(seen[-1]['freq'], 3),
            顶格率=round(cum_spend / (b * (d + 1)), 3),
            后3天_不动净=round(net_hold, 2),
            后3天_加预算净=round(net_up, 2),
            后3天_暂停净=round(net_stop, 2),
            加vs不动=round(net_up - net_hold, 2),
            正确动作=act,
            _受众池=round(a['A_pool']), _alpha=round(alpha, 3),
        ))

# ==================== 4. 落盘 ====================
P_DATA = os.path.join(D, '模拟广告数据-100广告10天-v5.csv')
P_TRUTH = os.path.join(D, '真值表-v5.csv')
P_HID = os.path.join(D, '真值表-逐日-v5.csv')
P_LOG = os.path.join(D, 'gen_v5_out.txt')

with io.open(P_DATA, 'w', encoding='utf-8-sig', newline='') as f:
    w = csv.writer(f)
    w.writerow(COL)
    for r in sorted(rows_out, key=lambda z: (z['_name'], z['_date'])):
        w.writerow([
            (START + timedelta(days=r['_date'])).isoformat(),
            (START + timedelta(days=r['_date'])).isoformat(),
            r['_name'], 'active', '点击后 7 天内、浏览后 1 天内或互动观看后 1 天内',
            int(round(r['pur'])), r['_metric'],
            int(round(r['reach'])), int(round(r['imp'])), round(r['freq'], 6),
            int(round(r['clicks'])),
            round(r['spend'] / r['clicks'], 4) if r['clicks'] > 0 else 0,
            round(r['spend'] / r['imp'] * 1000, 6) if r['imp'] > 0 else 0,
            r['_budget'], r['_btype'], round(r['spend'], 2),
            int(round(r['atc'])), int(round(r['ic'])), int(round(r['pur'])), int(round(r['pay'])),
            round(r['rev'], 2),
            round(r['rev'] / r['spend'], 2) if r['spend'] > 0 else 0,
            '', '', r['_obj'], r['_ad_id'],
        ])

with io.open(P_TRUTH, 'w', encoding='utf-8-sig', newline='') as f:
    w = csv.DictWriter(f, fieldnames=list(truth[0].keys()))
    w.writeheader()
    w.writerows(truth)

with io.open(P_HID, 'w', encoding='utf-8-sig', newline='') as f:
    w = csv.DictWriter(f, fieldnames=list(hid_rows[0].keys()))
    w.writeheader()
    w.writerows(hid_rows)

P_ROLL = os.path.join(D, '滚动决策真值-v5.csv')
with io.open(P_ROLL, 'w', encoding='utf-8-sig', newline='') as f:
    w = csv.DictWriter(f, fieldnames=list(roll_rows[0].keys()))
    w.writeheader()
    w.writerows(roll_rows)

# ==================== 5. 自检日志（真实性 + 分布） ====================
L = []
def say(s=''):
    L.append(s)

from collections import Counter

say('=== 真值分布（100 条）===')
cnt = Counter(t['正确动作'] for t in truth)
for k in ('加预算', '观察', '暂停'):
    say('  %s: %d 条 (%.0f%%)' % (k, cnt[k], cnt[k] / len(truth) * 100))

say()
say('=== 真实性自检（v4 就是死在这里）===')
fq = sorted(t['_频次5'] for t in truth)
sp = [r for r in rows_out]
say('  第 5 天频次:  最小 %.2f / 中位 %.2f / p90 %.2f / 最大 %.2f' % (
    fq[0], fq[len(fq) // 2], fq[int(len(fq) * 0.9)], fq[-1]))
say('  频次 5 天涨幅中位: %.3f' % sorted(
    t['_频次5'] - 1.0 for t in truth)[len(truth) // 2])
chg = sorted(t['_覆盖末日'] / t['_覆盖首日'] for t in truth)
say('  覆盖 5 天变化倍数: 最小 %.2f / 中位 %.2f / 最大 %.2f' % (chg[0], chg[len(chg) // 2], chg[-1]))
# 花费/展示是否还恒定
byname = {}
for r in sp:
    byname.setdefault(r['_name'], []).append(r)
flat_spend = sum(1 for v in byname.values() if len(set(round(x['spend'], 2) for x in v)) == 1)
flat_imp = sum(1 for v in byname.values() if len(set(round(x['imp'], 1) for x in v)) == 1)
say('  花费 5 天完全不变的广告: %d / %d 条（v4 是 100）' % (flat_spend, len(byname)))
say('  展示 5 天完全不变的广告: %d / %d 条（v4 是 100）' % (flat_imp, len(byname)))
bad = sum(1 for r in rows_out if int(r['pur']) == 0 and r['rev'] > 0.5)
nonmono = sum(1 for r in rows_out
              if not (int(r['atc']) >= int(r['ic']) >= int(r['pay']) >= int(r['pur'])))
say('  「购物次数=0 却有购物转化价值」的行: %d / %d（自相矛盾，必须 0）' % (bad, len(rows_out)))
say('  漏斗不单调（加购<结账 等）的行: %d / %d（必须 0）' % (nonmono, len(rows_out)))

say()
say('=== 结构性诊断（调参看这里）===')
al = sorted(t['_放量掉转化alpha'] for t in truth)
say('  alpha: p25 %.3f / 中位 %.3f / p75 %.3f' % (al[len(al) // 4], al[len(al) // 2], al[len(al) * 3 // 4]))
say('  受众池: p25 %d / 中位 %d / p75 %d' % tuple(
    sorted(t['_受众池'] for t in truth)[int(len(truth) * q_)] for q_ in (0.25, 0.5, 0.75)))
say('  第5天饱和: 中位 %.3f / p90 %.3f' % (
    sorted(t['_饱和5'] for t in truth)[len(truth) // 2], sorted(t['_饱和5'] for t in truth)[int(len(truth) * 0.9)]))
say('  前5天净利为正: %d / %d' % (sum(1 for t in truth if t['总花费'] and t['总收入'] - t['总花费'] > 0), len(truth)))
say('  花不完预算(顶格率<0.9): %d / %d' % (sum(1 for t in truth if t['_顶格率5'] < 0.9), len(truth)))
say('  delta>0 的: %d / %d' % (sum(1 for t in truth if t['差额'] > 0), len(truth)))
dl = sorted(t['差额'] for t in truth)
say('  delta 分布: p25 %.1f / 中位 %.1f / p75 %.1f' % (dl[len(dl) // 4], dl[len(dl) // 2], dl[len(dl) * 3 // 4]))
na = sorted(t['不动_净'] for t in truth)
say('  隐藏5天净A 分布: p25 %.1f / 中位 %.1f / p75 %.1f' % (na[len(na) // 4], na[len(na) // 2], na[len(na) * 3 // 4]))
say('  顶格且净利正且 delta>0: %d' % sum(
    1 for t in truth if t['_顶格率5'] >= 0.9 and t['不动_净'] > 0 and t['差额'] > 0))
say('  没顶格(加预算也花不出去): %d' % sum(1 for t in truth if t['_顶格率5'] < 0.9))
say('  覆盖 5 天在缩(末日<首日)的: %d / %d' % (
    sum(1 for t in truth if t['_覆盖末日'] < t['_覆盖首日']), len(truth)))

say()
say('=== 分档交叉（答案能否从可见信号推出）===')
say('  %-8s %8s %8s %8s %8s %8s' % ('动作', '受众池中位', '频次5中位', '顶格率5', 'alpha中位', '差额中位'))
for k in ('加预算', '观察', '暂停'):
    sub = [t for t in truth if t['正确动作'] == k]
    if not sub:
        continue
    med = lambda xs: sorted(xs)[len(xs) // 2]
    say('  %-8s %8d %8.2f %8.2f %8.3f %8.1f' % (
        k, med([t['_受众池'] for t in sub]), med([t['_频次5'] for t in sub]),
        med([t['_顶格率5'] for t in sub]), med([t['_放量掉转化alpha'] for t in sub]),
        med([t['差额'] for t in sub])))

say()
say('=== 滚动决策（%d 个决策点 = %d 广告 × %d 天）===' % (len(roll_rows), len(ads), DAYS_VIS))
rc = Counter(r['正确动作'] for r in roll_rows)
for k in ('加预算', '观察', '暂停'):
    say('  %s: %d (%.0f%%)' % (k, rc[k], rc[k] / len(roll_rows) * 100))
say()
say('  按决策日看（第几天收盘时判的）:')
say('    %-8s %8s %8s %8s' % ('决策日', '加预算', '观察', '暂停'))
for d_ in range(1, DAYS_VIS + 1):
    sub = [r for r in roll_rows if r['决策日'] == d_]
    c = Counter(r['正确动作'] for r in sub)
    say('    第%d天   %8d %8d %8d' % (d_, c['加预算'], c['观察'], c['暂停']))

byname_roll = {}
for r in roll_rows:
    byname_roll.setdefault(r['广告'], []).append(r)
only_hold = [t['广告'] for t in truth if t['正确动作'] == '观察']
early_up = [n for n in only_hold
            if any(r['正确动作'] == '加预算' for r in byname_roll[n] if r['决策日'] <= 3)]
say()
say('  主真值判「观察」%d 条 → 其中**前 3 天内曾判过「加预算」**的 %d 条'
    % (len(only_hold), len(early_up)))
ups = [t['广告'] for t in truth if t['正确动作'] == '加预算']
fu = [min(r['决策日'] for r in byname_roll[n] if r['正确动作'] == '加预算')
      for n in ups if any(r['正确动作'] == '加预算' for r in byname_roll[n])]
if fu:
    say('  主真值判「加预算」%d 条 → 滚动视角最早判加日: 第%d天 / 中位第%d天 / 最晚第%d天'
        % (len(ups), min(fu), sorted(fu)[len(fu) // 2], max(fu)))

say()
say('=== 前 5 天大盘 ===')
say('  花费 $%.2f / 收入 $%.2f / 购买 %d / 整体 ROAS %.2f' % (
    sum(t['总花费'] for t in truth), sum(t['总收入'] for t in truth),
    sum(t['总购买'] for t in truth),
    sum(t['总收入'] for t in truth) / sum(t['总花费'] for t in truth)))
say('  行数 %d（= %d 广告 × %d 天）' % (len(rows_out), len(ads), DAYS_VIS))

with io.open(P_LOG, 'w', encoding='utf-8') as f:
    f.write('\n'.join(L))

print('\n'.join(L))
print()
print('data  -> %s' % P_DATA)
print('truth -> %s' % P_TRUTH)
