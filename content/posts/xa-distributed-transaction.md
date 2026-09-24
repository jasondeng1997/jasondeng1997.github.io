---
title: 6 张图带你彻底搞懂分布式事务 XA 模式
slug: xa-distributed-transaction
date: 2022-11-16
tags: 分布式事务, Seata, MySQL, 微服务
summary: 从 XA 协议到两阶段/三阶段提交的取舍，再顺着 Seata 的数据源代理一路读到底层 XA end/prepare 的源码，把 XA 模式为什么对业务无侵入讲清楚。
---

XA 协议是由 X/Open 组织提出的分布式事务处理规范，主要定义了事务管理器 TM 和局部资源管理器 RM 之间的接口。目前主流的数据库，比如 Oracle、DB2 都是支持 XA 协议的。

MySQL 从 5.0 版本开始，InnoDB 存储引擎已经支持 XA 协议，今天这篇源码分析使用的实验环境就是 MySQL。

![分布式事务中 TM 与 RM 的整体角色划分](/assets/images/posts/xa-distributed-transaction/01.jpeg)

## 两阶段提交

分布式事务的两阶段提交是把整个事务提交分为 prepare 和 commit 两个阶段。以电商系统为例，分布式系统中有订单、账户和库存三个服务，如下图：

![订单、账户、库存三个服务参与同一个全局事务](/assets/images/posts/xa-distributed-transaction/02.jpeg)

- 第一阶段，事务协调者向事务参与者发送 prepare 请求，事务参与者收到请求后，如果可以提交事务，回复 yes，否则回复 no。
- 第二阶段，如果所有事务参与者都回复了 yes，事务协调者向所有事务参与者发送 commit 请求，否则发送 rollback 请求。

两阶段提交存在三个问题：

- **同步阻塞**：本地事务在 prepare 阶段锁定资源，如果有其他事务也要修改 xiaoming 这个账户，就必须等待前面的事务完成，这样就造成了系统性能下降。
- **协调节点单点故障**：如果第一阶段 prepare 成功了，但第二阶段协调节点发出 commit 指令之前宕机了，所有服务的数据资源处于锁定状态，事务将无限期地等待。
- **数据不一致**：如果第一阶段 prepare 成功了，但第二阶段协调节点向某个节点发送 commit 命令时失败，就会导致数据不一致。

## 三阶段提交

为了解决两阶段提交的问题，三阶段提交做了改进：

- 在协调节点和事务参与者都引入了超时机制。
- 第一阶段的 prepare 阶段分成了两步：canCommit 和 preCommit。

如下图：

![三阶段提交把 prepare 拆成了 canCommit 与 preCommit](/assets/images/posts/xa-distributed-transaction/03.jpeg)

引入 preCommit 阶段后，协调节点会在 commit 之前再次检查各个事务参与者的状态，保证它们的状态是一致的。但是也存在问题：如果第三阶段发出 rollback 请求，有的节点没有收到，那没有收到的节点会在超时之后进行提交，造成数据不一致。

## XA 事务语法介绍

XA 事务的语法如下，三阶段的第一阶段是开启 XA 事务，这里 xid 为全局事务 id：

```sql
XA {START|BEGIN} xid [JOIN|RESUME]
```

结束 XA 事务：

```sql
XA END xid [SUSPEND [FOR MIGRATE]]
```

三阶段的第二阶段，即 prepare：

```sql
XA PREPARE xid
```

三阶段的第三阶段，即 commit / rollback：

```sql
XA COMMIT xid [ONE PHASE]
XA ROLLBACK xid
```

查看处于 PREPARE 阶段的所有事务：

```sql
XA RECOVER
XA RECOVER [CONVERT XID]
```

## Seata XA 简介

Seata 是阿里推出的一款开源分布式事务解决方案，目前有 AT、TCC、SAGA、XA 四种模式。

Seata 的 XA 模式是利用分支事务中数据库对 XA 协议的支持来实现的。看一下官网的介绍：

![Seata XA 模式的整体流程](/assets/images/posts/xa-distributed-transaction/04.jpeg)

从上面的图可以看到，Seata XA 模式的流程跟其他模式一样：

1. TM 开启全局事务
2. RM 向 TC 注册分支事务
3. RM 向 TC 报告分支事务状态
4. TC 向 RM 发送 commit / rollback 请求
5. TM 结束全局事务

这里再介绍一下 RM 客户端初始化关联的 UML 类图：

![RM 客户端初始化关联的核心类](/assets/images/posts/xa-distributed-transaction/05.jpeg)

这个图中有一个类是 `AbstractNettyRemotingClient`，它的内部类 `ClientHandler` 负责处理 TC 发来的请求，并委托给父类 `AbstractNettyRemoting` 的 `processMessage` 方法。`processMessage` 方法最终调用 `RmBranchCommitProcessor` 类的 `process` 方法。

需要注意的是，**Seata 的 XA 模式对传统的三阶段提交做了优化，改成了两阶段提交**：

- 第一阶段执行 XA 开启、执行 SQL、XA 结束三个步骤，之后直接执行 XA prepare。
- 第二阶段执行 XA commit / rollback。

MySQL 目前是支持 Seata XA 模式的两阶段优化的。

**但是这个优化对 Oracle 不支持**，因为 Oracle 实现的是标准的 XA 协议，即 XA end 后，协调节点向事务参与者统一发送 prepare，最后再发送 commit / rollback。这也导致了 Seata 的 XA 模式对 Oracle 支持不太好。

## Seata XA 源码

Seata 中的 XA 模式是使用数据源代理来实现的，需要手动配置数据源代理，代码如下：

```java
@Bean
@ConfigurationProperties(prefix = "spring.datasource")
public DruidDataSource druidDataSource() {
    return new DruidDataSource();
}

@Bean("dataSourceProxy")
public DataSource dataSource(DruidDataSource druidDataSource) {
    return new DataSourceProxyXA(druidDataSource);
}
```

- 也可以根据普通 `DataSource` 来创建 `XAConnection`，但是这种方式有兼容性问题（比如 Oracle），所以 Seata 使用了开发者自己配置 `XADataSource`。
- Seata 提供的 XA 数据源代理，要求代码框架中必须使用 Druid 连接池。

### 1. XA 第一阶段

当 RM 收到 DML 请求后，Seata 会使用 `ExecuteTemplateXA` 来执行，执行方法 `execute` 中有一个地方很关键，就是把 `autocommit` 属性改为了 `false`，而 MySQL 默认 `autocommit` 是 `true`。事务提交之后，还要把 `autocommit` 改回默认。

下面看一下 XA 第一阶段提交的主要代码。

**1）开启 XA**

`ConnectionProxyXA` 类的 `setAutoCommit` 方法中，XA start 主要做了三件事：

- 向 TC 注册分支事务
- 调用数据源的 XA Start

```java
xaResource.start(this.xaBranchXid, XAResource.TMNOFLAGS);
```

- 把 `xaActive` 设置为 `true`

RM 并没有直接使用 TC 返回的 `branchId` 作为 XA 数据源的 `branchId`，而是使用全局事务 id（xid）和 `branchId` 重新构建了一个。

**2）执行 SQL**

调用 `PreparedStatementProxyXA` 的 `execute` 执行 SQL。

**3）XA end / prepare**

```java
public void commit() throws SQLException {
    // 省略部分源代码
    try {
        // XA End: Success
        xaResource.end(xaBranchXid, XAResource.TMSUCCESS);
        // XA Prepare
        xaResource.prepare(xaBranchXid);
        // Keep the Connection if necessary
        keepIfNecessary();
    } catch (XAException xe) {
        try {
            // Branch Report to TC: Failed
            DefaultResourceManager.get().branchReport(BranchType.XA, xid, xaBranchXid.getBranchId(),
                BranchStatus.PhaseOne_Failed, null);
        } catch (TransactionException te) {
            // 这儿只打印了一个 warn 级别的日志
        }
        throw new SQLException(
            "Failed to end(TMSUCCESS)/prepare xa branch on " + xid + "-" + xaBranchXid.getBranchId() + " since " + xe
                .getMessage(), xe);
    } finally {
        cleanXABranchContext();
    }
}
```

从这个源码我们看到，commit 主要做了三件事：

- 调用数据源的 XA end
- 调用数据源的 XA prepare
- 向 TC 报告分支事务状态

到这里就可以看到，Seata 把 XA 协议的前两个阶段合成了一个阶段。

### 2. XA commit

这里的调用关系用一个时序图来表示：

![XA commit 阶段的调用时序](/assets/images/posts/xa-distributed-transaction/06.jpeg)

看一下 `RmBranchCommitProcessor` 类的 `process` 方法，代码如下：

```java
@Override
public void process(ChannelHandlerContext ctx, RpcMessage rpcMessage) throws Exception {
    String remoteAddress = NetUtil.toStringAddress(ctx.channel().remoteAddress());
    Object msg = rpcMessage.getBody();
    if (LOGGER.isInfoEnabled()) {
        LOGGER.info("rm client handle branch commit process:" + msg);
    }
    handleBranchCommit(rpcMessage, remoteAddress, (BranchCommitRequest) msg);
}
```

从调用关系时序图可以看出，上面的 `handleBranchCommit` 方法最终调用了 `AbstractRMHandler` 的 `handle` 方法，最后通过 `branchCommit` 方法调用了 `ResourceManagerXA` 类的 `finishBranch` 方法。

`ResourceManagerXA` 类是 XA 模式的资源管理器，看下面这个类图，也就是 Seata 中资源管理器（RM）的 UML 类图：

![Seata 中资源管理器的 UML 类图](/assets/images/posts/xa-distributed-transaction/07.jpeg)

上面的 `finishBranch` 方法调用了 `connectionProxyXA.xaCommit` 方法，最后看一下 `xaCommit` 方法：

```java
public void xaCommit(String xid, long branchId, String applicationData) throws XAException {
    XAXid xaXid = XAXidBuilder.build(xid, branchId);
    // 因为使用 MySQL，这里 xaResource 是 MysqlXAConnection
    xaResource.commit(xaXid, false);
    releaseIfNecessary();
}
```

上面调用了数据源的 commit 方法，提交了 RM 分支事务。到这里，整个 RM 分支事务就结束了。Rollback 的代码逻辑跟 commit 类似。

最后要说明的是，上面的 `xaResource` 是 `mysql-connector-java.jar` 包中的 `MysqlXAConnection` 类实例，它封装了 MySQL 提供的 XA 协议接口。

## 总结

- Seata 中 XA 模式的实现是使用数据源代理完成的，底层使用了数据库对 XA 协议的原生支持。
- MySQL 的 Java 驱动库中，`MysqlXAConnection` 类封装了 XA 协议的底层接口供外部调用。
- 跟 TCC 和 SAGA 模式需要在业务代码中实现 prepare / commit / rollback 逻辑相比，**XA 模式对业务代码无侵入**，这是它最大的优势；代价是依赖数据库本身的 XA 实现，且持有锁的时间更长。
