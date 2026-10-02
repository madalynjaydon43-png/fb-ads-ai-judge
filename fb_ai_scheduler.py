# -*- coding: utf-8 -*-
"""
广告 AI 调度器（无 GUI 依赖，可单独跑，也可被 GUI 调用）
职责：每 interval_minutes 分钟 拉系列洞察 -> 存历史 -> 调 9router -> 出建议 -> 交给人确认
"""
import json
import os
import re
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from fb_ai_engine import (
    AI_CONFIG_DEFAULTS, make_snapshot, build_prompt,
    call_llm, parse_suggestions, apply_guardrails,
    enforce_risk_guardrails,
)

# 上游报错里的「reset after 16s / reset after 1m 1s」= 限流窗口剩余时间
_RESET_RE = re.compile(r'reset\s+after\s*(?:(\d+)\s*m)?\s*(\d+)\s*s', re.I)


def _retry_wait_seconds(err_text, attempt):
    """按错误类型算退避秒数（2026-09-30 新增）。

    429 是**限流**（tpm/rpm exhausted），不是 key 失效 —— 会自愈，必须等窗口重置。
    立刻重试只会再吃一个 429：实测 10 批背靠背发、批间零间隔，5 批连撞
    `inference exceeds tpm/rpm limit`，三次重试全在同一个限流窗口里打水漂。
    上游会把窗口剩余时间写在 `reset after Ns`，优先按它等（上限 60s）。
    """
    t = err_text or ''
    m = _RESET_RE.search(t)
    if m:
        mins = int(m.group(1) or 0)
        secs = int(m.group(2) or 0)
        return min(mins * 60 + secs + 1, 60)
    if '429' in t:
        return min(5 * (attempt + 1), 30)
    return min(2 * (attempt + 1), 10)


# 🔴 进程级「AI 判断」互斥锁（2026-09-30 修）
# 为什么必须是**模块级**而不是实例级：同一个进程里可能同时活着两个 AdsAiScheduler ——
#   ① 生产那个（GUI 启用 AI 后的自动循环）；
#   ② 「测试：上传文件」临时 new 出来的那个。
# 实例级锁管不住 ①②，两边会同时打同一个上游（9router → 同一个模型）→ 撞 429，
# 而且白烧一份 token。实例锁只能防「同一个实例被并发调用」。
# 上游配额是**进程级的公共资源**，锁就必须是进程级的。
# 语义：抢到锁 = 此刻本进程有一轮 AI 判断在跑（不管是谁发起的）。
_AI_RUN_LOCK = threading.Lock()


class AdsAiScheduler:
    def __init__(self, get_insights_fn, config_path=None, log_fn=print,
                 history_path=None, zero_spend_path=None,
                 config_override=None, window=None):
        """
        get_insights_fn: 函数，返回系列洞察列表（list of dict，需含 id/name/status/spend/
                         impressions/clicks/cpc/cpm/purchase/purchase_value/cost_per_purchase
                         可选 start_time）。由 GUI 或独立脚本提供（内部会调 Facebook API）。
        config_path: ai_config.json 路径；None 则用默认配置 + 当前目录
        log_fn: 日志回调（GUI 传 self.log，独立脚本传 print）

        --- 以下四个参数是「拿上传的文件测 AI」用的（2026-09-30 新增），默认 None = 生产行为不变 ---
        history_path: 历史文件落点。测试时必须指到别的文件，否则会把真实账户的历史
                      和模拟数据的历史混在一起（后面做趋势对比时两边互相污染）。
        zero_spend_path: 零消耗清单落点。同上，测试不能覆盖真实账户那份清单。
        config_override: 覆盖配置项（dict），在读完 ai_config.json 之后合并 —— 用于测试时把
                      `lookback_days` 对齐成被测文件实际的天数，避免 prompt 里声明的
                      「最近 7 天」和文件里的 7 天不是同一段日期。
        window: (start_date, end_date) 字符串元组，直接作为 prompt 里的数据口径声明。
                      None = 按 lookback_days 从今天往前推（生产默认）。
        """
        self.get_insights_fn = get_insights_fn
        self.log_fn = log_fn
        self.script_dir = os.path.dirname(os.path.abspath(__file__))
        self.config_path = config_path or os.path.join(self.script_dir, "ai_config.json")
        self.history_path = history_path or os.path.join(self.script_dir, "ai_history.json")
        self.zero_spend_path = zero_spend_path or os.path.join(self.script_dir, "ai_zero_spend_skipped.json")
        self.config_override = dict(config_override or {})
        self.window = tuple(window) if window else None
        self.suggestions = []
        self.last_run = None
        self.last_error = None
        self.last_timing = None   # 上一轮的耗时明细（供 GUI / 脚本读）
        self._stop = threading.Event()
        self._thread = None
        self._lock = threading.Lock()
        # 后台循环的「代号」：stop() 会让代号自增，从而**立刻作废**正在跑的那个循环。
        # 见 start() / _loop() 的注释 —— 解决「stop 后马上 start 会静默死掉」的坑。
        self._gen = 0
        self._loop_gen = 0
        # 防重入锁用的是**模块级** _AI_RUN_LOCK（跨实例互斥），见文件顶部注释。
        # 注意：__init__ 里不再建实例锁 —— 留一个没人用的实例锁只会误导下一个读代码的人。

    def log(self, msg, level="INFO"):
        try:
            self.log_fn(f"[AI调度] {msg}", level)
        except Exception:
            pass

    # ---------- 配置 ----------
    def load_config(self):
        cfg = dict(AI_CONFIG_DEFAULTS)
        if os.path.exists(self.config_path):
            try:
                with open(self.config_path, encoding='utf-8') as f:
                    user_cfg = json.load(f)
                if isinstance(user_cfg, dict):
                    cfg.update(user_cfg)
            except Exception as e:
                self.log(f"读取 ai_config.json 失败 {e}", "ERROR")
        # 测试模式的覆盖项放最后合并 —— 优先级：命令行/构造参数 > ai_config.json > 默认值
        if self.config_override:
            cfg.update(self.config_override)
        return cfg

    def save_config(self, cfg):
        try:
            with open(self.config_path, 'w', encoding='utf-8') as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
        except Exception as e:
            self.log(f"保存 ai_config.json 失败 {e}", "ERROR")

    # ---------- 历史 ----------
    def _load_history(self):
        if os.path.exists(self.history_path):
            try:
                with open(self.history_path, encoding='utf-8') as f:
                    return json.load(f)
            except Exception:
                pass
        return []

    def _save_history(self, hist):
        try:
            with open(self.history_path, 'w', encoding='utf-8') as f:
                json.dump(hist, f, ensure_ascii=False, indent=2)
        except Exception as e:
            self.log(f"保存 ai_history.json 失败 {e}", "ERROR")

    def _save_zero_spend_skipped(self, zero_ads, min_spend):
        """把被零消耗过滤挡下的广告写成清单文件，供人工看审核状态 / 出价。

        这些广告没进 prompt，所以 AI 永远不会给它们结论 —— 但「挂着不花钱」本身就是
        一个要处理的状态（审核未过 / 出价抢不到量 / 刚建未起量），不能只留一行日志飘走。
        每次运行都重写（哪怕 0 条），避免读到上一轮的过期清单。
        """
        path = self.zero_spend_path
        try:
            payload = {
                "ts": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                "min_spend_to_ai": min_spend,
                "count": len(zero_ads),
                "ads": [{
                    "id": r.get('id'), "name": r.get('name'), "status": r.get('status'),
                    "objective": r.get('objective'),
                    "spend": r.get('spend'), "impressions": r.get('impressions'),
                    "hint": ("有展示但零花费 → 出价太低抢不到量，去看竞价和受众"
                             if (r.get('impressions') or 0) > 0
                             else "零展示零花费 → 大概率审核未过 / 刚建未起量 / 已暂停"),
                } for r in zero_ads],
            }
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
        except Exception as e:
            self.log(f"写零消耗清单失败 {e}", "WARNING")

    # ---------- 核心：跑一轮 ----------
    def is_running(self):
        """是否有一轮判断正在进行中（**进程级**：含别的实例发起的那轮）。

        GUI 用它做前置拦截（避免重复消耗 token）。因为读的是模块级锁，
        「上传文件测试」和「真实账户自动循环」也互相看得见 —— 本来就是要互斥的。
        """
        return _AI_RUN_LOCK.locked()

    def run_once(self):
        """执行一轮：拉数据->快照->调 LLM->建议。返回建议列表（被防重入跳过时返回 None）。

        ⚠️ 防重入（2026-09-30 补）：自动循环（_loop，每 interval_minutes 一次）和界面的
        「立即判断」（ai_run_now）是两条互不知情的入口，撞在一起就是两个 run_once 并发跑。
        实测 19:49 那轮的日志里「分 2 批投喂」「覆盖率 100%」「AI 建议 13 条」各出现了两次，
        耗时明细也是两条（总 24.38s / 24.97s）—— 同一批广告被模型白调了两遍（token 双倍）。
        并发还有两个副作用：① _save_history 是「读-改-写」且只有 suggestions 那个锁，
        两份历史会互相覆盖；② last_timing 被后完成的那轮覆盖，日志里看到的是随机一轮。
        所以用非阻塞锁把「正在跑」立起来：抢不到就立刻返回 None。
        返回 None（被跳过）和返回 []（真跑了但没建议）必须区分开 —— 见 api_fb._ai_run_once_bg。
        """
        if not _AI_RUN_LOCK.acquire(blocking=False):
            self.log("已有一轮 AI 判断在进行中，本次触发被忽略（防并发重复调用）", "WARNING")
            return None
        try:
            return self._run_once_inner()
        finally:
            _AI_RUN_LOCK.release()

    def _run_once_inner(self):
        """真正的一轮逻辑。不要直接调它 —— 走 run_once 才能拿到防重入保护。"""
        try:
            t_run0 = time.time()
            timing = {
                'prepare': 0.0,      # 拉数据 + 快照 + 零消耗过滤
                'llm': 0.0,          # 真正在调模型的时长（含失败的那几次）
                'batch_wait': 0.0,   # 批间主动节流等待
                'backoff_wait': 0.0,  # 429 / 错误退避等待
                'guard': 0.0,        # 护栏 + 覆盖率兜底 + 落盘
                'total': 0.0,
                'llm_calls': 0,      # 实际发出的请求数（含重试）
                'retries': 0,        # 重试次数
                'failed_batches': 0,
                'batches': [],       # 每批明细
            }
            t0 = time.time()
            config = self.load_config()
            insights = self.get_insights_fn()
            if not insights:
                self.last_error = "未获取到系列数据"
                self.log(self.last_error, "WARNING")
                return []

            snapshot = make_snapshot(insights)

            # 零消耗过滤（两档，都从 ai_config.json 读）：
            #   ① min_spend_to_ai（默认 0）：整条广告「总花费」不高于该值 → 整条丢弃。
            #      为什么丢：约束在**模型输出侧**，不在执行侧 —— 模型的动作词汇表只有 4 个
            #      （pause / increase_budget / decrease_budget / observe），且 parse_suggestions
            #      有硬白名单，不在表里的动作词会被直接 continue 丢掉（连 reason 一起丢）。
            #      一条没花钱的广告，前三个动作全是空操作，输出必然恒等于 observe，
            #      送进 prompt 只是稀释注意力。
            #      「有展示但零花费」= 出价太低抢不到量，单独计数 + 写清单文件报出来，
            #      让人自己去看出价，而不是混进 100 条里让 AI 空转。
            #   ② drop_zero_spend_days（默认 false）：是否连「单个 0 花费的日子」也从逐日明细里删掉。
            #      默认不删：某天 0 花费 = 学习期未过审 / 手动暂停 / 预算跑完 的证据，是生命周期信息，
            #      删了 AI 就看不出「这条广告哪天停的、哪天重启的」，反而丢判断依据。
            min_spend = float(config.get('min_spend_to_ai', 0) or 0)
            drop_zero_days = bool(config.get('drop_zero_spend_days', False))
            zero_ads = [r for r in snapshot if (r.get('spend') or 0) <= min_spend]
            snapshot = [r for r in snapshot if (r.get('spend') or 0) > min_spend]
            if drop_zero_days:
                n_days = 0
                for r in snapshot:
                    ds = r.get('daily_spend') or []
                    keep = [d for d in ds if float(d.get('spend') or 0) > 0]
                    n_days += len(ds) - len(keep)
                    r['daily_spend'] = keep
                if n_days:
                    self.log(f"另按 drop_zero_spend_days 删除 {n_days} 个零消耗「广告-日」")
            self._save_zero_spend_skipped(zero_ads, min_spend)
            if zero_ads:
                imp_only = sum(1 for r in zero_ads if (r.get('impressions') or 0) > 0)
                note = f"，其中 {imp_only} 条仍有展示（出价抢不到量，建议单独看出价）" if imp_only else ""
                self.log(f"跳过 {len(zero_ads)} 条零消耗广告（总花费 <= ${min_spend}）{note}")
                self.log(f"零消耗清单已写出 ai_zero_spend_skipped.json（{len(zero_ads)} 条，"
                         f"进来没花钱这件事 AI 不会告诉你，得自己看）")
            if not snapshot:
                self.last_error = "所有广告均无投放数据"
                self.log(self.last_error, "WARNING")
                return []

            timing['prepare'] = round(time.time() - t0, 2)
            timing['n_in'] = len(snapshot)

            # 存历史（按天+时间戳，保留最近 N 条，供趋势参考）
            hist = self._load_history()
            hist.append({
                "ts": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                "snapshot": snapshot,
            })
            hist = hist[-500:]
            self._save_history(hist)

            base_url = config.get('base_url', '')
            api_key = config.get('api_key', '')
            model = config.get('model', AI_CONFIG_DEFAULTS['model'])

            # 历史趋势（过去几轮）按广告 ID 归集，分批时只带「本批」的那部分，
            # 避免把全量历史塞进每一批 prompt。
            hist_by_id = {}
            if len(hist) > 1:
                for h in hist[-6:-1]:
                    for r in h.get("snapshot", []):
                        hist_by_id.setdefault(r.get('id'), []).append(
                            [h["ts"], r.get('spend'), r.get('purchase_value'), r.get('roas')])

            # ---------- 分批投喂（2026-09-30 v2：并发 + 错峰）----------
            # 快照含完整逐日明细后，单条约 2.3K 字符、100 条 = 33 万字符 / 约 10 万 token，
            # 一次全量投喂必然触发网关 413 / 上游 429，所以必须分批。
            # 但「分批 + 纯串行 + 批间隔」代价极高。实测（100 条 / 10 批，2026-09-30）：
            #     串行 + 8s 批间隔    → 236.0s，撞 429 四次
            #     3 路并发，无错峰     → 102.2s，撞 429 七次（三个请求同时发出，瞬时打满配额）
            #     3 路并发 + 错峰 4s   →  36.7s，撞 429 零次   ← 当前默认
            # 关键实测：单次调用耗时与 prompt 体积几乎无关（体积差 4 倍，耗时 9.1s vs 9.7s），
            # 所以提速的唯一杠杆是「并发」，不是「给 prompt 瘦身」。批间无脑 sleep 只是在空等。
            batch_size = max(1, int(config.get('batch_size', 10) or 10))
            workers = max(1, int(config.get('concurrent_workers', 3) or 1))
            # 并发模式下 stagger = 「每轮之间」的错峰间隔；workers=1 时退化成原来的串行批间隔。
            stagger = max(0.0, float(config.get('stagger_seconds', 4) or 0))
            if workers == 1:
                stagger = max(stagger, float(config.get('batch_interval_seconds', 3) or 0))
            batches = [snapshot[i:i + batch_size] for i in range(0, len(snapshot), batch_size)]
            self.log(f"分 {len(batches)} 批投喂（每批 {batch_size} 条，共 {len(snapshot)} 条，"
                     f"{workers} 路并发，每轮错峰 {stagger:g}s）")

            sugg = []
            seen = set()
            failed_batches = []
            failed_ids = set()   # 整批失败的广告 id，兜底时用来区分「压根没判断」和「模型漏写」

            def _do_batch(idx, chunk, turn):
                """跑一批：4 次重试 + 按错误类型退避。只返回结果，不碰共享状态（线程安全）。"""
                bt = {'batch': idx, 'n': len(chunk), 'llm': 0.0, 'backoff': 0.0,
                      'retries': 0, 'calls': 0, 'ok': False, 'n_sugg': 0, 'pass_no': turn}
                # window：测试模式传 (起始日, 结束日)，让 prompt 里的口径声明 = 文件实际的时间段；
                # 生产为 None，build_prompt 自己按 lookback_days 从今天往前推。
                prompt = build_prompt(chunk, config, window=self.window)
                tr = {r['id']: hist_by_id[r['id']] for r in chunk if r.get('id') in hist_by_id}
                if tr:
                    prompt += ("\n\n历史趋势（过去几轮同批广告的 spend/purchase_value/roas，供判断变化）：\n"
                               + json.dumps(tr, ensure_ascii=False))
                part = []
                for attempt in range(4):
                    tc = time.time()
                    bt['calls'] += 1
                    try:
                        raw = call_llm(base_url, api_key, model, prompt)
                        part = parse_suggestions(raw)
                        bt['llm'] += time.time() - tc
                        if part:
                            break
                        self.log(f"批{idx} 未解析出建议（第 {attempt + 1} 次），重试...", "WARNING")
                        if attempt < 3:
                            w = _retry_wait_seconds('', attempt)
                            time.sleep(w)
                            bt['backoff'] += w
                            bt['retries'] += 1
                    except Exception as e:
                        bt['llm'] += time.time() - tc
                        emsg = str(e)
                        if attempt == 3:
                            self.log(f"批{idx} 调用失败：{emsg}", "ERROR")
                        else:
                            wait = _retry_wait_seconds(emsg, attempt)
                            tag = '限流(429)' if '429' in emsg else '错误'
                            self.log(f"批{idx} {tag}，{wait}s 后重试：{emsg[:120]}", "WARNING")
                            time.sleep(wait)
                            bt['backoff'] += wait
                            bt['retries'] += 1
                return idx, chunk, bt, part

            # 两轮：第 1 轮全并发；第 2 轮只跑第 1 轮失败的（等一轮下来限流窗口已重置，往往一次就过）
            remaining = [(i + 1, batches[i]) for i in range(len(batches))]
            turn = 0
            while remaining and turn < 2:
                turn += 1
                cur, remaining = remaining, []
                if turn > 1:
                    self.log(f"第 {turn} 轮：重试第 1 轮失败的 {len(cur)} 批", "WARNING")
                with ThreadPoolExecutor(max_workers=workers) as ex:
                    futs = []
                    for k, (idx, chunk) in enumerate(cur):
                        futs.append(ex.submit(_do_batch, idx, chunk, turn))
                        # 错峰：每满一轮 workers 个就等一下，避免瞬时并发把 TPM 打满（实测 429 归零的关键）
                        if stagger > 0 and (k + 1) % workers == 0 and (k + 1) < len(cur):
                            time.sleep(stagger)
                            timing['batch_wait'] += stagger
                    for f in as_completed(futs):
                        bi, chunk, bt, part = f.result()
                        timing['llm'] += bt['llm']
                        timing['backoff_wait'] += bt['backoff']
                        timing['llm_calls'] += bt['calls']
                        timing['retries'] += bt['retries']
                        bt['llm'] = round(bt['llm'], 2)
                        bt['backoff'] = round(bt['backoff'], 2)
                        timing['batches'].append(bt)
                        if not part:
                            if turn == 1:
                                remaining.append((bi, chunk))
                                timing['requeued'] = timing.get('requeued', 0) + 1
                                self.log(f"批{bi} 第 1 轮失败，进入第 2 轮重试（避开限流窗口）", "WARNING")
                            else:
                                failed_batches.append(bi)
                                failed_ids.update(str(r.get('id')) for r in chunk)
                            continue
                        for s in part:
                            cid = str(s['campaign_id'])
                            if cid in seen:
                                continue
                            seen.add(cid)
                            sugg.append(s)
                        bt['ok'] = True
                        bt['n_sugg'] = len(part)
                        self.log(f"批{bi}/{len(batches)}：输入 {len(chunk)} 条 → 建议 {len(part)} 条"
                                 f"（{bt['llm']}s"
                                 + (f"，退避 {bt['backoff']}s，重试 {bt['retries']} 次" if bt['backoff'] else "")
                                 + "）")

            if not sugg:
                raise RuntimeError(f"全部 {len(batches)} 批均未产出可解析建议")
            if failed_batches:
                self.log(f"有 {len(failed_batches)} 批未产出建议（批次号 {failed_batches}）", "WARNING")

            # 护栏层已撤（fb_ai_engine.ENABLE_BUDGET_CAP=False）：此处只做同对象去重，不夹取预算幅度
            t_g = time.time()
            sugg = apply_guardrails(sugg, snapshot, config)

            # ---------- 覆盖率兜底：输入 N 条 → 必须拿到 N 条 ----------
            # prompt 里写着「输入 N 条 → 必须输出 N 条」，但 parse_suggestions 的 valid_actions
            # 白名单是个**静默漏斗**：模型漏写一条、或动作词写歪（check_bid / hold / stop 这类
            # 不在表里的词），那条建议会被 continue 直接丢掉 —— 不报错、不进日志、不补位。
            # 后果很坏：界面上看起来「剩下的是 AI 判断过、没问题」，实际是「根本没被判断」。
            # 这里做结构性保证：差集一律补 observe（**绝不补 pause / 调预算** —— 兜底动作
            # 不能替模型去动账户）。两类缺失分开报，别把「模型漏写」和「整批调用失败」混成一句。
            got_ids = {str(s.get('campaign_id')) for s in sugg}
            missing = [r for r in snapshot if str(r.get('id')) not in got_ids]
            if missing:
                n_fail = n_model = 0
                for r in missing:
                    is_fail = str(r.get('id')) in failed_ids
                    if is_fail:
                        n_fail += 1
                    else:
                        n_model += 1
                    sugg.append({
                        'campaign_id': str(r.get('id')),
                        'action': 'observe',
                        'budget_change_pct': 0,
                        'reason': ('该批调用失败，本轮未判断（自动兜底）' if is_fail
                                   else '模型未给出结论（自动兜底）'),
                    })
                self.log(f"覆盖率兜底：补 {len(missing)} 条 observe（批失败 {n_fail} / 模型漏写 {n_model}），"
                         f"模型实际给出 {len(snapshot) - len(missing)}/{len(snapshot)}", "WARNING")
            else:
                self.log(f"覆盖率 100%：{len(snapshot)} 条全部由模型给出结论")

            # ---------- 关停护栏 + 加预算提名（2026-10-02）----------
            # ⚠️ 必须在这之后：护栏要对着「已经补全成 N 条」的结论跑，
            #    否则兜底补成 observe 的那几条会绕过护栏。
            sugg, gstat = enforce_risk_guardrails(sugg, snapshot, config)
            if gstat['forced_pause'] or gstat['blocked_kill'] or gstat['nominated']:
                self.log(f"护栏：强制止损 {gstat['forced_pause']} 条 / 禁止误杀 {gstat['blocked_kill']} 条 / "
                         f"加预算提名 {gstat['nominated']} 条"
                         + (f"（{gstat['no_net']} 条无逐日明细，护栏未介入）" if gstat['no_net'] else ""))
            timing['guardrail'] = gstat

            # 条数上限：**默认 0 = 不截断**。要求「每条广告都有一条结论」时绝不能砍。
            # 旧版固定截断到 suggestion_limit（当时 20），会让「100 条广告 → 只显示 20 条建议」。
            limit = int(config.get('suggestion_limit', 0) or 0)
            if limit > 0:
                sugg = sugg[:limit]

            with self._lock:
                self.suggestions = sugg
            timing['guard'] = round(time.time() - t_g, 2)
            timing['failed_batches'] = len(failed_batches)
            timing['n_out'] = len(sugg)
            timing['llm'] = round(timing['llm'], 2)
            timing['batch_wait'] = round(timing['batch_wait'], 2)
            timing['backoff_wait'] = round(timing['backoff_wait'], 2)
            timing['total'] = round(time.time() - t_run0, 2)
            # 「净判断」= 墙钟 - 显式等待。
            # 串行时退避是实打实独占的（一个人等，全员停摆），必须扣；
            # 并发时某一批退避期间其它批仍在跑（时间重叠），再扣一次就会低估，
            # 所以并发口径只扣「错峰等待」——那才是主线程真正空转的部分。
            if workers > 1:
                timing['net_judge'] = round(timing['total'] - timing['batch_wait']
                                           - timing['prepare'] - timing['guard'], 2)
            else:
                timing['net_judge'] = round(timing['total'] - timing['batch_wait']
                                           - timing['backoff_wait'] - timing['prepare']
                                           - timing['guard'], 2)
            self.last_timing = timing
            self.last_run = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            self.last_error = None
            self.log(f"AI 建议 {len(sugg)} 条（模型 {config.get('model', '?')}）", "SUCCESS")
            self.log(f"耗时明细：总 {timing['total']}s = 准备 {timing['prepare']}s + "
                     f"模型调用 {timing['llm']}s + 批间隔 {timing['batch_wait']}s + "
                     f"限流退避 {timing['backoff_wait']}s + 收尾 {timing['guard']}s"
                     f"（净判断 {timing['net_judge']}s，共 {timing['llm_calls']} 次请求，"
                     f"重试 {timing['retries']} 次）")
            for s in sugg:
                self.log(f"  → {s['campaign_id']} {s['action']} {s.get('budget_change_pct', '')}% {s['reason']}")
            return sugg

        except Exception as e:
            self.last_error = str(e)
            self.log(f"AI 决策失败 {e}", "ERROR")
            self.log(traceback.format_exc(), "ERROR")
            return []

    # ---------- 后台循环 ----------
    def start(self):
        """启动后台定时循环。

        🔴 2026-09-30 修「静默死状态」：原实现是
            if self._thread and self._thread.is_alive(): return
            self._stop.clear()
        这个顺序有个漏洞 —— stop() 之后线程不一定马上死：它可能正卡在一轮 LLM 调用里
        （一轮最长 ~40s），而 stop() 的 join(timeout=5) 会超时返回。此时立刻重新「启用 AI」：
        is_alive() 仍为 True → 直接 return（不建新线程），而 _stop 还挂在 set 状态 →
        旧线程跑完当前轮就退出 → **界面显示「运行中（每3分钟判断）」，实际一个循环都没有**。
        不报错、不告警，就是不再判断了。

        改法用「代号」而不是「线程活没活」判断：
          · stop() 让 _gen 自增 → 正在跑的旧循环下一轮循环条件就不成立，必然退出；
          · start() 总是把 _gen 自增并起**新**线程，旧线程即使还活着也只是收尾，
            不会再抢到循环（它那代的代号已经作废）；
          · 只有「当前这代循环活着且没被要求停」才算已经在跑，才早退。
        """
        if self.is_looping():
            return
        # 每一代循环配一个**独立**的停止事件（不是复用同一个 Event）。
        # 复用会留一个尾巴：stop() 后旧线程跑完当前轮会停在 _stop.wait(interval) 里，
        # 而 start() 把 _stop.clear() 清掉后它的等待又变成「有效等待」→ 要等满一个间隔
        # （默认 3 分钟）才醒过来检查退出条件 → 白挂一个线程。
        # 换成每代独立：stop() set 的是旧那代的事件，旧线程的 wait 立刻返回 → 马上退场。
        self._stop = threading.Event()
        self._gen += 1
        self._loop_gen = self._gen
        self._thread = threading.Thread(target=self._loop,
                                        args=(self._gen, self._stop), daemon=True)
        self._thread.start()
        self.log("AI 调度线程启动", "SUCCESS")

    def stop(self):
        self._stop.set()
        # 代号自增 = 立刻作废在跑的那个循环（哪怕它正卡在一轮 LLM 调用里）。
        # 不这样做的话，它跑完当前轮会再看一眼 _stop —— 如果用户在这之前又点了启用，
        # _stop 会被 start() 清掉，旧循环就会「诈尸」和新的那个循环并行跑。
        self._gen += 1
        t = self._thread
        if t and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=5)
        self.log("AI 调度线程停止")

    def _loop(self, gen, stop_evt):
        """后台循环。gen 是本线程的代号、stop_evt 是本代的停止事件（见 start 注释）。

        ⚠️ 这里必须用**参数传进来的** stop_evt，不能读 self._stop ——
        self._stop 已经是「下一代」的事件了，读它会让旧线程永远醒不过来。
        """
        while gen == self._gen and not stop_evt.is_set():
            try:
                self.run_once()
            except Exception as e:
                self.log(f"调度循环异常 {e}", "ERROR")
            cfg = self.load_config()
            interval = max(1, int(cfg.get('interval_minutes', 3)))
            stop_evt.wait(interval * 60)

    # ---------- 查询 ----------
    def get_suggestions(self):
        with self._lock:
            return list(self.suggestions)

    def clear_suggestions(self):
        with self._lock:
            self.suggestions = []

    def is_looping(self):
        """后台定时循环线程是否活着（「AI 已启用」的语义）。

        ⚠️ 别把它和 is_running() 混用 —— 2026-09-30 踩过：本文件原先在类末尾也叫
        `is_running`，而 Python 里**后定义的同名方法会覆盖先定义的**，于是 api_fb 里那句
        「正在判断就跳过」实际判的是「循环线程活着」→ 只要启用了 AI，手动「立即判断」
        就永远被拒。现改名为 is_looping，`is_running` 专指「有一轮 run_once 在执行中」。

        判据是「当前这一代循环还活着且没被要求停」，不是裸的 thread.is_alive() ——
        stop() 之后线程可能还在收尾（卡在一轮 LLM 里），那时它已经不算在循环了。
        """
        t = self._thread
        return bool(t and t.is_alive() and self._loop_gen == self._gen
                    and not self._stop.is_set())
