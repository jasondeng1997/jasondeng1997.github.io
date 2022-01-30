# dubbo-go引入RocketMQ作为RPC设计文档1月22



## 设计

实现基于RocketMQ的RPC能力需要实现注册中心模块和protocol模块

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
| rocketmq | nameservice | rocketmq的注册中心与管理中心           |
| dubbo    | 元数据      | 方法参数类型信息方法请求与响应配置信息 |

### 架构流程

![img](https://cdn.nlark.com/yuque/0/2022/png/1509048/1642912259091-076e0fe0-fd12-44e2-95bb-8ab205e27e4a.png)

#### RPC流程

第一步： client发送请求数据到broker

第二步：service从broker拉去请求数据

第三步：当业务处理完成 service把响应数据发送到broker

第四步：client从broker拉去响应数据

#### 注册流程

第一步：broker向nameservice注册broker，topic，queue三类信息

第二步：client从nameservice拉去路由信息



PS: 

1. 元数据与配置中心可以不做任何改变
2. 可以把元数据注册到nameservice中





### 注册中心设计

\1. mock一个注册中心，把路由功能直接交给rocketmq

\2. 以nameserver为注册中心

\3. 以topic作为注册中心

### protocol设计

1. protocol模块制定一套标准用于支持各种注册中心
2. 基于dubbo-go的protocol标准开发。

### 总结

1. 可以实现多套注册中心
2. 注册中心与protocol的开发可以并行开发



### 预计开发时间

| 功能                   | 预计开发时间     | 负责人 |
| ---------------------- | ---------------- | ------ |
| protocol               | 1月22日到1月30日 |        |
| 以nameserver为注册中心 | 2月10日          |        |
| 以topic作为注册中心    |                  |        |





## 实现细节问题

### Topic设定

1. 一个topic对应一个方法
2. 一个topic对应一个类



#### 对比

|              | 方法     | 类     | 说明                                                         |
| ------------ | -------- | ------ | ------------------------------------------------------------ |
| Topic量      | 大       | 中     | 方法与类对比大概是10:1。可以使用tag用于区别方法实现标签路由，只能基于tag。标签路由与使用tag区别方法实现冲突了 |
| 实现难度     | 大且麻烦 | 中     | 方法：需要对config进行扩展类：只需要对invoker进行维护。invoker对应一个方法 |
| 方法级别隔离 | 可以     | 不可以 | 可以基于tag进行区别                                          |







### 问题



#### RocketMQ-client配置信息

client的对象创建有一些关键的信息需要配置



#### 传输数据

client：直接把invoker中相关数据直接当做message的body传递，不做任何加工

server： 直接解析body

#### 元数据的传递

元数据可以放到message中的properties字段里面

#### queue问题

是否允许多个write queue。如果允许多个write queue，需要多点进行维护

1. instance的适配
2. 负载均衡每个算法

1. Directory.cacheInvoker的instance与invoker的处理



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

1. dubbo的条件路由支持非常困难

1. 1. 如果需要支持

1. 1. 1. 需要对broker与queue进行标签
      2. server端需要动态感知标签与动态监听

1. 1. 不建议第一期就支持dubbo的条件路由

1. tracing的支持

1. 1. 开启RocketMQ的tracing
   2. dubbo与RocketMQ的兼容