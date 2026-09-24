---
title: 关于
slug: about
---

## 关于我

- **邮箱**：15301580353@163.com
- **坐标**：上海
- **经历**：2020 年毕业于南京某 211，现就职于上海某芯片国企，从事 IoT 相关研发
- **语言**：Java、Go

平时的工作大致是三件事：写业务代码、读中间件源码、把踩过的坑写成文档。这个博客就是第三件事的产物。

## 关于这个站

原来的老站（2022 年）只有两篇手写 HTML 的文章，更新一次要手改 index.html，实在提不起劲。所以把它重做了一遍：

- **内容**：用 Markdown 写作，`content/posts/` 下的每个 `.md` 文件就是一篇文章
- **构建**：`build.py` 是一个零依赖设计的静态生成器，负责渲染 Markdown、生成标签页与归档页、产出 RSS 与搜索索引
- **发布**：提交 Markdown 后由 GitHub Actions 自动构建并发布到 GitHub Pages
- **图片**：统一放在 `assets/images/` 下，随站点一起托管

写作流程因此变得很短——在 `/admin/` 页面里写好 Markdown，点发布，一分钟内就能上线。

## 关于旧文

老站的两篇文章（[分布式事务 XA 模式](/posts/xa-distributed-transaction/)、[dubbo-go 引入 RocketMQ](/posts/dubbo-go-rocketmq-rpc/)）已经迁移过来，原文的图片一并转存到本站，老链接做了 301 跳转，不会失效。

## 聊两句

如果你在分布式事务、RPC 或者 Go 工程实践上有什么想法，欢迎邮件交流。

> 2022/01/30，JasonDeng，于上海。
