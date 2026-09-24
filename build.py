#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
JasonDeng 的技术随笔 —— 静态站点生成器

用法：
    python build.py                # 构建到仓库根目录
    python build.py --out ./dist   # 构建到指定目录（本地预览用）
    python build.py --clean-only   # 只清理不构建

设计约定：
    content/site.json   站点配置
    content/posts/*.md  文章（front matter + Markdown）
    content/pages/*.md  独立页面（如「关于」）
    theme/templates/    Jinja2 模板
    theme/static/       样式、脚本、图标
    assets/images/      图片资源（站点直接托管）
"""

import argparse
import datetime as dt
import html
import json
import math
import os
import re
import shutil
import sys
import unicodedata
from pathlib import Path

import markdown
from jinja2 import Environment, FileSystemLoader, select_autoescape
from pygments.formatters import HtmlFormatter

ROOT = Path(__file__).resolve().parent
CONTENT = ROOT / "content"
THEME = ROOT / "theme"

# ---------------------------------------------------------------- 标签 slug

TAG_SLUG_MAP = {
    "分布式事务": "distributed-transaction",
    "分布式系统": "distributed-system",
    "微服务": "microservices",
    "架构设计": "architecture",
    "高并发": "high-concurrency",
    "并发编程": "concurrency",
    "工程实践": "engineering",
    "技术写作": "tech-writing",
    "团队协作": "teamwork",
    "性能优化": "performance",
    "效率": "productivity",
    "后端": "backend",
    "数据库": "database",
    "源码分析": "source-code",
}

# 老站 URL → 新地址（保证外链不失效）
LEGACY_REDIRECTS = {
    "html/rmq_dubbogo.html": "/posts/dubbo-go-rocketmq-rpc/",
    "html/about.html": "/about/",
}


def tag_slug(name: str) -> str:
    if name in TAG_SLUG_MAP:
        return TAG_SLUG_MAP[name]
    ascii_slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    if ascii_slug:
        return ascii_slug
    # 纯中文标签：保留原名，浏览器会自动做 URL 编码
    return name


# ---------------------------------------------------------------- front matter

def parse_front_matter(text: str):
    """解析形如 ---\nkey: value\n---\n 的头部，返回 (meta, body)。"""
    text = text.replace("\r\n", "\n").lstrip("\ufeff")
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    head = text[3:end].strip("\n")
    body = text[end + 4:].lstrip("\n")
    meta = {}
    for line in head.split("\n"):
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key == "tags":
            parts = re.split(r"[,，、|]", value)
            meta[key] = [p.strip() for p in parts if p.strip()]
        else:
            meta[key] = value
    return meta, body


# ---------------------------------------------------------------- 统计

CJK_RE = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]")
LATIN_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_'\-]*")


def count_words(text: str) -> int:
    """中文按字计，英文按词计。"""
    cjk = len(CJK_RE.findall(text))
    latin = len(LATIN_WORD_RE.findall(text))
    return cjk + latin


def reading_minutes(words: int) -> int:
    return max(1, int(math.ceil(words / 420)))


def rfc822(date_str: str) -> str:
    """把 YYYY-MM-DD 转成 RSS 用的 RFC 822 时间（按东八区）。"""
    try:
        d = dt.datetime.strptime(date_str, "%Y-%m-%d")
    except (ValueError, TypeError):
        d = dt.datetime(1970, 1, 1)
    d = d.replace(hour=9, minute=0, second=0)
    return d.strftime("%a, %d %b %Y %H:%M:%S +0800")



def strip_markdown(text: str) -> str:
    text = re.sub(r"```[\s\S]*?```", " ", text)
    text = re.sub(r"`[^`]*`", " ", text)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"^[#>\-*+\d.\s]+", " ", text, flags=re.M)
    text = re.sub(r"[*_~|]", "", text)
    return re.sub(r"\s+", " ", text).strip()


def strip_html(text: str) -> str:
    text = re.sub(r"<(script|style)[\s\S]*?</\1>", " ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return html.unescape(re.sub(r"\s+", " ", text)).strip()


def make_summary(body: str, meta: dict) -> str:
    if meta.get("summary"):
        return meta["summary"]
    plain = strip_markdown(body)
    # 跳过第一段可能是图片的情况
    plain = re.sub(r"^\s*", "", plain)
    if len(plain) > 118:
        return plain[:118].rstrip() + "…"
    return plain


# ---------------------------------------------------------------- Markdown

MD_EXTENSIONS = [
    "extra",
    "tables",
    "fenced_code",
    "codehilite",
    "sane_lists",
    "attr_list",
    "admonition",
    "md_in_html",
    "toc",
]

MD_EXT_CONFIG = {
    "codehilite": {"css_class": "codehilite", "guess_lang": False, "linenums": False},
    "toc": {"toc_depth": "2-3", "anchorlink": False, "permalink": False},
}


def render_markdown(body: str):
    md = markdown.Markdown(extensions=MD_EXTENSIONS, extension_configs=MD_EXT_CONFIG)
    html_out = md.convert(body)
    toc_html = render_toc(getattr(md, "toc_tokens", []))
    return html_out, toc_html


def render_toc(tokens) -> str:
    if not tokens:
        return ""
    parts = ["<ul>"]
    for t in tokens:
        name = html.escape(re.sub(r"<[^>]+>", "", t.get("name", "")))
        parts.append(f'<li><a href="#{t["id"]}">{name}</a>')
        if t.get("children"):
            parts.append(render_toc(t["children"]))
        parts.append("</li>")
    parts.append("</ul>")
    return "".join(parts)


# ---------------------------------------------------------------- 清理

GENERATED_DIRS = ["posts", "tags", "archive", "about", "admin", "html", "assets/static"]
GENERATED_FILES = [
    "index.html", "404.html", "feed.xml", "sitemap.xml",
    "search-index.json", "posts.json", ".nojekyll", "robots.txt",
]


def clean(out: Path):
    for d in GENERATED_DIRS:
        p = out / d
        if p.exists():
            shutil.rmtree(p)
    for f in GENERATED_FILES:
        p = out / f
        if p.exists():
            p.unlink()


# ---------------------------------------------------------------- 主流程

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT), help="输出目录，默认为仓库根目录")
    ap.add_argument("--clean-only", action="store_true")
    args = ap.parse_args()

    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)

    if args.clean_only:
        clean(out)
        print(f"[clean] 已清理生成产物：{out}")
        return

    config = json.loads((CONTENT / "site.json").read_text(encoding="utf-8"))
    site = config
    site["author"] = config["author"]
    site["repo"] = config["repo"]
    site["admin"] = config["admin"]

    env = Environment(
        loader=FileSystemLoader(str(THEME / "templates")),
        autoescape=select_autoescape(["html", "xml"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )

    # ---------- 读取文章 ----------
    posts = []
    for md_file in sorted((CONTENT / "posts").glob("*.md")):
        raw = md_file.read_text(encoding="utf-8")
        meta, body = parse_front_matter(raw)
        slug = meta.get("slug") or md_file.stem
        title = meta.get("title") or md_file.stem
        date = meta.get("date") or "1970-01-01"

        html_out, toc_html = render_markdown(body)
        plain = strip_markdown(body)
        words = count_words(plain)
        tags = meta.get("tags") or []

        posts.append({
            "slug": slug,
            "title": title,
            "date": date,
            "updated": meta.get("updated", ""),
            "tags": tags,
            "summary": make_summary(body, meta),
            "url": f"/posts/{slug}/",
            "html": html_out,
            "toc": toc_html,
            "has_toc": bool(toc_html),
            "plain": plain,
            "search_text": strip_html(html_out),
            "words": words,
            "reading": reading_minutes(words),
            "source": md_file.name,
        })

    posts.sort(key=lambda p: (p["date"], p["slug"]), reverse=True)
    for p in posts:
        p["tag_links"] = [{"name": t, "slug": tag_slug(t)} for t in p["tags"]]
        p["rfc822"] = rfc822(p["date"])

    # 上一篇 / 下一篇（列表已按时间倒序，索引小的更新）
    for i, p in enumerate(posts):
        p["prev_post"] = posts[i - 1] if i > 0 else None      # 更早的一篇
        p["next_post"] = posts[i + 1] if i + 1 < len(posts) else None

    # ---------- 标签聚合 ----------
    tag_index = {}
    for p in posts:
        for t in p["tags"]:
            tag_index.setdefault(t, []).append(p)

    tags = []
    for name, items in tag_index.items():
        tags.append({
            "name": name,
            "slug": tag_slug(name),
            "count": len(items),
            "posts": items,
            "preview": [i["title"] for i in items[:2]],
        })
    tags.sort(key=lambda t: (-t["count"], t["name"]))

    # ---------- 统计 ----------
    dates = sorted(p["date"] for p in posts if p["date"])
    years = sorted({d[:4] for d in dates}, reverse=True)
    total_words = sum(p["words"] for p in posts)
    stats = {
        "post_count": len(posts),
        "tag_count": len(tags),
        "total_words": f"{total_words:,}",
        "years_span": (int(years[0]) - int(years[-1]) + 1) if len(years) > 1 else 1,
        "latest_date": dates[-1] if dates else "",
        "year": dt.date.today().year,
        "build_time": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "build_rfc822": rfc822(dt.date.today().isoformat()),
    }

    base_ctx = dict(site=site, tags=tags, posts=posts, **stats)

    clean(out)

    def write(rel: str, content: str):
        path = out / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def render(tpl: str, **kwargs):
        ctx = dict(base_ctx)
        ctx.update(kwargs)
        return env.get_template(tpl).render(**ctx)

    written = []

    # ---------- 首页 ----------
    write("index.html", render("index.html", canonical="/", active="/"))
    written.append("index.html")

    # ---------- 文章详情 ----------
    for p in posts:
        write(f"posts/{p['slug']}/index.html", render(
            "post.html",
            canonical=p["url"],
            active="",
            post=p,
            prev_post=p["prev_post"],   # 时间上更新的一篇
            next_post=p["next_post"],   # 时间上更早的一篇
        ))
    written.append(f"posts/ ({len(posts)} 篇)")

    # ---------- 归档 ----------
    groups = []
    for y in years:
        items = [p for p in posts if p["date"].startswith(y)]
        if items:
            groups.append({"year": y, "posts": items})
    write("archive/index.html", render("archive.html", canonical="/archive/", active="/archive/", groups=groups))
    written.append("archive/")

    # ---------- 标签 ----------
    write("tags/index.html", render("tags.html", canonical="/tags/", active="/tags/"))
    seen = set()
    for t in tags:
        if t["slug"] in seen:
            continue
        seen.add(t["slug"])
        others = [x for x in tags if x["name"] != t["name"]][:20]
        write(f"tags/{t['slug']}/index.html", render(
            "tag.html", canonical=f"/tags/{t['slug']}/", active="/tags/",
            tag=t, other_tags=others,
        ))
    written.append(f"tags/ ({len(seen)} 个标签页)")

    # ---------- 独立页面 ----------
    pages = []
    for md_file in sorted((CONTENT / "pages").glob("*.md")):
        raw = md_file.read_text(encoding="utf-8")
        meta, body = parse_front_matter(raw)
        slug = meta.get("slug") or md_file.stem
        html_out, _ = render_markdown(body)
        page = {
            "slug": slug,
            "title": meta.get("title") or md_file.stem,
            "summary": meta.get("summary", ""),
            "html": html_out,
            "url": f"/{slug}/",
            "source": md_file.name,
        }
        pages.append(page)
        active = f"/{slug}/"
        write(f"{slug}/index.html", render("page.html", canonical=page["url"], active=active, page=page))
    written.append("pages/ (" + ", ".join(p["slug"] for p in pages) + ")")

    # ---------- 404 ----------
    write("404.html", render("404.html", canonical="/404.html", active=""))

    # ---------- 老 URL 跳转 ----------
    for old_path, target in LEGACY_REDIRECTS.items():
        write(old_path, REDIRECT_TPL.format(target=target, url=site["url"] + target))
    written.append(f"redirects/ ({len(LEGACY_REDIRECTS)})")

    # ---------- 管理后台 ----------
    write("admin/index.html", render("admin.html", canonical="/admin/", active="/admin/"))
    written.append("admin/")

    # ---------- 静态资源 ----------
    static_out = out / "assets" / "static"
    static_out.mkdir(parents=True, exist_ok=True)
    css = (THEME / "static" / "style.css").read_text(encoding="utf-8")
    pygments_css = "\n\n/* ===== Pygments 代码高亮 ===== */\n"
    pygments_css += HtmlFormatter(style="friendly").get_style_defs('html[data-theme="light"] .codehilite')
    pygments_css += "\n"
    pygments_css += HtmlFormatter(style="github-dark").get_style_defs('html[data-theme="dark"] .codehilite')
    (static_out / "style.css").write_text(css + pygments_css, encoding="utf-8")
    for f in (THEME / "static").iterdir():
        if f.is_file() and f.name != "style.css":
            shutil.copy2(f, static_out / f.name)
    written.append("assets/static/")

    # ---------- 图片资源（构建到外部目录时才需要复制） ----------
    src_images = ROOT / "assets" / "images"
    if src_images.exists() and (out / "assets" / "images").resolve() != src_images.resolve():
        dst_images = out / "assets" / "images"
        if dst_images.exists():
            shutil.rmtree(dst_images)
        shutil.copytree(src_images, dst_images)
        written.append("assets/images/（复制）")

    # ---------- RSS ----------
    write("feed.xml", render("feed.xml", canonical="/feed.xml"))
    written.append("feed.xml")

    # ---------- sitemap ----------
    urls = ["/", "/archive/", "/tags/", "/about/"]
    urls += [p["url"] for p in posts]
    urls += [f"/tags/{t['slug']}/" for t in tags if t["slug"] in seen]
    write("sitemap.xml", render("sitemap.xml", canonical="/sitemap.xml", urls=urls))
    written.append("sitemap.xml")

    # ---------- 搜索索引 ----------
    index = [{
        "title": p["title"],
        "url": p["url"],
        "date": p["date"],
        "tags": p["tags"],
        "summary": p["summary"],
        "plain": p["search_text"][:6000],
    } for p in posts]
    write("search-index.json", json.dumps(index, ensure_ascii=False, separators=(",", ":")))
    write("posts.json", json.dumps([{
        "title": p["title"], "url": p["url"], "date": p["date"],
        "tags": p["tags"], "summary": p["summary"],
    } for p in posts], ensure_ascii=False, indent=2))
    written.append("search-index.json / posts.json")

    # ---------- 杂项 ----------
    (out / ".nojekyll").write_text("", encoding="utf-8")
    write("robots.txt", f"User-agent: *\nAllow: /\n\nSitemap: {site['url']}/sitemap.xml\n")
    written.append(".nojekyll / robots.txt")

    print("构建完成 →", out)
    for w in written:
        print("  ·", w)
    print(f"  文章 {len(posts)} 篇 / 标签 {len(tags)} 个 / 共 {total_words:,} 字")


REDIRECT_TPL = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>页面已迁移</title>
<link rel="canonical" href="{url}">
<meta http-equiv="refresh" content="0; url={target}">
<meta name="robots" content="noindex">
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", "Microsoft YaHei", sans-serif;
         display: grid; place-items: center; min-height: 100vh; margin: 0; background: #fff; color: #14161a; }}
  .box {{ text-align: center; padding: 24px; }}
  a {{ color: #2f6df6; }}
</style>
</head>
<body>
<div class="box">
  <p>页面地址已更新，正在跳转…</p>
  <p><a href="{target}">如果没有自动跳转，点这里</a></p>
</div>
<script>location.replace("{target}");</script>
</body>
</html>
"""

if __name__ == "__main__":
    main()
