---
title: Go 服务里的并发控制：从 context 到 errgroup 的工程实践
slug: go-concurrency-context-errgroup
date: 2026-09-18
tags: Go, 并发编程, 工程实践, 后端
summary: 把 context 当成"取消信号广播器"而不是参数袋子，用 errgroup 管住一批 goroutine 的生命周期，再配合 semaphore 限流——一套可以直接抄进项目的并发控制骨架。
---

写 Go 服务最容易被低估的一件事，是**给 goroutine 收尾**。起协程谁都会，难的是在请求被取消、某个下游超时、进程收到 SIGTERM 的时候，让这一批协程干净地退出去。这篇文章把我自己项目里反复用的那套骨架整理出来。

## 一、先把 context 的角色摆正

很多代码里 `context.Context` 沦为了"参数袋子"——`ctx` 里塞 DB 连接、塞用户信息、塞 trace id。这会让函数签名失去意义，也会让取消语义变得模糊。

我更推荐的划分：

| 内容 | 放哪 | 原因 |
| --- | --- | --- |
| 取消信号 / deadline | `context` | 它的本职 |
| trace id / request id | `context` | 需要跨层透传，且是请求级生命周期 |
| 用户身份 | `context` | 同上 |
| DB 连接池、配置对象 | 结构体字段 | 生命周期比请求长 |
| 可选参数 | options 结构体 | 与生命周期无关 |

关键判据是**生命周期**：跟着单次请求生、跟着单次请求死的东西，才属于 context。

一个常见的反面写法：

```go
// 反例：在库里用 context.Background() 切断了取消链
func (r *Repo) GetUser(id int64) (*User, error) {
    ctx := context.Background()
    return r.db.QueryContext(ctx, "select * from user where id = ?", id)
}
```

上游已经超时了，这个查询还在傻跑。正确做法是把 `ctx` 一路透传：

```go
func (r *Repo) GetUser(ctx context.Context, id int64) (*User, error) {
    return r.db.QueryContext(ctx, "select * from user where id = ?", id)
}
```

> 一个经验法则：**凡是可能阻塞的调用，第一个参数都应该是 `ctx`**，包括 HTTP、DB、RPC、channel 收发、`time.Sleep`（用 `select` + `time.After` 替代）。

## 二、context 的三条纪律

1. **谁创建，谁 cancel。** `context.WithCancel` / `WithTimeout` 返回的 `cancel` 必须被调用，否则会泄漏 timer。稳妥写法是紧跟着 `defer cancel()`：

```go
ctx, cancel := context.WithTimeout(parent, 3*time.Second)
defer cancel() // 即使提前 return 也不会泄漏
```

2. **不把 context 存进结构体。** 唯一的例外是 struct 本身就是"一次请求的上下文载体"，而且不会跨请求复用。

3. **不用 `nil` context。** 不确定就用 `context.TODO()`，它的存在就是为了让代码可编译、又可被搜索出来。

## 三、用 errgroup 管住一批 goroutine

`errgroup` 解决的是"并发发起 N 个任务，任一失败就整体取消，并且要能等到所有任务收尾"这个需求。它是 `sync.WaitGroup` 的加强版，多了两件事：**错误传播**和**取消传播**。

```go
func (s *Service) Detail(ctx context.Context, uid int64) (*Detail, error) {
    g, ctx := errgroup.WithContext(ctx)

    var user *User
    var orders []Order
    var coupons []Coupon

    g.Go(func() error {
        var err error
        user, err = s.userRepo.Get(ctx, uid)
        return err
    })
    g.Go(func() error {
        var err error
        orders, err = s.orderRepo.ListByUser(ctx, uid)
        return err
    })
    g.Go(func() error {
        var err error
        coupons, err = s.couponRepo.ListValid(ctx, uid)
        return err
    })

    if err := g.Wait(); err != nil {
        return nil, err
    }
    return &Detail{User: user, Orders: orders, Coupons: coupons}, nil
}
```

这里有三个细节值得强调：

- `g, ctx := errgroup.WithContext(ctx)` 返回的 `ctx` 会在**任一** goroutine 返回错误时被取消。所以另外两个 goroutine 里的 `ctx` 会立刻感知到，能提前退出。
- `g.Wait()` 会等**所有** goroutine 返回，而不是第一个错误就返回。所以你不必担心变量被并发读写的悬垂问题——但也正因为如此，每个 goroutine 的返回值要能安全丢弃。
- 这里的 `user`/`orders`/`coupons` 是不同变量，各写各的，没有 data race。**如果是往同一个 slice 里 append，就一定需要加锁或预分配按索引写。**

### 加上并发上限

无脑并发在依赖出问题时会把下游打垮，也会把自己的连接池耗光。`errgroup` 配 `semaphore` 是最省事的限流方式：

```go
import "golang.org/x/sync/semaphore"

func (s *Service) BatchGet(ctx context.Context, ids []int64) ([]*Item, error) {
    g, ctx := errgroup.WithContext(ctx)
    sem := semaphore.NewWeighted(8) // 最多 8 个并发
    result := make([]*Item, len(ids))

    for i, id := range ids {
        if err := sem.Acquire(ctx, 1); err != nil {
            return nil, err // ctx 已取消，直接退出
        }
        i, id := i, id
        g.Go(func() error {
            defer sem.Release(1)
            item, err := s.repo.Get(ctx, id)
            if err != nil {
                return err
            }
            result[i] = item // 按索引写，天然无竞争
            return nil
        })
    }

    if err := g.Wait(); err != nil {
        return nil, err
    }
    return result, nil
}
```

`i, id := i, id` 这一行在 Go 1.22 之前是必须的；Go 1.22 起循环变量改为每轮独立，可以省掉，但显式写出来也读得懂。

## 四、优雅退出：让信号一路传到最底层

服务收到 SIGTERM 之后要做的事，本质上是**先停止接受新请求，再给在途请求留时间，最后释放资源**。

```go
func main() {
    ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
    defer stop()

    srv := &http.Server{Addr: ":8080", Handler: router()}

    go func() {
        if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
            log.Fatalf("listen: %v", err)
        }
    }()
    log.Println("server started on :8080")

    <-ctx.Done() // 收到信号
    stop()       // 恢复默认信号行为，再按一次 Ctrl+C 直接强杀

    log.Println("shutting down...")
    shutdownCtx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
    defer cancel()

    if err := srv.Shutdown(shutdownCtx); err != nil {
        log.Printf("graceful shutdown failed: %v", err)
    }
    log.Println("server exited")
}
```

几点实践经验：

- `signal.NotifyContext` 比手写 `signal.Notify` + channel 干净得多，Go 1.16+ 可用。
- 关闭超时不要设太长。K8s 的 `terminationGracePeriodSeconds` 默认 30s，本地 grace 设 15s 左右比较合适；如果超过，Pod 会被 `SIGKILL`，设置再长也没意义。
- 注意 `srv.Shutdown` **不会**等待 hijack 连接（比如 WebSocket），需要自己维护连接列表。
- 后台常驻的 goroutine（消费 MQ、定时任务）也要接同一个 `ctx`，否则它们会在服务"退出"后继续跑。

## 五、一组容易踩的坑

| 现象 | 原因 | 修法 |
| --- | --- | --- |
| goroutine 数持续上涨 | 上游 `context.Background()` 切断了取消链 | 全链路透传 ctx |
| CPU 空转 | `for { select {} }` 里缺 default 导致忙等 | 加 `runtime.Gosched()` 或改成长阻塞 select |
| 定时器泄漏 | `WithTimeout` 的 cancel 没调 | `defer cancel()` |
| 超时不起作用 | 客户端设了超时但 DB 层用 Background | 检查每一层是否都传了 ctx |
| 偶发 data race | 多 goroutine 写同一个 map/slice | 按索引写、加锁，或改 channel 收口 |

## 小结

- `context` 是**取消信号广播器**，判据是生命周期而非"能不能少传个参数"。
- `errgroup` + `semaphore` 是绝大多数"并发取数"场景的标准答案：错误能传播、取消能传播、并发有上限。
- 优雅退出要做三件事：停接新请求 → 等在途请求（有超时）→ 释放资源，并且信号要传到每一个常驻 goroutine。

这套骨架我在几个服务里用了两三年，基本没有再因为并发收尾出过线上问题。如果你的项目还在裸用 `sync.WaitGroup`，值得花半小时换过来。
