---
title: dubbo-go 引入 RocketMQ 作为 RPC 的设计文档
slug: dubbo-go-rocketmq-rpc
date: 2022-01-22
tags: dubbo-go, RocketMQ, RPC, 微服务, Go
summary: 把 RocketMQ 当作 RPC 通道来接进 dubbo-go：注册中心与 protocol 两个模块怎么拆、topic 粒度选方法还是选类、元数据往哪塞，以及条件路由为什么建议第一期不做。
---

## 设计

实现基于 RocketMQ 的 RPC 能力，需要实现注册中心模块和 protocol 模块。

### 术语

#### 术语统一

| dubbo-go | RocketMQ |
| -------- | -------- |
| client   | producer |
| service  | consumer |

#### 术语解释

| 组件     | 术语        | 解释                                   |
| -------- | ----------- | -------------------------------------- |
| rocketmq | broker      | 数据存储组件                           |
| rocketmq | nameservice | RocketMQ 的注册中心与管理中心          |
| dubbo    | 元数据      | 方法参数类型信息、方法请求与响应配置信息 |

### 架构流程

![基于 RocketMQ 的 RPC 整体架构](/assets/images/posts/dubbo-go-rocketmq/01.png)

#### RPC 流程

1. client 发送请求数据到 broker
2. service 从 broker 拉取请求数据
3. 当业务处理完成，service 把响应数据发送到 broker
4. client 从 broker 拉取响应数据

#### 注册流程

1. broker 向 nameservice 注册 broker、topic、queue 三类信息
2. client 从 nameservice 拉取路由信息

PS：

1. 元数据与配置中心可以不做任何改变
2. 也可以把元数据注册到 nameservice 中

### 注册中心设计

1. mock 一个注册中心，把路由功能直接交给 RocketMQ
2. 以 nameserver 为注册中心
3. 以 topic 作为注册中心

### protocol 设计

1. protocol 模块制定一套标准用于支持各种注册中心
2. 基于 dubbo-go 的 protocol 标准开发

### 总结

1. 可以实现多套注册中心
2. 注册中心与 protocol 的开发可以并行

### 预计开发时间

| 功能                   | 预计开发时间     | 负责人 |
| ---------------------- | ---------------- | ------ |
| protocol               | 1 月 22 日到 1 月 30 日 |        |
| 以 nameserver 为注册中心 | 2 月 10 日       |        |
| 以 topic 作为注册中心    |                  |        |

## 实现细节问题

### Topic 设定

1. 一个 topic 对应一个方法
2. 一个 topic 对应一个类

#### 对比

|              | 方法     | 类     | 说明                                                         |
| ------------ | -------- | ------ | ------------------------------------------------------------ |
| Topic 量     | 大       | 中     | 方法与类对比大概是 10:1。可以使用 tag 用于区别方法实现标签路由，只能基于 tag。标签路由与使用 tag 区别方法实现冲突了 |
| 实现难度     | 大且麻烦 | 中     | 方法：需要对 config 进行扩展；类：只需要对 invoker 进行维护。invoker 对应一个方法 |
| 方法级别隔离 | 可以     | 不可以 | 可以基于 tag 进行区别                                        |

### 问题

#### RocketMQ-client 配置信息

client 的对象创建有一些关键的信息需要配置。

#### 传输数据

- client：直接把 invoker 中相关数据直接当做 message 的 body 传递，不做任何加工
- server：直接解析 body

#### 元数据的传递

元数据可以放到 message 中的 `properties` 字段里面。

#### queue 问题

是否允许多个 write queue？如果允许多个 write queue，需要多点进行维护：

1. instance 的适配
2. 负载均衡每个算法
3. `Directory.cacheInvoker` 的 instance 与 invoker 的处理

```go
type Instance struct {
	Valid       bool              `json:"valid"`
	Marked      bool              `json:"marked"`
	InstanceId  string            `json:"instanceId"`
	Port        uint64            `json:"port"`
	Ip          string            `json:"ip"`
	Weight      float64           `json:"weight"`
	Metadata    map[string]string `json:"metadata"`
	ClusterName string            `json:"clusterName"`
	ServiceName string            `json:"serviceName"`
	Enable      bool              `json:"enabled"`
	Healthy     bool              `json:"healthy"`
	Ephemeral   bool              `json:"ephemeral"`
}
```

### 基本功能情况

1. dubbo 的条件路由支持非常困难
   - 如果需要支持：
     1. 需要对 broker 与 queue 进行标签
     2. server 端需要动态感知标签与动态监听
   - **不建议第一期就支持 dubbo 的条件路由**
2. tracing 的支持
   - 开启 RocketMQ 的 tracing
   - dubbo 与 RocketMQ 的兼容
