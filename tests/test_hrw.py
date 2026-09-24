# -*- coding: utf-8 -*-
"""`yiban.engine.hrw` 的契约用例：纯函数 HRW 分工（虚分片 + argmax 归属）。

标签：A · 调度：计划与分片
覆盖：虚分片层（vshard_of 确定性、值域、吃 day、blake2b 编码定值、v_for
   阈值）与归属层（owner_of / shards_of / assignment
   三方一致、换天重排、增删执行体的迁移量、均衡度包络、平局字典序、空执行体）共十一条契约。
对应实现：yiban/engine/hrw.py（vshard_of、v_for、owner_of、shards_of、assignment、V_DEFAULT）；取值写进
   yiban/store/queue_store 的 sign_tasks.vshard。
关键断言：分片归属必须跨进程一致：判据靠两个全新解释器互验，并且同时钉住「内置 hash()
   每进程不同」——陷阱没 armed 时这条用例是空转。vshard 层也必须吃
   day（它要落库，只在 owner
   层换天等于每天把同一批账号压回同一个执行体）。blake2b
   的编码被定值钉死：改编码等于作废当天全站计划。增删执行体只迁移约
   1/K、平局与执行体顺序无关（否则两个执行体会同时领同一分片）。
依赖：起 2 个全新子解释器（sys.executable，PYTHONHASHSEED=random，超时
   120s）；其余为纯函数。不建库、不发网络请求。整文件在本机执行，无 skip。

**两处容差比设计文档宽，依据是实测**（HRW 的归属是"每片独立均匀选主"，分片数与每片
人数都服从多项分布）：
- 256 片 / K=8 时执行体分片数的理论标准差 `√(V·(1/K)·(1−1/K)) ≈ 5.3` 片（32 片的
  ≈16.5%），故"各执行体 32±5%"（±1.6 片）不是这套算法能有的精度：实测 365 天里没有
  一天满足，最好的一天跨度也有 3 片；
- 10000 人落 256 片时每片理论标准差 `√(n·p·(1−p)) ≈ 6.2` 人，而"±40%"（±2.5σ）在
  256 个格子同时受检下等于要求最大偏差 ≤2.5σ（最大偏差期望已 ≈3.3σ）：实测 365 天
  只有 10 天满足。
两处都改判 `mean ± 4.5σ` 包络（σ 各按自己的多项分布算）：4σ 时人数包络全年有 13/365
天不满足（日期一换就误报），4.5σ 降到 1/365；分片数包络 4σ 起就是 0/365。选定日
2026-09-22 实测最差偏差：人数 19.9 / 上限 28.1、分片数 6 / 上限 23.8。包络仍能拦住
哈希坏掉（分片塌缩、执行体吃独食、内置 hash 每进程随机化）。
"""
import json
import os
import subprocess
import sys
import unittest
from unittest import mock

from yiban.engine import hrw

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DAY = "2026-09-22" # 模块 docstring 里的包络容差是按这一天的实测定的，换日期要重测而不是改常数
NEXT_DAY = "2026-09-23"
V = hrw.V_DEFAULT

#: 第 2 条用的子进程脚本：同一次运行里既回话 hrw 的取值，也回话内置 `hash()` 的取值
#: （后者必须每进程不同，否则说明哈希随机化没开、这条陷阱用例是空转）
_CHILD_SRC = (
    "import json, sys;"
    "sys.path.insert(0, sys.argv[1]);"
    "from yiban.engine import hrw;"
    "print(json.dumps({"
    "'pyhash': hash('13800000000'),"
    "'vshard': hrw.vshard_of('13800000000', sys.argv[2]),"
    "'owner': hrw.owner_of(7, ['w0', 'w1', 'w2'], sys.argv[2]),"
    "}))"
)


def _phone(i):
    return f"138{i:08d}"


def _child_probe(day):
    """起一个全新解释器求值，返回其输出 dict（PYTHONHASHSEED=random 保证哈希随机化开着）。"""
    env = dict(os.environ)
    env["PYTHONPATH"] = BASE
    env["PYTHONHASHSEED"] = "random" # 显式打开随机化：不开时 hash() 恰好稳定，这条陷阱检查就成了空转
    r = subprocess.run(
        [sys.executable, "-c", _CHILD_SRC, BASE, day],
        cwd=BASE, env=env, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=120,
    )
    if r.returncode != 0:
        raise AssertionError(f"子进程求值失败: {r.stdout}{r.stderr}")
    return json.loads(r.stdout.strip().splitlines()[-1]) # 只取末行：子进程可能先吐一条警告，整段喂 json 会炸


class HrwIdentityTest(unittest.TestCase):
    def test_vshard_is_deterministic_and_within_range(self):
        """同一 `(phone, day)` 反复求值逐字相等，且落在 [0, v)。"""
        for phone in (_phone(0), _phone(1), "13900000001", "18612345678", "0000"):
            vs = hrw.vshard_of(phone, DAY)
            self.assertIsInstance(vs, int)
            self.assertTrue(0 <= vs < V, f"{phone} 的分片号越界: {vs}")
            self.assertEqual(vs, hrw.vshard_of(phone, DAY), "同输入两次结果不同")
            self.assertTrue(0 <= hrw.vshard_of(phone, DAY, 64) < 64, "分片号未随 v 收缩")

    def test_vshard_is_stable_across_processes(self):
        """跨进程一致：两个全新解释器 + 哈希随机化开启，取值仍相同。

        内置 `hash()` 对字符串每进程随机化（`PYTHONHASHSEED` 未固定时），一旦实现里
        误用它，执行体之间对同一账号的 owner 判断就会分叉（重复登录）。所以这里必须
        验到进程边界，且同时确认陷阱是"armed"的——`hash()` 进程间确实不同。
        """
        a, b = _child_probe(DAY), _child_probe(DAY)
        self.assertNotEqual(a["pyhash"], b["pyhash"],
                            "两个子进程的内置 hash 相同，说明哈希随机化没开、用例空转")
        self.assertEqual(a["vshard"], b["vshard"], "分片号跨进程不一致")
        self.assertEqual(a["vshard"], hrw.vshard_of("13800000000", DAY),
                         "子进程与本进程的取值不一致")
        self.assertEqual(a["owner"], b["owner"], "归属跨进程不一致")
        self.assertEqual(a["owner"], hrw.owner_of(7, ["w0", "w1", "w2"], DAY))

    def test_vshard_depends_on_day(self):
        """vshard 层同样必须吃 `day`：换天后同一批账号的分片号整体重排，且编码被钉住。

        vshard 是要落库的（`sign_tasks.vshard`）——day 只在 owner 层生效不够：那等于每天
        把同一批账号压回同一个执行体，虚分片削峰在跨天维度上失效。owner 层的跨天重排
        （`test_day_change_reshuffles_owners`）判别不了这里，所以本层单独验。
        末尾两个定值钉的是**编码本身**（`\\x1f` 连接 + blake2b 前 8 字节 + 大端）：当天
        已落的计划与库里的 vshard 都按它算，改编码等于作废当天全站计划。
        """
        phones = [_phone(i) for i in range(512)]
        moved = sum(1 for p in phones if hrw.vshard_of(p, DAY) != hrw.vshard_of(p, NEXT_DAY))
        self.assertGreaterEqual(moved, len(phones) * 0.3,
                                f"换天后只有 {moved}/{len(phones)} 个账号换片，vshard 没吃 day")
        self.assertEqual(hrw.vshard_of(_phone(0), DAY), 203)
        self.assertEqual(hrw.vshard_of(_phone(0), NEXT_DAY), 218)

    def test_v_for_thresholds(self):
        """分片数按规模选：n<500→64、n<3000→128、否则 256。"""
        self.assertEqual(hrw.v_for(499), 64)
        self.assertEqual(hrw.v_for(500), 128)
        self.assertEqual(hrw.v_for(2999), 128)
        self.assertEqual(hrw.v_for(3000), 256)
        self.assertEqual(hrw.v_for(0), 64)
        self.assertEqual(hrw.v_for(30000), 256)

    def test_shard_population_has_no_holes_and_stays_in_envelope(self):
        """10000 个不同账号同一天落 256 片：无空洞（每片必有账号）、每片人数在 ±4.5σ 内。"""
        n = 10000
        counts = [0] * V
        for i in range(n):
            counts[hrw.vshard_of(_phone(i), DAY)] += 1
        holes = [s for s, c in enumerate(counts) if c == 0]
        self.assertEqual(holes, [], f"出现空分片（哈希塌缩）: {holes[:8]}")
        mean = n / V
        sigma = (n * (1 / V) * (1 - 1 / V)) ** 0.5
        worst = max(abs(c - mean) for c in counts)
        self.assertLessEqual(
            worst, 4.5 * sigma,
            f"分片人数偏差 {worst:.1f} 超出 4.5σ（mean={mean:.1f}, sigma={sigma:.2f}）")


class HrwAssignmentTest(unittest.TestCase):
    def test_day_change_reshuffles_owners(self):
        """换天后归属整体重排：至少 30% 的分片换主（跨天无相关性）。"""
        executors = ["w0", "w1", "w2"]
        today = hrw.assignment(executors, DAY)
        tomorrow = hrw.assignment(executors, NEXT_DAY)
        self.assertEqual(sorted(today), list(range(V)), "分片键应为 0..V-1 的全部整数")
        self.assertTrue(set(today.values()) <= set(executors))
        changed = sum(1 for v in range(V) if today[v] != tomorrow[v])
        self.assertGreaterEqual(changed, V * 0.3,
                                f"换天后只换了 {changed}/{V} 个分片的主，跨天未重排")

    def test_growth_migrates_about_one_over_k(self):
        """加一个执行体只迁移约 1/K 的分片，且迁走的片全部落到新执行体。"""
        before = hrw.assignment([f"w{i}" for i in range(10)], DAY)
        after = hrw.assignment([f"w{i}" for i in range(11)], DAY)
        changed = [v for v in range(V) if before[v] != after[v]]
        self.assertGreater(len(changed), 0, "加入新执行体却一片都没迁移")
        self.assertLessEqual(len(changed), V / 11 * 1.5,
                             f"迁移了 {len(changed)} 片，超出 1/K 迁移的容差")
        owners = {after[v] for v in changed}
        self.assertEqual(owners, {"w10"},
                         f"迁移目标不是新执行体（HRW 不该在旧执行体之间搬）: {owners}")

    def test_balance_within_envelope(self):
        """均衡度：K=8、256 片时各执行体的分片数落在 HRW 理论抖动的 4.5σ 包络内。"""
        executors = [f"w{i}" for i in range(8)]
        board = hrw.assignment(executors, DAY)
        counts = [sum(1 for v in range(V) if board[v] == e) for e in executors]
        mean = V / len(executors)
        sigma = (V * (1 / 8) * (1 - 1 / 8)) ** 0.5
        worst = max(abs(c - mean) for c in counts)
        self.assertLessEqual(
            worst, 4.5 * sigma,
            f"分片数偏差 {worst:.1f} 超出 4.5σ（mean={mean}, sigma={sigma:.2f}）: {counts}")

    def test_shards_of_matches_owner_and_partitions_space(self):
        """`shards_of` 与 `owner_of`/`assignment` 一致，且各执行体的分片集互斥且覆盖 0..V-1。"""
        executors = ["w0", "w1", "w2"]
        board = hrw.assignment(executors, DAY)
        seen = set()
        for e in executors:
            mine = hrw.shards_of(e, executors, DAY)
            self.assertIsInstance(mine, tuple)
            self.assertEqual(list(mine), sorted(mine), "分片集应为升序 tuple")
            self.assertEqual(mine, tuple(v for v in range(V) if board[v] == e))
            self.assertEqual(mine, tuple(v for v in range(V)
                                          if hrw.owner_of(v, executors, DAY) == e))
            self.assertEqual(seen & set(mine), set(), "两个执行体拿到同一分片")
            seen |= set(mine)
        self.assertEqual(seen, set(range(V)), "各执行体的分片集并集不是全部分片")

    def test_tie_break_is_order_independent(self):
        """平局按字典序最小者：执行体顺序（含乱序）不影响归属。"""
        v = 7
        self.assertEqual(hrw.owner_of(v, ["b", "a"], DAY),
                         hrw.owner_of(v, ["a", "b"], DAY))
        for shard in range(V):
            self.assertEqual(hrw.owner_of(shard, ["b", "a"], DAY),
                             hrw.owner_of(shard, ["a", "b"], DAY),
                             f"分片 {shard} 的归属随执行体顺序变化")
        # 真平局只有把打分抹平才构造得出来（64 位空间下自然撞车概率可忽略）；
        # 不抹平的话"排序再 max"与"直接 max"在测试里表现一样，这条就是空转。
        with mock.patch.object(hrw, "_h", return_value=42):
            self.assertEqual(hrw.owner_of(v, ["b", "a"], DAY), "a",
                             "平局未判给字典序最小的执行体")
            self.assertEqual(hrw.owner_of(v, ["a", "b"], DAY), "a")
            self.assertEqual(hrw.owner_of(v, ["c", "a", "b"], DAY), "a")

    def test_empty_executors_do_not_raise(self):
        """无执行体时不抛：owner 为空串、分片集为空 tuple、计划表分片全空主。"""
        self.assertEqual(hrw.owner_of(3, [], DAY), "")
        self.assertEqual(hrw.shards_of("w", [], DAY), ())
        board = hrw.assignment([], DAY)
        self.assertEqual(sorted(board), list(range(V)))
        self.assertEqual(set(board.values()), {""})


if __name__ == "__main__":
    unittest.main(verbosity=2)
