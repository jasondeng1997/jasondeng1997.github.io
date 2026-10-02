---
title: 配了负载均衡策略却没生效：从四行 case 挖出三处沉睡的 bug
slug: seata-go-loadbalance-dispatch
date: 2026-10-02
tags: Go, 分布式事务, Seata, 负载均衡
summary: load-balance.type 配成 ConsistentHashLoadBalance 或 LeastActiveLoadBalance，行为却和随机一样，没有报错也没有日志。补上分发表里缺的两个 case 只是开始——策略一旦真的可达，三个一直睡着的缺陷会被同时激活。
---

在 apache/incubator-seata-go 修了一个"配置正确但不生效"的 bug。表面上看是分发表漏了两个 `case`，四行就能补完；但真正花时间的是补完之后发生的事：**那两个策略第一次真的可达，于是三处早就写错、却因为死代码而从未被执行的逻辑，同时变成了现网行为。**

这个 PR 最后是 12 个文件、+404/−25，其中新增测试 250 行——四行 `case` 的成本在这里，不在四行里。

## 一、症状：配置被接受了，行为没有变

`pkg/remoting/loadbalance.Select(loadBalanceType, sessions, xid)` 是 Seata-go 的负载均衡分发入口。框架支持五种策略名：`RandomLoadBalance`、`XidLoadBalance`、`RoundRobinLoadBalance`、`ConsistentHashLoadBalance`、`LeastActiveLoadBalance`。配置层全部接受，实现文件也都真实存在（`consistent_hash_loadbalance.go`、`least_active_loadbalance.go` 都不是空壳）。

但 `Select` 的 `switch` 只写了前三个：

```go
switch loadBalanceType {
case randomLoadBalance:
    return RandomLoadBalance(sessions, xid)
case xidLoadBalance:
    return XidLoadBalance(sessions, xid)
case roundRobinLoadBalance:
    return RoundRobinLoadBalance(sessions, xid)
default:
    return RandomLoadBalance(sessions, xid)   // 其余全部静默降级为随机
}
```

所以配了 `ConsistentHashLoadBalance` 或 `LeastActiveLoadBalance` 的实例，会掉进 `default`，**静默地**用随机负载均衡跑。没有 warning、没有 metric、没有异常。

这正是这类 bug 最贵的地方：**它不制造故障，它只是让配置无声地失效。** 一致性哈希的意义是"同一事务稳定落到同一台 TC"，失效之后集群还能跑，只是热点和"以为有亲和性其实没有"的问题不会有人发现。

## 二、策略一旦可达，三个隐藏缺陷同时被激活

死代码是没有代价的，活代码才有。两个 `case` 补上之后，下面三件事立刻从"以后可能会有的问题"变成"现在的运行路径"。

### 1. 哈希 key 是个常量：所有请求钉在同一台 TC

`SendSync` / `SendAsync` 在没有现成连接时，会把**整个** `message.RpcMessage` 传给 `selectSession(msg)` / `selectChannel(msg)`，而 `getXid(msg)` 期望的是一份具体的消息 body。于是：

- getty 侧：`reflect` 在 `RpcMessage` 上找不到 `Xid` 字段，`FieldByName(...).String()` 对无效值返回**字面量字符串** `"<invalid Value>"`；
- gRPC 侧：类型断言全部落空，直接返回空字符串。

两种情况下，一致性哈希拿到的都是一个**所有请求都相同的 key**——把整个集群的请求钉死在同一台 TC 上。修法在调用点和提取函数各一处：

```go
// 调用点：把 body 交出去，而不是整个信封
s = sessionManager.selectSession(msg.Body)
```

```go
// getXid：防御性解包 + 反射每一步都加守卫
if rpcMsg, ok := msg.(message.RpcMessage); ok {
    msg = rpcMsg.Body
}
if msg == nil {
    return ""
}
// ... 常见的几种 body 走类型断言 ...

msgValue := reflect.ValueOf(msg)
if msgValue.Kind() == reflect.Ptr {
    if msgValue.IsNil() {
        return ""
    }
    msgValue = msgValue.Elem()
}
if msgValue.Kind() != reflect.Struct {
    return ""
}
if field := msgValue.FieldByName("Xid"); field.IsValid() && field.Kind() == reflect.String {
    return field.String()
}
if field := msgValue.FieldByName("TransactionName"); field.IsValid() && field.Kind() == reflect.String {
    return field.String()
}
return ""
```

值得单独指出的是最后几行守卫：原来的实现里，**"字段不存在"这条路径不会返回空字符串，而是返回那个字面量占位符**。它长得像数据，实际是把所有请求合并成一个 key。空 key 的表达方式必须是空 key——否则"没有 key"会被误当成"大家共享一个 key"。

顺带对齐 Java 客户端的行为：真的没有事务 key 时（心跳、无 key 请求），`ConsistentHashLoadBalance` 用 `uuid.NewString()` 打散，而不是让空 key 变成"固定节点"。

### 2. 在途计数是普通读：`-race` 直接报 DATA RACE

`LeastActiveLoadBalance` 需要读每个 session 的在途请求数，这些计数由 `rpc.BeginCount` / `rpc.EndCount` 通过 `atomic.AddInt32` 写入，而读取侧是这样的：

```go
func (s *Status) GetActive() int32 { return s.Active }  // 与 atomic.AddInt32 并发
```

策略不可达时，这只是一段"以后可能会出问题"的代码；补上 `case` 之后，它是一条稳定的数据竞争路径。改用 `atomic.LoadInt32`，并补一个能稳定复现的并发测试。

### 3. 哈希环的两个死角

一致性哈希的环由包级 `sync.Once` 构建后缓存。有两个 corner case：

- **首次选择早于任何 session 注册**：环用空快照建成，之后永远为空，每次选择都退化成随机，而且**不会自愈**；
- **key 的哈希值越过最后一个虚拟节点**：原实现 fallback 到随机。也就是说同一个 xid 每次可能落到不同节点——一致性哈希最主要的承诺（同一 key 稳定映射）直接失效。

修法：环为空就从当前快照重建；越界则环绕到 `sortedHashNodes[0]`。环本来就是圆的，第一个节点就是最后一个节点的后继。

## 三、测试怎么写，才真的锁住行为

"派发类"缺陷最容易写出**看起来覆盖了、实际什么都锁不住**的测试。这个 PR 里用了四条手法：

**（1）用可观测副作用区分"真派发"和"随机兜底"。** LeastActive 的测试给 8 个 session 各设置不同的在途数，正确答案唯一；ConsistentHash 的测试则断言包级缓存里的哈希环被填充——随机兜底永远不会去建环。

**（2）比身份，不比结构。** gomock 造出来的 session 结构完全一致，`assert.Equal` 会欣然接受任何一个，测试等于没写。所以断言写成 `got == all[0]`；需要在下一行解引用的地方用 `require` 而不是 `assert`，否则断言失败会变成 panic。

**（3）并发缺陷必须用 `-race` 复现**，不能靠读代码。一个 goroutine 循环选择，多个 goroutine 反复 `BeginCount/EndCount`，修复前稳定报 DATA RACE。

**（4）表驱动守住分发表本身。** 枚举包内声明的五个策略常量，逐个断言"能选到 session、且选到的是自己的候选"，这条测试防的是漂移：以后再加策略却忘了写 `case`，它会红。

**（5）验收标准是"把修复删掉，测试必须失败"。** 环绕、空环、空 key、派发四类逐条回退验证：并发那条报 DATA RACE，其余在断言上失败。做不到这一点的测试，只是在描述现状。

## 四、两个 review 来回

reviewer 在这一轮指出的是两条"你的 `case` 让老问题进入了生产路径"的问题——就是上面第 1、2 条。我在同一轮修完并补了回归。第三条（**快照成员变化时重建环**，而不是只在连接关闭时重建）他判断为非阻塞：要做对得先做成员比较，我另开 issue 作为后续，并把 `Fixes #1073` 写进描述。

还有一个值得记的插曲：同一个 issue 下有另一个 PR 也在做同一件事，维护者需要在两者之间选一个。决定性的差异不在功能，而在**测试强度**——把新增的两个 `case` 删掉之后，对方的分发测试仍然全绿（说明它没有锁住派发行为），而这个 PR 的测试会失败。

所以"删掉修复后测试必须失败"不只是自我要求，它也是评审时最有说服力的一份证据。

## 五、几条可复用的判断

1. **配置项被接受，不等于被实现。** 分发表、注册表、插件表都应该有一条"声明的名字全部可达"的测试。
2. **`default:` 里的静默兜底是最贵的 bug。** 它把"不支持"伪装成"支持"。至少应该 warn，或者干脆 fail fast——让配置错误在启动时就尖叫。
3. **补齐一个分发点，等于点亮一整条沉睡的代码路径。** 紧接着要做的是审它的并发、边界和退化分支。那不是"顺带发现的问题"，是这个 PR 的责任范围。
4. **测试要锁行为，不是锁不崩。** 判断标准很简单：把修复删掉，它会不会红。

PR：[apache/incubator-seata-go#1205](https://github.com/apache/incubator-seata-go/pull/1205)，issue：[#1073](https://github.com/apache/incubator-seata-go/issues/1073)。
