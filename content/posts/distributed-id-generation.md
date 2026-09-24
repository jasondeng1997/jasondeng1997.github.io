---
title: 分布式 ID 生成方案横评：雪花、号段与 Leaf
slug: distributed-id-generation
date: 2026-07-12
tags: 分布式系统, 架构设计, 后端, 高并发
summary: 自增主键在分库分表下不够用了怎么办？把 UUID、数据库自增、号段模式、雪花算法、美团 Leaf、百度 UidGenerator 放在一起，从趋势递增、时钟回拨、可用性三个维度做横向对比，并给出选型决策树。
---

分库分表之后，第一个撞上的问题往往就是主键。单表自增在多个库上会互相冲突，而如果用 UUID，插入性能又会掉得很难看。这篇把常见的几套方案摊开对比一遍。

## 一、先明确需求

选型之前先回答四个问题，答案基本就唯一了：

| 问题 | 为什么重要 |
| --- | --- |
| ID 需要**趋势递增**吗？ | 影响 InnoDB 聚簇索引的插入性能、以及分页/排序的可读性 |
| ID 会不会**暴露给外部**（URL、接口）？ | 递增 ID 会泄露业务量、可被遍历爬取 |
| 允许**多长**？ | 64 位能塞进 `BIGINT`，128 位只能存字符串 |
| 能接受**多高的可用性要求**？ | 依赖中心服务 vs 完全去中心化 |

## 二、六种方案逐个看

### 1. UUID

```java
String id = UUID.randomUUID().toString(); // 36 字符
```

- ✅ 完全本地生成，无网络开销，无单点
- ❌ 无序，作为 InnoDB 主键会导致**页分裂**，写入性能极差
- ❌ 36 字符占用大，二级索引也跟着膨胀
- ❌ 不可读

**结论**：不要用作主键。可以作为**业务无关的唯一标识**（如 trace id、文件 key）使用。如果非要用，用 UUIDv7（时间有序）会好很多。

### 2. 数据库自增 + 步长

```sql
-- 库 1
auto_increment_increment = 2
auto_increment_offset = 1   -- 生成 1, 3, 5, 7...
-- 库 2
auto_increment_increment = 2
auto_increment_offset = 2   -- 生成 2, 4, 6, 8...
```

- ✅ 实现最简单，趋势递增
- ❌ 扩容麻烦：加一个库就要改所有库的步长，且要停机或精心规划
- ❌ 强依赖 DB 可用性

**结论**：库数量固定、规模不大的场景可用。超过 3 个分片就别这么玩了。

### 3. 号段模式（Segment）

核心思路：**一次从数据库取一批 ID 缓存在内存，用完再取**。

```sql
CREATE TABLE id_segment (
  biz_tag     VARCHAR(64) NOT NULL,
  max_id      BIGINT      NOT NULL,
  step        INT         NOT NULL,
  update_time TIMESTAMP   NOT NULL,
  PRIMARY KEY (biz_tag)
);

-- 取号段：一次原子地推进 max_id，拿到 [old_max_id+1, new_max_id]
UPDATE id_segment SET max_id = max_id + step WHERE biz_tag = 'order';
SELECT max_id, step FROM id_segment WHERE biz_tag = 'order';
```

再做一个**双 buffer**：当前号段消耗到 10% 时，异步去取下一个号段。这样取号段的那次 DB 抖动不会阻塞业务。

- ✅ 趋势递增，ID 连续可读
- ✅ 数据库压力极小（1 万 QPS 下大约几十秒才查一次库）
- ✅ 步长可动态调整，扩容方便
- ❌ 依赖 DB；号段浪费（重启会丢弃未用完的号段）
- ❌ ID 会**跳变**，不适合做严格连续的流水号

**结论**：**最推荐的通用方案**。美团 Leaf-segment、滴滴 TinyID 都是这个思路。

### 4. 雪花算法（Snowflake）

经典 64 位布局：

```
0 | 41 bit 时间戳(ms) | 10 bit 机器ID | 12 bit 序列号
```

- ✅ 完全本地生成，性能极高（单机每秒 400 万+）
- ✅ 趋势递增，`BIGINT` 可存
- ❌ **强依赖时钟**，时钟回拨会产生重复 ID
- ❌ 机器 ID 分配需要额外机制（ZK / 配置中心 / Redis）
- ❌ 41 位时间戳只够用约 69 年

**时钟回拨**是最需要认真处理的：

```java
public synchronized long nextId() {
    long now = System.currentTimeMillis();
    if (now < lastTimestamp) {
        long offset = lastTimestamp - now;
        if (offset <= 5) {
            // 小回拨：等待追平
            try {
                wait(offset << 1);
                now = System.currentTimeMillis();
                if (now < lastTimestamp) {
                    throw new IllegalStateException("clock moved backwards");
                }
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
                throw new IllegalStateException("interrupted while waiting clock", e);
            }
        } else {
            // 大回拨：直接报错，让上层重试（或切换到备用 workerId）
            throw new IllegalStateException("clock moved backwards by " + offset + "ms");
        }
    }
    // ... 正常生成逻辑
}
```

生产环境还要注意：NTP 同步时**不要用 `-` 大步长跳变**，改用 `slew` 模式平滑校正。

### 5. 美团 Leaf-snowflake

在雪花基础上补了两个短板：

- **workerId 自动分配**：启动时用 ZK 顺序节点拿到唯一 workerId，并写入本地文件缓存，ZK 挂了也能启动。
- **时钟回拨检测**：启动时对比本地时间与 ZK 上记录的上次时间，不一致就告警并拒绝启动。

### 6. 百度 UidGenerator

用 `RingBuffer` 预生成 ID，进一步减少同步开销，吞吐比裸雪花更高。但依赖数据库分配 workerId，且 ID 位宽布局与雪花不兼容（时间戳左移、序列号在低位）。

## 三、横向对比

| 方案 | 趋势递增 | 性能 | 依赖 | 时钟敏感 | 长度 | 适用场景 |
| --- | --- | --- | --- | --- | --- | --- |
| UUID | ❌ | 极高 | 无 | 否 | 128 bit | 非主键的唯一标识 |
| DB 自增+步长 | ✅ | 中 | DB | 否 | 64 bit | 分片少、规模小 |
| 号段模式 | ✅ | 高 | DB | 否 | 64 bit | **通用首选** |
| 雪花算法 | ✅ | 极高 | 机器 ID 分配 | **是** | 64 bit | 超高并发、可容忍少量跳号 |
| Leaf-snowflake | ✅ | 极高 | ZK | 是 | 64 bit | 超高并发 + 运维规范 |
| UidGenerator | ✅ | 极高 | DB | 是 | 64 bit | 极致吞吐 |

## 四、选型决策树

```
需要对外暴露 ID 且不希望被猜出业务量？
├── 是 → 不要用纯自增/雪花的原始值，加一层「ID 混淆」
│        （如 Hashids、或内部 ID ↔ 外部短码 的映射表）
└── 否
    └── 单机 QPS 是否超过 5 万？
        ├── 否 → 号段模式（Leaf-segment / TinyID）
        │        运维成本最低，可读性最好
        └── 是 → 雪花算法
                 ├── 有 ZK/etcd → Leaf-snowflake
                 └── 无 → 自研雪花 + 配置中心分配 workerId
                        + 时钟回拨兜底策略
```

## 五、几个工程细节

**1. 号段模式要预留降级路径**

DB 挂掉时，如果本地还有剩余号段，服务能继续撑一会儿。可以把号段缓存到本地文件，重启后先尝试恢复。

**2. 雪花算法的 workerId 不要用 IP 末位**

IP 会变、会重复（容器环境下），一定要走中心化分配或 K8s StatefulSet 的序号。

**3. 不要用业务时间做 ID 的一部分**

见过用 `yyyyMMdd + 自增` 的，跨零点时自增重置，直接撞 ID。

**4. 考虑 ID 的"可读性"**

运维排查时，能从 ID 看出生成时间（雪花就能）是很爽的体验。如果团队经常需要按 ID 定位时间段，优先选雪花类方案。

**5. 分库分表路由与 ID 生成解耦**

不要因为"分 8 张表"就把 ID 设计成 `用户ID << 4 | 表序号`，后面改分片数会痛不欲生。用独立的 ID 生成器，路由用另外的字段（如 `user_id`）。

## 小结

- 大部分业务，**号段模式**是最优解：够快、够简单、够好运维。
- 超高并发场景再上**雪花**，但一定要把**时钟回拨**和**workerId 分配**这两件事做扎实。
- UUID 不是不能用，是别当主键用。

最后一句：ID 生成器是基础设施，**上线前一定要做单点故障演练**——手动把生成服务停掉，看业务能撑多久。
