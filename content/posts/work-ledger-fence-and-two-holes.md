---
title: fence 写对了，洞还在：拆一个被关掉的分布式任务系统
slug: work-ledger-fence-and-two-holes
date: 2026-10-23
tags: 分布式, 任务队列, OceanBase, 工程实践, 复盘
summary: #1446 的 Work Ledger 把分布式正确性全部押在数据库上：唯一 key 去重、lane 严格串行、单调 fence、库时间判过期。协议很干净，但 review 里两个 P1 洞都穿过它自己声明的不变量——一个在「读快照」和「写占位」的缝里，一个在一个 handler 的提前提交里。
---

`#1446` 在 10 月 19 日关掉，未合入：141 个文件、`+12774` 行、24 个 commit。几天前我写过它在「跨资源提交」上该不该借 Seata；这篇换一把刀，只拆它自己那套 Work Ledger —— 一个把分布式正确性全部押在数据库上的执行框架。

结论先给：**协议是干净的。洞不在协议里，在协议和代码的接缝上。**

## 一、它要换掉的，是三个「只有一个进程」的假设

旧实现的底座是 APScheduler 加一个 SQLite sidecar。上面叠着三条假设：

| 假设 | 扩副本那天会怎样 |
| --- | --- |
| handler 注册在进程内 | 新副本不知道自己要跑什么 |
| MCP session 在进程内 | 负载均衡必须粘性，否则上一个请求的会话找不到 |
| 调度状态在本地文件 | Scheduler 没有 failover，只能重启 |

三条假设的共同点是：**它们都不是被写下来的约束，而是「系统只有一个进程」这条隐性经验的不同说法。** 隐性经验最麻烦的地方在于，它不会在评审里被反驳，只会在扩副本的那天同时失效。

RFC 干的事就是把这些经验换成显式约束：租约、fence、单调序号、唯一 key、启动时校验。方向是对的，下面都建立在这个前提上。

## 二、一次窗口提交要走四段路

![Work Ledger 架构：入队、领取、事务外执行、fenced 提交四段路](/assets/images/posts/work-ledger-fence-and-two-holes/01-architecture.svg)

① 入队是短事务：定窗口、去重、写占位。① 领取也是短事务：拿 lane 队头，签一个 fence。② 调 provider 在事务外。③ 一个 COMMIT 里先验 fence，再落领域写。

这四段里只有 ② 慢。它慢到不能用事务包住：一次 extraction 几秒到几十秒，事务持有多久，连接就被钉住多久，锁的持有时间由别人的 P99 决定。这是把可用性交给了外部服务。

所以整个设计能压成一句，也是 RFC 自己写下的那句：

> 跨进程调用做不到 exactly-once；能强制的契约是 at-least-once 执行，加 at-most-once 的 fenced 提交。

「执行可能重复，提交只许一次」。后半句不靠协调者，靠一个唯一约束加一个单调号。

## 三、八张表，其实是三种真相

| 要表达的东西 | 落在哪 | 靠什么保证 |
| --- | --- | --- |
| 唯一性 | `pc_work_keys` | 主键，一个逻辑窗口只能有一个占用者 |
| 顺序 | `pc_work_lanes.head_sequence` | 每个一致性域严格串行，只认队头 |
| 状态 | `pc_work_items.state_version` | 每次迁移加一，CAS 写回 |
| 领导权 | `pc_scheduler_leases.fence` | 单调递增，只增不减 |
| 审计 | `pc_work_attempts` | append-only，谁在什么时候试过 |
| 扫描进度 | `pc_scheduler_scans` | keyset continuation，崩了重扫也安全 |
| 兼容性 | `pc_runtime_members` | 哪些角色活着、支持哪个 payload 版本 |
| 配额 | `pc_rate_limit_windows` | 跨副本共享的固定窗口 |

值得单独说的是**唯一性和顺序被拆成了两张表**。很多自研队列把这两件事压在 `status` 上：「没跑过就是没跑过」。一旦要求「同一 lane 严格按序、不同 lane 并行」，`status` 就不够用了——你需要一个东西表达「谁现在是队头」，那是 lanes 的 `head_sequence`，不是 items 的 `status`。混着用，早晚会写出「为了不重复而阻塞了顺序」的代码。

## 四、claim 为什么不用 SKIP LOCKED

`work.py:314` 的候选扫描先 join lanes 只取队头，`limit max(32, limit * 4)`，然后对每个候选走 `_claim_candidate`（`work.py:826`）。那里的锁序是固定的：lane → logical key → work item，锁完复核队头、复核状态与过期，最后 `update ... where state_version = 旧值`，`rowcount != 1` 直接抛 `StaleWorkClaimError`。

不用 SKIP LOCKED 的理由有两条，都不是性能问题：

1. 严格串行要求「只看队头」。SKIP LOCKED 的语义是「跳过被占的、拿下一个」，一旦跳过队头去拿后面的 sequence，串行就没了。
2. 跨表锁序没法用 SKIP LOCKED 表达。要同时锁 lane 行和 work 行，跳过会让加锁顺序不确定，死锁概率上升。

代价也真实：高并发下多个 worker 会重复扫同一批队头，claim 变成「worker 数 × 扫描宽度」的无效读。这是拿吞吐换确定性。在 LLM 任务这个量级完全划算——一次 claim 毫秒级，一次推理几十秒。

## 五、fence 只认数据库时间

说人话：这是一台取号机。每次有人领到活，号码必须比上一次大；写入方只认它见过的最大号，收到更小的号直接拒。这样即使 A 卡了 30 秒、锁早过期、B 已经干完，A 醒来交的仍然是旧号，被挡在门外。

三个细节决定它到底能不能用：

1. fence 取 `lease_fence + 1`（`coordination.py:115`），只增不减，新 owner 一定拿到更大的号。
2. 过期判断用数据库时间，应用时钟只用于 poll 和 deadline。
3. 这里踩过一个真坑：最初写的是 `CURRENT_TIMESTAMP(6)`，review 指出它是 session 时区，非 UTC tenant 上会把整个协调窗口平移，后来改成 `UTC_TIMESTAMP(6)`。

为什么不能用 `GET_LOCK()`：session 级锁，连接池里连接会被复用，锁跟着连接走而不是跟着逻辑 owner 走，释放和持有的边界都对不上。RFC 把它列进「未采用方案」是对的。

> 经验法则：锁解决「减少重复干活」（efficiency），fence 解决「不许写坏数据」（correctness）。前者失效只是浪费，后者失效是脏数据。Kleppmann 2016 年批 Redlock 批的就是这个混淆；而 Chubby 的 sequencer 在 2006 年就把答案写下了。

Memory cursor 属于 correctness 那一类：写错就是「同一批数据被处理两遍、cursor 跳了一格」。所以这里必须 fence，不能只靠 TTL 锁。

## 六、洞一：快照和入队之间那条缝

![两个 P1 洞：快照与入队之间的间隙，以及绕过 fence 的提前提交](/assets/images/posts/work-ledger-fence-and-two-holes/02-two-holes.svg)

`durable_scheduler.py:179` 的 `page()` 在事务外取快照，随后每个 spec 各开一个事务入队。中间这段时间里，可能塞进一次 manual flush 的完整提交。

时间线是这样的：t0 Scheduler 快照出 `0 → 2`；t1 manual flush 入队 `0 → 1`，logical key 相同，于是 join 到同一个 work；t2 manual 提交成功，cursor 变成 1，`complete()` 释放 logical key、队头前移；t3 Scheduler 拿着 t0 的快照入队——key 已经空了，插入成功，于是库里多了一个 payload 写着 `after=0 / through=2` 的新 work；t4 Worker 跑它，`_require_exact_cursor` 发现库里 cursor 已是 1，抛 `cursor_changed`。

**去重救不了这个洞**，因为去重的前提是「key 已经被占」。而释放 key 是设计要求的，不释放后面窗口永远排不进来。释放之后，陈旧快照就有位置了。它插进去的不是重复工作，是一个**指向过去的窗口**。

后果也不是「多跑一次」，而是「这条 lane 从此堵住」：终态失败会保留 key 与 lane 占位（这是防 poison window 无限重建的设计），于是后续所有窗口都排在这个坏队头后面，全部阻塞。

这个洞最有意思的地方是：**它不是谁粗心，是两个正确决定撞在一起。**「释放 key 让后续窗口能进」是对的，「终态失败保留占位」也是对的，两个都对，缝在中间。

修法有两条：入队前在 lane 锁内复核 cursor；或者把被超越的窗口退休掉、重扫它剩下的 Source。都不难，但都得先承认一件事——**快照不是承诺。**

## 七、洞二：有一个 handler 自己提交了

Memory 和 Experience 的形状是对的。`prepare()` 在短事务里读 cursor、取窗口行，然后**出事务**跑模型，返回一个 commit 闭包；这个闭包在 `complete()` 的事务里执行（`work_handlers.py:338`）：先 `FOR UPDATE` 读 cursor、`_require_exact_cursor` 复核、apply、save，全在一个事务内。

Profile 不一样。`prepare()` → `_catch_up()` → `self._profiles.flush()`，而 `flush()` 内部自己 commit，Profile 和 Profile cursor 都落了地。它返回的是 `PreparedWork(result=...)`，没有 commit（`work_handlers.py:516`）。

于是 `complete()` 里 `_verify_claim` 发现租约过期、抛 `StaleWorkClaimError`、ledger 回滚——但领域写已经在数据库里了。另一个 worker 接手再跑一次，看到 cursor 已推进，走 `already_committed` 收工。两边都「成功」，ledger 里 attempt 1 是失败，领域状态却前进了。

为什么会这样：`PreparedWork.commit` 的类型是 `WorkCommit | None`。这个 `| None` 原本是给「不需要额外领域写」的 handler 留的，但它同时是一个合法的逃生门——任何 handler 都能在自己的 `prepare()` 里偷偷提交，类型系统不会拦。

> 不变量写在 RFC 里，只对读过 RFC 的人有效；写在类型里，才对没读过的人有效。

修法不是加注释。让 `commit` 不可为空，并让 `prepare()` 根本拿不到能提交的连接——这样这类错误在写代码时就写不出来。

## 八、为什么被关掉

24 个 commit 里 10 个是 merge，占 42%。141 个文件横跨持久化、运行时、HTTP、客户端、MCP、部署六个面，`+8125` 行集中在 `src/powercontext` 的 59 个文件里。46 天里维护者提了四次 rebase，关掉前一天最后一条评论还是「please update & resolve conflicts」。

这不是死于技术分歧，是死于长命分支的腐坏：每 rebase 一次，就要把跨六个面的不变量重新验证一遍，验证成本随手写成本线性上涨，而收益要等整套拓扑上线才兑现。

## 九、能搬走的三条

1. **时间只认数据库。** 应用时钟做 poll 和 deadline 就够了；判租约、判窗口、判重试，一律用库时间。
2. **fence 必须罩住领域写，不是只罩住 ledger 行。** 校验在事务开头、领域写在事务结束，中间任何一次独立 commit 都会把 fence 变成装饰。
3. **「读快照 → 判断 → 写占位」必须同一个事务边界，或者判断必须可重做。** 唯一 key、去重、幂等，都替代不了这一点。

还有一条不算技术：一个 PR 同时改协议、改 schema、改 HTTP 契约、改部署拓扑，就没法分批合。分批合是长命分支唯一的解药。

## 边界

这篇只谈 `#1446` 自己的 Work Ledger。它没有合入，master 上也没有 `pc_work_items`；文中代码引用都取自 PR 分支 `feat/distributed`（最后 commit `ea3669f1`）。两个 P1 来自 10 月 15 日的 review，当时未修复，PR 四天后关闭。

也不能由此推出「数据库当队列不行」。在 LLM 任务这个量级——一次 claim 毫秒级、一次推理几十秒——库当队列是划算的：省掉一个持久真相、省掉一次跨系统提交。把同样的设计推到万级 QPS 的毫秒任务上，结论会反过来，那时候瓶颈是表膨胀和连接池，不是协议。

---

相关链接：

- PR #1446：https://github.com/oceanbase/powercontext/pull/1446
- RFC 1430（在 PR 分支内）：https://github.com/oceanbase/powercontext/blob/ea3669f168/docs/zh/rfcs/1430_distributed_server_workers.md
- Kleppmann, How to do distributed locking：https://martin.kleppmann.com/2016/02/08/how-to-do-distributed-locking.html
- Burrows, The Chubby lock service (2006)：https://research.google/pubs/the-chubby-lock-service-for-loosely-coupled-distributed-systems/
