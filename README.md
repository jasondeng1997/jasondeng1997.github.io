# JasonDeng 的技术随笔

个人技术博客，托管在 GitHub Pages：**https://jasondeng1997.github.io**

用 Markdown 写作，自己写的静态生成器构建，零服务器、零费用。

## 目录结构

```
├── content/
│   ├── site.json          # 站点配置（标题、作者、导航、管理密码哈希）
│   ├── posts/*.md         # 文章（一篇文章 = 一个 .md 文件）
│   └── pages/about.md     # 独立页面
├── theme/
│   ├── templates/         # Jinja2 模板
│   └── static/            # 样式 / 脚本 / 图标
├── assets/images/         # 图片资源（随站点托管，可当图床用）
├── build.py               # 静态站点生成器
├── .github/workflows/     # 推送后自动构建并回写产物
└── （根目录其余 HTML/JSON 为构建产物，勿手改）
```

## 怎么写文章

**方式一：网页后台（推荐）**

1. 打开 https://jasondeng1997.github.io/admin/
2. 输入管理密码（默认 `JasonDeng@2026`，改法见下）
3. 点右上角「设置」，填入 GitHub Personal Access Token
   - 生成地址：https://github.com/settings/personal-access-tokens/new
   - 权限：只勾选本仓库，**Contents: Read and write**
   - Token 只存在本机浏览器，不会上传到任何第三方
4. 写 Markdown → 点「发布」→ 约 1 分钟后自动上线

后台支持：实时预览、图片拖拽上传（存到 `assets/images/uploads/`）、本地草稿自动保存、编辑/删除已有文章、下载 .md。

**方式二：本地提交**

```bash
git clone https://github.com/jasondeng1997/jasondeng1997.github.io
cd jasondeng1997.github.io
# 新建 content/posts/你的文章.md
python3 -m pip install -r requirements.txt
python3 build.py          # 本地预览产物在仓库根目录，可起个 http 服务看效果
git add content/posts/你的文章.md && git commit -m "post: 文章标题" && git push
```

## 文章格式

```markdown
---
title: 文章标题
slug: english-slug            # URL 路径，建议英文短横线
date: 2026-09-24
tags: Go, 并发编程, 工程实践   # 逗号分隔，任意个
summary: 一句话摘要，会显示在列表页
---

正文 Markdown，支持表格、代码高亮、引用、任务列表。
图片放 assets/images/ 下，用 /assets/images/xxx.png 引用。
```

## 本地预览

```bash
python3 build.py --out ./dist
cd dist && python3 -m http.server 8000
# 打开 http://localhost:8000
```

## 修改管理密码

```bash
python3 -c "import hashlib;print(hashlib.sha256('新密码'.encode()).hexdigest())"
# 把输出替换 content/site.json 里 admin.passwordHash，然后提交推送
```

> 注意：这是纯静态站点，后台密码只是前端门禁，真正的权限控制在 GitHub Token 上。
> 建议使用**细粒度 Token**、只授权本仓库、设置过期时间。

## 常见问题

- **改了 Markdown 网站没更新？** 看 Actions 页面的「Build Blog」是否跑完，构建产物会自动回写到仓库。
- **图片显示不出来？** 确认路径以 `/assets/images/` 开头，且文件确实提交到了仓库。
- **老链接失效？** `/html/rmq_dubbogo.html`、`/html/about.html` 已做跳转；其他老地址在 `build.py` 的 `LEGACY_REDIRECTS` 里加。
- **想加新标签？** 直接在 front matter 里写即可；中文标签的 URL 别名可在 `build.py` 的 `TAG_SLUG_MAP` 里映射。
