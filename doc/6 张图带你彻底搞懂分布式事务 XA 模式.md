![img](https://cdn.nlark.com/yuque/0/2022/jpeg/12364963/1668565951280-aca43b36-0d82-408a-a62b-a8f886f61aee.jpeg)

XA 协议是由 X/Open 组织提出的分布式事务处理规范，主要定义了事务管理器 TM 和局部资源管理器 RM 之间的接口。目前主流的数据库，比如 oracle、DB2 都是支持 XA 协议的。

mysql 从 5.0 版本开始，innoDB 存储引擎已经支持 XA 协议，今天的源码介绍实验环境使用的是 mysql 数据库。

# 两阶段提交

分布式事务的两阶段提交是把整个事务提交分为 prepare 和 commit 两个阶段。以电商系统为例，分布式系统中有订单、账户和库存三个服务，如下图：

![img](https://cdn.nlark.com/yuque/0/2022/jpeg/12364963/1668565950991-1d218a14-0428-4d52-b884-a49cbea1cccc.jpeg)

第一阶段，事务协调者向事务参与者发送 prepare 请求，事务参与者收到请求后，如果可以提交事务，回复 yes，否则回复 no。

第二阶段，如果所有事务参与者都回复了 yes，事务协调者向所有事务参与者发送 commit 请求，否则发送 rollback 请求。

两阶段提交存在三个问题：

- 同步阻塞，本地事务在 prepare 阶段锁定资源，如果有其他事务也要修改 xiaoming 这个账户，就必须等待前面的事务完成。这样就造成了系统性能下降。
- 协调节点单点故障，如果第一个阶段 prepare 成功了，但是第二个阶段协调节点发出 commit 指令之前宕机了，所有服务的数据资源处于锁定状态，事务将无限期地等待。
- 数据不一致，如果第一阶段 prepare 成功了，但是第二阶段协调节点向某个节点发送 commit 命令时失败，就会导致数据不一致。

# 三阶段提交

为了解决两阶段提交的问题，三阶段提交做了改进：

- 在协调节点和事务参与者都引入了超时机制。
- 第一阶段的 prepare 阶段分成了两步，canCommi 和 preCommit。

如下图：

![img](https://cdn.nlark.com/yuque/0/2022/jpeg/12364963/1668565951175-08fd589a-424e-4e30-a1bf-10f7d89d66ef.jpeg)

引入 preCommit 阶段后，协调节点会在 commit 之前再次检查各个事务参与者的状态，保证它们的状态是一致的。但是也存在问题，那就是如果第三阶段发出 rollback 请求，有的节点没有收到，那没有收到的节点会在超时之后进行提交，造成数据不一致。

# XA 事务语法介绍

xa 事务的语法如下：

- 三阶段的第一阶段：开启 xa 事务，这里 xid 为全局事务 id：

XA {START|BEGIN} xid [JOIN|RESUME]

结束 xa 事务：

XA END xid [SUSPEND [FOR MIGRATE]]

- 三阶段的第二阶段，即 prepare：

XA PREPARE xid

- 三阶段的第三阶段，即 commit/rollback：

XA COMMIT xid [ONE PHASE]XA ROLLBACK xid

- 查看处于 PREPARE 阶段的所有事务：

XA RECOVER XA RECOVER [CONVERT XID]

# seata XA 简介

seata 是阿里推出的一款开源分布式事务解决方案，目前有 AT、TCC、SAGA、XA 四种模式。

seata 的 XA 模式是利用分支事务中数据库对 XA 协议的支持来实现的。我们看一下 seata 官网的介绍：[1]

![img](https://cdn.nlark.com/yuque/0/2022/jpeg/12364963/1668565951106-3e335258-21a9-45a6-b7e3-401fcc9d666c.jpeg)

从上面的图可以看到，seata XA 模式的流程跟其他模式一样：

1. TM 开启全局事务
2. RM 向 TC 注册分支事务
3. RM 向 TC 报告分支事务状态
4. TC 向 RM 发送 commit/rollback 请求
5. TM 结束全局事务

这里介绍一下 RM 客户端初始化关联的 UML 类图：[2]

![img](https://cdn.nlark.com/yuque/0/2022/jpeg/12364963/1668565957559-e776223a-1188-4854-a1f6-9d40c71a1d3e.jpeg)

这个图中有一个类是 AbstractNettyRemotingClient，这个类的内部类 ClientHandler 来处理 TC 发来的请求并委托给父类 AbstractNettyRemoting 的 processMessage 方法来处理。processMessage 方法调用 RmBranchCommitProcessor 类的 process 方法。

需要注意的是，**「seata 的 xa 模式对传统的三阶段提交做了优化，改成了两阶段提交」**:

- 第一阶段首执行 XA 开启、执行 sql、XA 结束三个步骤，之后直接执行 XA prepare。
- 第二阶段执行 XA commit/rollback。

mysql 目前是支持 seata xa 模式的两阶段优化的。

**「但是这个优化对 oracle 不支持，因为 oracle 实现的是标准的 xa 协议，即 xa end 后，协调节点向事务参与者统一发送 prepare，最后再发送 commit/rollback。这也导致了 seata 的 xa 模式对 oracle 支持不太好。」**

# seata XA 源码

seata 中的 XA 模式是使用数据源代理来实现的，需要手动配置数据源代理，代码如下：

@Bean@ConfigurationProperties(prefix = "spring.datasource")public DruidDataSource druidDataSource() {    return new DruidDataSource();}@Bean("dataSourceProxy")public DataSource dataSource(DruidDataSource druidDataSource) {    return new DataSourceProxyXA(druidDataSource);}

- 也可以根据普通 DataSource 来创建 XAConnection，但是这种方式有兼容性问题（比如 oracle），所以 seata 使用了开发者自己配置 XADataSource。
- seata 提供的 XA 数据源代理，要求代码框架中必须使用 druid 连接池。

**1. XA 第一阶段**

当 RM 收到 DML 请求后，seata 会使用 ExecuteTemplateXA来执行，执行方法 execute 中有一个地方很关键，就是把 autocommit 属性改为了 false，而 mysql 默认 autocommit 是 true。事务提交之后，还要把 autocommit 改回默认。

下面我们看一下 XA 第一阶段提交的主要代码。

1）开启 XA

上面代码标注[1]处，调用了 ConnectionProxyXA 类的 setAutoCommit 方法，这个方法的源代码中，XA start 主要做了三件事：

- 向 TC 注册分支事务
- 调用数据源的 XA Start

xaResource.start(this.xaBranchXid, XAResource.TMNOFLAGS);

- 把 xaActive 设置为 true

RM 并没有直接使用 TC 返回的 branchId 作为 xa 数据源的 branchId，而是使用全局事务 id(xid) 和 branchId 重新构建了一个。

2）执行 sql

调用 PreparedStatementProxyXA 的 execute 执行 sql。

3）XA end/prepare

public void commit() throws SQLException {    //省略部分源代码    try {        // XA End: Success        xaResource.end(xaBranchXid, XAResource.TMSUCCESS);        // XA Prepare        xaResource.prepare(xaBranchXid);        // Keep the Connection if necessary        keepIfNecessary();    } catch (XAException xe) {        try {            // Branch Report to TC: Failed            DefaultResourceManager.get().branchReport(BranchType.XA, xid, xaBranchXid.getBranchId(),                BranchStatus.PhaseOne_Failed, null);        } catch (TransactionException te) {            //这儿只打印了一个warn级别的日志        }        throw new SQLException(            "Failed to end(TMSUCCESS)/prepare xa branch on " + xid + "-" + xaBranchXid.getBranchId() + " since " + xe                .getMessage(), xe);    } finally {        cleanXABranchContext();    }}

从这个源码我们看到，commit 主要做了三件事：

- 调用数据源的 XA end
- 调用数据源的 XA prepare
- 向 TC 报告分支事务状态

到这里我们就可以看到，seata 把 xa 协议的前两个阶段合成了一个阶段。

**2. XA commit**

这里的调用关系用一个时序图来表示：

![img](https://cdn.nlark.com/yuque/0/2022/jpeg/12364963/1668565957441-15a7906b-1a14-4a3b-ac8a-2f2e625b79d0.jpeg)

看一下 RmBranchCommitProcessor 类的 process 方法，代码如下：

@Overridepublic void process(ChannelHandlerContext ctx, RpcMessage rpcMessage) throws Exception {    String remoteAddress = NetUtil.toStringAddress(ctx.channel().remoteAddress());    Object msg = rpcMessage.getBody();    if (LOGGER.isInfoEnabled()) {        LOGGER.info("rm client handle branch commit process:" + msg);    }    handleBranchCommit(rpcMessage, remoteAddress, (BranchCommitRequest) msg);}

从调用关系时序图可以看出，上面的 handleBranchCommit 方法最终调用了 AbstractRMHandler 的 handle 方法，最后通过 branchCommit 方法调用了 ResourceManagerXA 类的 finishBranch 方法。
ResourceManagerXA 类是 XA 模式的资源管理器，看下面这个类图，也就是 seata 中资源管理器（RM）的 UML 类图：

![img](https://cdn.nlark.com/yuque/0/2022/jpeg/12364963/1668565957453-3446d1a7-84b1-470f-9115-7f7a210b36da.jpeg)

上面的 finishBranch 方法调用了 connectionProxyXA.xaCommit 方法，我们最后看一下 xaCommit 方法：

public void xaCommit(String xid, long branchId, String applicationData) throws XAException {    XAXid xaXid = XAXidBuilder.build(xid, branchId); //因为使用mysql，这里xaResource是MysqlXAConnection    xaResource.commit(xaXid, false);    releaseIfNecessary();}

上面调用了数据源的 commit 方法，提交了 RM 分支事务。

到这里，整个 RM 分支事务就结束了。Rollback 的代码逻辑跟 commit 类似。

最后要说明的是，上面的 xaResource，是 mysql-connector-java.jar 包中的 MysqlXAConnection 类实例，它封装了 mysql 提供的 XA 协议接口。

# 总结

seata 中 XA 模式的实现是使用数据源代理完成的，底层使用了数据库对 XA 协议的原生支持。

mysql 的 java 驱动库中，MysqlXAConnection 类封装类 XA 协议的底层接口供外部调用。

跟 TCC 和 SAGA 模式需要在业务代码中实现 prepare/commit/rollback 逻辑相比，XA 模式对业务代码无侵入。