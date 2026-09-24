---
title: MySQL 索引失效的 12 个真实场景复盘
slug: mysql-index-not-used-cases
date: 2026-08-20
tags: MySQL, 数据库, 性能优化, 后端
summary: 把线上慢查询日志里反复出现的索引失效场景整理成 12 条：每条给出复现 SQL、为什么失效、以及改写成什么样。文末附一套排查 checklist。
---

索引失效这个话题老生常谈，但真到线上抓慢查询时，往往还是会愣一下。下面这 12 个场景全部来自我自己处理过的慢查询日志，按"最容易踩"排序。

先约定一张演示表和索引：

```sql
CREATE TABLE `t_order` (
  `id`          BIGINT       NOT NULL AUTO_INCREMENT,
  `order_no`    VARCHAR(32)  NOT NULL,
  `user_id`     BIGINT       NOT NULL,
  `status`      TINYINT      NOT NULL,
  `amount`      DECIMAL(12,2) NOT NULL,
  `remark`      VARCHAR(255) DEFAULT NULL,
  `created_at`  DATETIME     NOT NULL,
  PRIMARY KEY (`id`),
  KEY `idx_user_status_time` (`user_id`, `status`, `created_at`),
  KEY `idx_order_no` (`order_no`),
  KEY `idx_created_at` (`created_at`)
) ENGINE=InnoDB;
```

## 1. 联合索引不满足最左前缀

```sql
-- 不走 idx_user_status_time
SELECT * FROM t_order WHERE status = 1 AND created_at > '2026-01-01';
```

联合索引 `(user_id, status, created_at)` 的 B+ 树是按 `user_id` 先排序的。跳过最左列 `user_id`，后面的列在全局是无序的，无法用树来定位。

**改法**：补上 `user_id`，或者为这个查询单独建 `(status, created_at)`。

> 注意：MySQL 8.0 有 **索引跳跃扫描**（Index Skip Scan），在某些条件下跳过最左列也能用上索引，但它的适用条件很苛刻（最左列基数极低），不要把优化押在它身上。

## 2. 在索引列上做运算或函数

```sql
-- 失效：对索引列做了函数运算
SELECT * FROM t_order WHERE DATE(created_at) = '2026-01-01';
-- 失效：对索引列做了运算
SELECT * FROM t_order WHERE id + 1 = 100;
```

索引里存的是原始值，套了函数之后没法用 B+ 树的有序性定位。

**改法**：把运算挪到常量一侧，改写成范围查询。

```sql
SELECT * FROM t_order
WHERE created_at >= '2026-01-01 00:00:00'
  AND created_at <  '2026-01-02 00:00:00';
```

## 3. 隐式类型转换

这是线上最常见、也最隐蔽的一种。`order_no` 是 `VARCHAR`，如果传了数字：

```sql
-- 失效：字符串列 vs 数字常量，MySQL 会把列转成 double 比较
SELECT * FROM t_order WHERE order_no = 20260101001;
```

`EXPLAIN` 的 `Extra` 里往往不会明说，但 `key` 会从 `idx_order_no` 变成空。反过来，**数字列传字符串常量是可以走索引的**（常量被转换，列保持原样）。

**改法**：参数类型与列类型严格对齐。如果是 MyBatis，检查一下 `#{}` 传进来的 Java 类型。

## 4. `LIKE` 以通配符开头

```sql
-- 失效
SELECT * FROM t_order WHERE remark LIKE '%退款%';
-- 有效（前缀匹配）
SELECT * FROM t_order WHERE remark LIKE '退款%';
```

**改法**：
- 前缀匹配需求 → 保持 `LIKE 'xxx%'`。
- 中缀/后缀搜索 → 使用全文索引（`FULLTEXT` + `MATCH ... AGAINST`），或者把搜索交给 ES。
- 数据量不大（几万行以内）时，全表扫也未必是问题，别过早优化。

## 5. `OR` 连接的条件有一侧没索引

```sql
-- 只要 status 上没有索引，整个查询就可能退化成全表扫
SELECT * FROM t_order WHERE user_id = 1001 OR status = 2;
```

**改法**：两侧都建索引，或者拆成两条 SQL 在应用层 `UNION ALL`。

## 6. 使用 `NOT IN` / `!=` / `NOT LIKE`

这类否定条件通常意味着"要扫描大部分数据"，优化器判断走索引再回表不如直接全表扫。

**改法**：如果否定条件的候选集很小，改写成肯定条件：

```sql
-- 原来是 status != 1
SELECT * FROM t_order WHERE status IN (0, 2, 3, 4);
```

## 7. `IS NULL` / `IS NOT NULL` 的坑

单列索引中 `IS NULL` 是可以走索引的。但如果是**联合索引**，而 `NULL` 列不在最左，情况就变了。另外 `IS NOT NULL` 在 `NULL` 值占比很低时通常也会全表扫。

**改法**：数据库设计阶段就给关键列加 `NOT NULL DEFAULT`，从根上避免。

## 8. 范围查询之后的列无法用于排序

```sql
-- 能用索引过滤，但 ORDER BY 用不上索引排序
SELECT * FROM t_order
WHERE user_id = 1001 AND created_at > '2026-01-01'
ORDER BY status;
```

联合索引 `(user_id, status, created_at)` 中，`created_at` 是范围条件，它的**后面**已经没有列了；而 `status` 在 `created_at` 之前，一旦 `created_at` 变成范围扫描，`status` 在结果集内就不再有序。

**改法**：调整索引列顺序。把等值条件列放前面，范围条件列放最后：

```sql
KEY `idx_user_status_time` (`user_id`, `status`, `created_at`)  -- 已是正确顺序
-- 如果 ORDER BY status 是主诉求，则建立 (user_id, created_at) 覆盖，或调整业务分页方式
```

## 9. 回表代价过高，优化器主动放弃索引

```sql
-- 假设 status = 1 的数据占 90%
SELECT * FROM t_order WHERE status = 1 LIMIT 10;
```

优化器会估算：走 `status` 索引拿到 90% 的主键，再逐行回表，成本高于直接全表扫 + `LIMIT` 提前结束。

**改法**：
- 用**覆盖索引**消除回表：把查询需要的列加进索引。

```sql
ALTER TABLE t_order ADD KEY idx_status_cover (status, user_id, amount, created_at);
```

- 或者接受全表扫——当 `status` 区分度真的很低时，强行走索引反而更慢。

## 10. 排序字段与索引顺序不一致

```sql
-- 混合升降序，MySQL 8.0 之前无法用索引排序
SELECT * FROM t_order WHERE user_id = 1001
ORDER BY status ASC, created_at DESC;
```

MySQL 8.0 起支持**降序索引**，可以显式声明：

```sql
KEY `idx_user_status_time_desc` (`user_id`, `status` ASC, `created_at` DESC);
```

8.0 之前只能靠额外排序（`Using filesort`）。

## 11. 统计信息过期导致选错执行计划

现象很典型：**昨天还好好的 SQL，今天突然慢了**，`EXPLAIN` 显示走了另一个索引。

**排查**：

```sql
SHOW INDEX FROM t_order;              -- 看 Cardinality 是否明显偏离实际
ANALYZE TABLE t_order;                -- 重新采样统计信息
```

**改法**：
- 对数据分布剧烈变化的表，定期 `ANALYZE TABLE`。
- 关键 SQL 用 `FORCE INDEX` 兜底（但要记得它会锁死选择权，索引改名/删除后会报错）。
- 上线前用 `EXPLAIN ANALYZE`（8.0.18+）看真实执行耗时，而不是只看估算。

## 12. 索引选择性太差

如果一个索引列的区分度极低（比如性别、状态位、是否删除），走索引的收益本身就很小。

**判断方法**：

```sql
SELECT COUNT(DISTINCT status) / COUNT(*) AS selectivity FROM t_order;
```

一般选择性低于 0.01 的列，单独建索引意义不大。可以考虑：
- 与高选择性列组成联合索引
- 改用位图、分区等其他手段

## 排查 checklist

遇到慢查询，我一般按这个顺序走：

1. `EXPLAIN` 看 `type`、`key`、`rows`、`Extra`（重点看 `Using filesort` / `Using temporary`）
2. `SHOW WARNINGS` 看优化器重写后的 SQL——经常能一眼看出为什么没用索引
3. 确认参数类型与列类型一致（隐式转换第一名）
4. 确认条件是否满足最左前缀、范围条件是否在最右
5. `SHOW INDEX` 看 `Cardinality`，必要时 `ANALYZE TABLE`
6. 对高频 SQL 用 `EXPLAIN ANALYZE` 验证实际耗时
7. 数据量到百万级以上且是分析类查询，考虑加覆盖索引或换存储（ES / ClickHouse）

## 小结

12 条里，真正高频的其实就三类：**最左前缀不满足、索引列上做运算（含隐式类型转换）、回表代价过高**。把这三类记住，剩下的靠 `EXPLAIN` + `SHOW WARNINGS` 基本都能定位。

最后一句提醒：**索引不是越多越好**。每个索引都会让写入变慢、占用空间、增加优化器选错计划的概率。加索引前先问一句——这条 SQL 的 QPS 和耗时，真的到了需要优化的程度吗？
