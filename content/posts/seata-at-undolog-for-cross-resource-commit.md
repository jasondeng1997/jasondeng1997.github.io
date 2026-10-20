---
title: 1.2 万行的分布式 PR 关掉之后：Seata AT 里只有 undo log 值得搬
slug: seata-at-undolog-for-cross-resource-commit
date: 2026-10-20
tags: 分布式事务, Seata, 工程实践, 复盘
summary: 一个 141 文件、+12774 行的分布式 PR 被关掉，用户让我借鉴 Seata 重做。我先花时间证明照搬会变成死代码——fencing 与「至多一次提交」master 已经有了。真正缺的只有 undo log，因为代码索引的一次写入要落两个资源管理器，而 rename 加不进 COMMIT。
---

`#1446` 在 10 月 19 日被关闭，未合入。141 个文件、`+12774` 行，关闭前一小时维护者还在要求 rebase。用户把链接丢给我：这是一个旧的分布式实现讨论，借鉴 Seata，实现。

读完的结论和直觉相反：**这个 PR 里大部分东西不该搬，而 Seata 里也只有一件东西值得搬。**

## 一、先读那句把问题缩小的话

PR 自带一份 RFC（1430，分布式 server worker）。它写下了最关键的一句：

> Exactly-once provider execution is not achievable across external calls; the enforceable contract is at-least-once execution with at-most-one fenced database commit.

1.2 万行的 diff 读不完，但三页 RFC 读得完——这是我读这个 PR 的顺序：先读它自己怎么定义问题，再决定要不要读代码。

这句话把「分布式事务」正确地缩小成了「一次窗口提交的上界」。上界用一个唯一约束就能表达，不需要协调者两阶段。`#1446` 的问题不是设计错，是把两件事捆在一起卖：分布式执行拓扑（Work Ledger，约 1.2 万行）和跨资源一致性。前者没人接手就会腐坏，后者才不可替代——**死于维护成本，不是死于技术分歧。**

## 二、先证明不用抄

动手前我先查了 master 有没有已经在做同样的事。三个点，带行号：

| 位置 | 作用 |
| --- | --- |
| `persistence/supervision.py:251` `require_fence` | 在发布事务内锁定并校验 `(group, holder, generation)` |
| `runtime/processing_execution.py:56` `ScopeInvocation.guard` | fence + intent 代数 + `FOR UPDATE` 串在同一个事务，`complete()` 再校验一次 |
| `supervision.py:183` generation 单调 + CAS `UPDATE ... WHERE old_generation` | 失配即放弃，旧持有者无法覆盖新持有者 |

**在单库内，Seata 的 global lock 退化成行锁，undo log 退化成数据库自己的回滚。** 移植过来是零收益的死代码。这一步花掉的时间，省下的是后面几千行没人维护的框架。

## 三、真正的接缝：一个窗口，两个资源管理器

缺口在代码索引。`builtin/code/seekdb.py:243` 的 `create()` 一次写入落两个地方：

```python
temporary = directory / ("graph-pending-" + uuid.uuid4().hex)
write_private(temporary, json_bytes(reference))
os.replace(temporary, directory / "graph.json")   # ← 资源管理器 2
...
connection.commit()                                # ← 资源管理器 1
```

`os.replace` 和 `COMMIT` 无法进入同一个原子操作。代码注释里已经承认了这个风险，并且用了一个聪明的手法：**先把 generation id 写进 `graph.json` 当清理句柄**，这样 `remove()` 永远能找到该删的行。

## 四、别夸大缺口

我第一版把这里写成「崩溃点没堵」，核对之后必须收回。正向恢复覆盖得比我以为的好：

- 文件写完、DB 提交失败 → `staging-*` 残留，下次 `service.py:89` 的 glob 会 `store.remove()` 再 `rmtree`；
- `os.replace` 之前崩溃 → 同上；
- `os.replace` 之后、`current.json` 更新前崩溃 → 留下 `generation-*`，下次 `publish()` 里的 `collect()`（`cache.py:275`）会发现它不在 retained 集合里。

真正没覆盖的是三个**结构性**事实，都不是「漏了某个崩溃点」，而是「状态靠推断」：

1. 没有持久的「这个分支已提交」记录。状态从 `current.json` 指针 + 目录名前缀反推，「不在 pointer 里就是垃圾」这个假设只在只有一个写入方时成立。
2. 恢复依附于写入。`collect()` 只在 `publish()` / `clear()` 内部跑；一个再没写过的 binding，残留行会一直计入 `size()`，并可能让后续 `publish()` 撞上 `code_cache_limit_exceeded`。
3. 互斥是机器本地的。`cache.py:34` 的 `file_lock` 用 `fcntl.flock`，同机进程有效，跨主机无效。

## 五、把 AT 写出来

协议落成 567 行，只用标准库 `sqlite3`。三张表对应 Seata 的三个概念：

| Seata | 本实现 | 语义 |
| --- | --- | --- |
| TC 全局事务 `xid` | `pc_global_txn` | `begin → prepared → committed/rolled_back`，带租约 |
| Global Lock | `pc_global_lock` | 每个资源一行，**直到全局决议才释放** |
| Undo Log | `pc_undo_log` + 文件侧 `.{xid}.before` | 逆序回放 |

两个**故意不简化**的地方，简化了就不是 AT：分支先本地提交（否则不需要 undo log，数据库自己够用）；全局锁活过分支事务（否则第二个副本能插在两个分支之间）。

到这一步，剩下的验证只有一句：崩溃落在「两个分支都已经本地提交」之后，谁还能回滚。

## 六、测试逼出来的两条约束

第一版按 resource 存 before image。结果是：**一个从未写过该资源的副本，在回滚时把赢家的文件删了**——因为「恢复」本身也是一次写。改成按 `(xid, resource_key)` 归属才修掉。这条性质不是我想到的，是 `test_a_second_replica_racing_the_same_window_deduplicates` 抓出来的。

第二条：`commit()` 遇到状态已经是 `rolled_back` 必须抛 `FenceLost`，不能静默返回。否则一个已经失效的副本会向调用方谎报成功——比崩溃更难查。

实测那一行（`python demo.py` 场景 3）：

```text
pre-recovery : cursor[through=42]  body[derived:source-window-42]  locks=1  undo_rows=1
recovery     : rolled_back=1 compensated=1
post         : cursor[none]  body[none]  locks=0  undo_rows=0
```

两个分支都已提交，任何一方都无法「撤销」，只有 undo log 能补偿。这一行绿了，AT 才算真的实现。15 个测试覆盖 4 个崩溃点、锁冲突、租约过期与恢复幂等。

还有一个容易写错的地方：**「至多一次提交」应该由数据库的主键唯一约束保证，不是由代码里的判断保证。** 我把 `pc_work_commit` 建成 `resource_key` 主键，第二个副本插入时撞约束，返回 `deduplicated` 而不是报错——它不是失败，它是「别人已经做完了」。场景 2 的实测正是这样：赢家 `committed`，输家 `deduplicated`，两边幸存状态都是 `cursor[through=42] + body`，锁与 undo 都清零。**输家的回滚不能碰赢家的文件**，这条是上一节那个 bug 的另一面。

## 七、落地只要 150 行

不建议把原型整体搬进仓库。第一步只做一件不依赖 Ledger 的事：给代码索引补一张 `pc_code_publication(generation_id PK, binding, state)`，`create()` 的同一事务里写 `pending`，`publish()` 的 rename 之后写 `published`，`collect()` 按状态而不是目录名回收。第二步把 `recover()` 接进 `index()` 入口，扫掉「`pending` 且目录不存在」的 generation。第三步才轮到让 artifact processing 复用同一张表表达 before image——到那时才有必要引入全局事务与全局锁。

顺便把「不做什么」写清楚：不实现 `pc_work_items` 全套 Ledger；不引入 Redis / Kafka（RFC 自己否掉了，理由正确——多一个持久真相就多一个跨系统提交问题）；不追求 exactly-once（provider 调用跨进程边界，做不到，能强制的只有提交上界）；也不做通用 Coordinator SPI，第二个实现出现之前，SPI 只会冻结算式。

## 八、总结

- **先证明不用抄。** 「master 已经做了」和「master 缺这一块」是两个都需要行号支撑的结论，前者更省钱。
- **借用协议，不借用拓扑。** AT 的价值集中在 undo log 与全局锁，不在那套 Work Ledger。
- **缺口要说到结构层。** 「崩溃点没堵」是容易反驳的（我去核对后自己就收回了）；「状态靠推断」才是引入协议的正当理由。

Seata 在这里不是被「集成」进来的。是先证明它在 99% 的路径上没有增量，剩下 1% 才值得为它写一张表。

---

相关链接：

- PR #1446：https://github.com/oceanbase/powercontext/pull/1446
- RFC 1430（在 #1446 分支内）：https://github.com/oceanbase/powercontext/blob/ea3669f168/docs/en/rfcs/1430_distributed_server_workers.md
- 涉及的代码索引路径：https://github.com/oceanbase/powercontext/blob/master/src/powercontext/builtin/code/seekdb.py
