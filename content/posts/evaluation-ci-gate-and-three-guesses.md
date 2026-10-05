---
title: 给评测接上 CI 门禁：一条 209 个用例的检查，和一次改错两遍的归因
slug: evaluation-ci-gate-and-three-guesses
date: 2026-10-05
tags: Agent, 评测, CI, 工程实践
summary: 接着上一篇：reviewer 的 Blocking 只有一句——这 209 个新用例没有任何 CI job 跑。门禁接上之后它自己红了三次，前两次的归因都是错的：第一次怪错了锁文件，第二次把"宽度"当成了原因，真正的原因是 Typer 在 GITHUB_ACTIONS 下强制开色。记下这台"测量仪器"本身需要先修的过程，以及最后怎么在日志被挡住的情况下把绿灯验实。
---

上一篇写了这个 harness 的设计和 reviewer 揪出的六个洞。这一篇写收尾那一轮：把评测接进 CI，以及门禁接上之后它自己红的这三次。

那六个洞修完之后，第二位 reviewer 的 Blocking 只有一句：

> ### 1. The 209 new test cases are not run by any CI job

```
uv run --project evaluation pytest -c evaluation/pyproject.toml evaluation/tests -m "not live" -q
```

一句话，但这个洞和正文里那六个是同一个形状的——**一套没人运行的测试，等于一套"能被测"还没交付的测试。** 接上它花的时间不多，接上之后门禁自己红的三次才是这段经历的正文。

## 一、先把门禁接上，并且刻意收窄它的范围

为什么没有任何 job 收集到它：`evaluation/` 是一个独立的 uv 工程，有自己的 `testpaths`，而仓库根的 `unit-test` job 是在仓库根跑 `pytest`——两棵 testpath 不重叠，于是这个 benchmark 的测试从来只在人手上跑过。

修法是一行 Makefile 加一个 job：

```make
evaluation-unit-test:
	@uv sync --project evaluation --frozen
	@uv run --project evaluation pytest -c evaluation/pyproject.toml evaluation/tests/unit -m "not live" -q
```

注意范围是 `evaluation/tests/unit`，不是 reviewer 命令里的 `evaluation/tests`。这不是偷懒：`evaluation/tests/web` 和 `evaluation/tests/contract` **在 master 上本来就是红的**（`tests/web/test_worker.py` 5 个失败、`tests/contract/test_codex_contract.py` 1 个，web 整目录一起收集时会级联出 207 个 setup error）。把新门禁指向它们，它会在能报告任何关于改动的信息之前就先红掉。留下它们给拥有它们的那次改动。

> 经验法则：门禁的范围要收窄到"它的红一定能归因到它 gate 的东西"。一个出生就是红的检查，和没有检查的区别只是多了一个噪音源。

## 二、第一次红：先修"读不到日志"这件事

门禁第一次跑就红了，31 秒，其中测试步骤 20 秒。同一批 runner 上根套件要跑 556 秒、`Set up the environment` 只要 6–9 秒——所以没有任何测试跑到结束，失败在它前面的 `uv sync` 里。

查出一个真实缺陷：`evaluation/uv.lock` 的 29 个包**全部**解析自 `pypi.tuna.tsinghua.edu.cn`，而仓库根的 `uv.lock` 的 257 个全部来自 `pypi.org`。这份锁是在作者机器上有镜像配置的情况下生成的，镜像在那台机器上可达、在 Azure 的 runner 上不可达——所以这个工程**从来没能在 CI 里装上过**，这也正是"没有 job 收集它"能一直没人发现的原因。

重新对着 PyPI 解析这份锁，动的只有源：`name`/`version` 逐行对比为空，hash 与产物路径不变，只有 host 从 `pypi.tuna.tsinghua.edu.cn/packages/...` 变成 `files.pythonhosted.org/packages/...`。

**但这是真缺陷，不是这次的失败。** 下一次跑依旧红。这里我犯了一次典型的推理错误：从"锁解析自一个 runner 到不了的主机"推到"所以就是它"，中间跳过了"那 20 秒到底停在哪"的取证。修下一个提交时把这个判断错了的地方写进了 commit message——改归因也要留痕，否则下一轮 review 会以为镜像那件事已经解释完了。

真正挡住诊断的是权限：**从 fork 提交的 PR 读不到这次运行的原始 job 日志**，日志接口答 `Must have admin rights to Repository`；而 check 的注解只有 `Process completed with exit code 2.`——这一步跑的是 `make`，GNU make 对任何失败的 recipe 都返回 2，所以这条注解携带的信息量是零。两次红灯换回零信息，代价是两轮往返。

修法是把失败信息搬到一个 fork 读得到的载体上：测试步骤 `tee` 出日志，再加一个 `if: failure()` 的步骤，把失败用例写进 step summary，并发出 `::error::` 工作流注解（按命令要求把换行转义成 `%0A`），summary 里同时打出 `uv --version` 和 `python3 -V`。两个分支都用合成日志演练过：有失败用例时列 `FAILED <case>::<name>`，没有时打日志尾部。

> 经验法则：测量仪器坏了，修仪器就是修复的一部分。这次两次红灯的全部产出是"我不知道为什么红"——这不是运气差，是流程缺了一条通道。

## 三、第二次红：一次改错两遍的归因

拿到日志之后原因很清楚：`1 failed, 665 passed in 10.31s`，失败的是断言 `--judge-price-policy must be a JSON object` 出现在被拒绝调用的输出里。

**第一遍归因（错）：终端宽度。** 句子被渲染进一个面板，面板宽度跟随终端；本地只改宽度就复现了——80 列通过，60 列和 40 列失败，因为面板从**词中间**断行（`...must b` / `e a JSON object`），子串搜索因此找不到。于是提交了"固定宽度"。

下一次跑，依旧红。

**第二遍归因（对）：颜色。** 触发变量是 `GITHUB_ACTIONS`，Typer 把它硬编码成了"强制开终端"：

```python
# typer/rich_utils.py
FORCE_TERMINAL = (
    True if getenv("GITHUB_ACTIONS") or getenv("FORCE_COLOR") or getenv("PY_COLORS")
    else None
)
```

Rich 随后给报错里的选项名加样式，而且是**逐字符**加的，转义码就插进了断言那句话的字符之间（`\x1b[1;2;34m-\x1b[0m\x1b[1;2;34m-`）。**一个不再连续存储的句子，任何子串搜索都找不到它。**

定位方法是写一个探针，重复那次失败的调用，同时报告"原始搜索"和"去掉样式后再搜索"的结果，**一次只变一个环境变量**：

| 环境 | 原始搜索 | 去掉样式后 |
| --- | --- | --- |
| （空） | 找到 | 找到 |
| `CI=true` | 找到 | 找到 |
| `CLICOLOR=1` | 找到 | 找到 |
| `CLICOLOR_FORCE=1` | 找到 | 找到 |
| `TERM=dumb` | 找到 | 找到 |
| `FORCE_COLOR=1` | **找不到** | 找到 |
| `PY_COLORS=1` | **找不到** | 找到 |
| `GITHUB_ACTIONS=true` | **找不到** | 找到 |

`GITHUB_ACTIONS` 单独就足够，而它恰好是 GitHub runner 必定会设的一个变量——这才解释了为什么它只在 CI 上失败、在任何工作站上都通过，包括 40 列的时候。

修法是断言之前先剥掉样式。在 runner 的条件下对着这个用例自己的文件量了一遍：

```
env GITHUB_ACTIONS=true pytest evaluation/tests/unit/test_longmemeval_v2_cli.py
改之前：1 failed, 19 passed
改之后：20 passed
```

宽度固定保留了下来——它确实是个真实的脆弱点（有人把终端收窄到 60 列就会踩到），但它不是这次的原因。这一点也写进了 commit message，因为前一个提交把根因说成了宽度。

这三条是这轮最值钱的产出：

1. **"复现了"不等于"复现了触发条件"。** 第一遍的复现变的是一个 runner 根本不会变的变量（宽度），于是那是个凑巧带着绿勾的巧合。
2. **探针要一次只变一个变量，并且同时报"原始"和"归一化"两种结果。** 只有这样，表才能回答"哪个变量是充分的"，而不只是"存在一个变量"。
3. **优先选"与所有变量都无关"的修法**，而不是"刚好躲开当前那个变量"的修法。剥样式对上面八个环境都成立；`NO_COLOR=1` 只躲开当前这一个。

## 四、同一轮里剩下的几个洞

门禁之外的几条都收在同一批提交里，形状和上一篇一致——**读了载体的存在，没读载体遭遇了什么**：

- **比较门禁从来不读 run 声明的 arm 集合。** `comparable_arms` 是 runner 写进每个 manifest 的，但只要它的块缺失或为空，门禁照样放行。现在读它，并新增 `powercontext-eval work-continuity compare --baseline <run> --treatment <run>`——文档里的比较规则第一次有了生产上的调用者，而不只是被描述了。
- **恢复被"点名了材料"认证，而不是材料被投递。** 同一次评分已经在算"未投递的依赖"了，却没让它否决恢复。修完：`task_success: true` → `false` 且 `missing_evidence: 1`。
- **`budget_truncation` 覆盖不了"被上限砍掉的就是 next_action 本身"**（282 字节那次 `facts_lost_to_the_budget` 为空，`next_action_dropped: true`）：`vague_next_action` → `budget_truncation`；多留一个字节、动作保住了，就仍然是 `vague_next_action`。
- **`context_quality` 分不清"没检查"和"检查了没问题"。** 加了 `applicable`，报告对从未检查的方法渲染 `n/a`；shipped fixture 里三个转录方法 `applicable=false`，`rollover-handoff-v1` 是 6/6。
- **测试缺口**：`host_revision` 报两个值要拒绝（原来只覆盖了 `model`）、Unicode 边界要用真正多字节的载荷驱动、shipped 任务锁用 sha256 钉死。

单测 653 → 666 条；shipped fixture 的公开数字一个都没动（注入字节 8784/7、5007/0、1711/0、6697/11），同一个 run id 的 6 个产物依旧逐字节相同。

## 五、最后一步：证明门禁不只是绿，而且真的跑了

所有验证都在** runner 的条件下**做，也就是带着 `GITHUB_ACTIONS=true`。推上去之后，Main 工作流的 11 个 job 全部 `success`，`evaluation-tests` 整个 27 秒，拆开是：

| 步骤 | 结论 | 耗时 |
| --- | --- | --- |
| Set up the environment | success | 10s |
| Run the evaluation project unit tests | success | 10s |
| Report the failing cases | **skipped** | — |

`Report the failing cases` 被 skip 是对的——它只在该步骤失败时运行，它没跑就是"没有失败用例"。

这里顺带学到一个可复算的取证手法。fork 的 PR 读不到 job 日志，但**状态仍然在运行页的 HTML 里**：每个 job 是一个 `<streaming-graph-job data-job-id=... data-concluded="true">` 元素，行内的图标与 `aria-label` 就是结论，每一步是一个 `<check-step data-name=... data-conclusion="success">`。日志被权限挡住的时候，结论没有；这比"截一张图说绿了"更可核验。

还有一个数字值得记：这 666 个用例在本地 macOS 上要跑 50–95 秒，在 runner 上 10 秒。同一份代码。这也说明我最早那次"20 秒内没有任何测试跑完"的推断站不住——它成了我第二次归因错误的土壤。

## 六、边界

照例把边界写在正文里：上面所有数字都来自 authored fixture 和这个仓库自己的测试，它们证明的是**这把尺子能量、且从此被门禁守着**，不是任何真实环境的表现。要谈真实结论，还差接真实 host 的录制和更大的任务集。

> PR：https://github.com/oceanbase/powercontext/pull/1819
> Issue：https://github.com/oceanbase/powercontext/issues/1791
> 上一篇：https://jasondeng1997.github.io/posts/work-continuity-eval-harness/
