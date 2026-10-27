---
title: 冲突清完了，地基还在动：把 1.4 万行搬回上游 master
slug: rebase-onto-master-four-displacements
date: 2026-10-27
tags: 分布式, 工程实践, 复盘, 数据库
summary: 50 个冲突文件、115 个冲突块，一天清完，冲突标记归零——然后 14 个测试挂在同一句话上。把 #1446 的 1.4 万行搬到当前 master 上，真正的成本不是合并冲突，是分支依赖的四样东西在 641 个 commit 里被删掉、搬走、改语义、或者反过来开始依赖你。
---

`#1446` 在 10 月 19 日被关掉，base 分支 `feat/distributed` 在上游已经不存在了。所以「重新打开」不是一个选项——对着今天的 master，这个分支落后 **641 个 commit**、**143 个文件 +13282 / −2197**、**50 个冲突文件**。

我把它 port 到了 `cdb8da29`（PR #2240 之后的 master）上。冲突清完，冲突标记 0 个，`import powercontext.builtin.runtime` 通了，然后 `tests/builtin/persistence/` 里 **14 个测试全挂在同一句话上**：

```
MigrationError: unsupported_configuration:
The versioned database does not cover the requested feature tables.
```

那不是冲突，是一块缺掉的迁移 bundle。

结论先给：**rebase 的成本不在冲突里，在「你以为还在的东西」里。**

## 一、先把处境摆出来，因为数字本身在讲一件事

| 项 | 值 |
| --- | --- |
| merge-base | `e0e65643` |
| master 领先 | 641 commit |
| 分支相对旧 base | 143 文件，+13282 / −2197 |
| 实际冲突 | 50 个冲突路径：38 个文件 / 115 个冲突块，其余是删除与重命名类 |
| 冲突清完后的规模 | 111 文件，+14032 / −337（45 新增、66 修改、**0 删除**） |

最后一行那个「0 删除」后面会单独讲一节，它是我这次最没想到的发现。

先说冲突。115 个冲突块听起来很吓人，但它是**可见成本**：可数、可分桶、可以按文件并行。真正的成本是另外四件事，它们一个都不出现在 `git status` 的 `UU` 列表里。

## 二、工具报的冲突数，比实际多 3.6 倍

开工前我先用 `git merge-tree --name-only` 估了一下工作量，它报 **179 个冲突文件**。实际 `git merge` 停下来时是 **50 个**。

差 3.6 倍。原因不难猜：`merge-tree --name-only` 报的是「两侧都动过这个文件」，而「两侧都动过」不等于「合不上」。一个文件两侧各改一行互不相邻的代码，merge-tree 会把它列进冲突，git 自己合并完根本不会停。

这件事的教训不是「别用 merge-tree」，是：**这类输出适合估工作量，不适合判断冲突范围。** 如果拿它排期，你会给自己留出三倍半的空，然后在真正硬的地方没有时间。

好在冲突可分桶，分桶的价值不是分类，是**排序**——A 桶决定其他所有桶的解：

| 桶 | 内容 | 冲突块 |
| --- | --- | --- |
| A 结构性重接 | `runtime/scheduler.py`、`composition.py`、`application.py`、`artifact_processing.py` | 25 |
| B 服务端装配 | `server/{factory,app,mapping,cli,metrics,tracing,mcp}.py`、`client/client.py` | 39 |
| C 持久化 | `database.py`、三个后端 `profile.open()`、`migrations/__init__.py` | 7 |
| D 生成文件 | `openapi/powercontext.yaml`、`http/_generated/operations.py` | 18 |
| E 测试 | 12 个测试文件 | 26 |

D 桶值得单独说一句：`_generated/operations.py` 有 17 个冲突块，**一个都不能手改**。它是 `scripts/generate_api.py` 的输出，正确顺序是先把 openapi 合完、再重跑生成器。手改生成文件是那种「当下最快、两小时后最慢」的选择。

## 三、为什么用 merge，不用 rebase

这一节先把工具选掉，因为它决定了后面所有成本的形状。

分支上有 24 个 commit，其中 **10 个是 merge commit**（42%）。面对这个形状，三个选择：

| 做法 | 冲突要解几次 | 代价 |
| --- | --- | --- |
| `git rebase` | **24 次**（每个 commit 各一次） | 历史干净；但每解错一次都会顺延到下一个 commit，且改写历史会让已有的引用关系断掉 |
| `git merge` | **1 次** | 历史里多一个 merge commit；原来的 24 个 commit 一个不动 |
| squash 成一个 | 1 次 | 一个 1.4 万行的提交，没法 review，也没法二分 |

我选 merge。

理由不是「省事」，是**这次要验证的问题变了**。原本那 24 个 commit 回答的是「每一步改动对不对」；现在要回答的是「这堆改动作为一个整体，能不能挂在今天的 master 上」。这是两个不同的问题，而 24 次冲突解决会把第二个问题淹死在第一个里——你会在解第 7 个 commit 的冲突时，忘记第 3 个 commit 里那个语义已经变了的前提。

merge 还留了一个实际好处：原分支的 24 个 commit 原封不动地躺在历史里。后面要判断「这段代码当初为什么这么写」，`git log acd4cf5f` 就能回答，不用靠记忆。

代价也在这一节说清楚：merge 之后，这个分支的 diff 是「相对 master 的全部改动」，不再是「相对原 base 的增量」。于是所有「我们删掉了什么」的信息都不在 diff 里了——这个副作用有多严重，第七节专门讲。

## 四、四个地基位移

冲突清到一半我才意识到，这次不是 rebase，是**照着移动过的地基重写**。

![四个地基位移：分支依赖的四样东西在 641 个 commit 里全部变过](/assets/images/posts/rebase-onto-master-four-displacements/01-four-displacements.svg)

### 位移一：master 删掉了你正在扩展的模块

| 文件 | 我们 | master |
| --- | --- | --- |
| `persistence/memory_index.py` | +11 / −1 | **−283（整删）** |
| `persistence/oceanbase/memory_index.py` | +44 | **−506（整删）** |
| `benchmark/locomo_plus/*` | 修改 | 删除 |

master 把 Memory 索引收进了 `persistence/atomic_projection.py`。我们在那两处一共只加了 55 行——不多，但**这 55 行没有「合并」这个选项**。它挂在一个已经不存在的结构上，只能在新架构里重写。

这类冲突最容易被误判：文件删了，git 给你一个 `DU`（deleted by them），看起来就是「接受删除」四个字。接受了就少了 55 行功能，而测试多半还是绿的——因为那 55 行是给某个特定路径加速的。

### 位移二：同名函数不代表同一件事

`database.py` 在 master 上被重写了（+229 / −28）。我们依赖的两个原语，一个搬家，一个消失：

- **`database_now()`** 搬到了 `persistence/dream.py:42`，而且**语义从 naive UTC 改成了 tz-aware**。账本按 naive UTC 存 `lease_expires_at`，直接复用 master 那个会在 claim 路径炸：`can't compare offset-naive and offset-aware datetimes`。
- **`insert_if_absent()`** 在 master 上搜不到了。它被降级成 `cursors.py` 里一个表专用的私有函数（`_insert_if_absent(connection, scope_id=…, binding_name=…)`）。而整个 Work Ledger 的「唯一键竞争不中断事务」就靠它——OceanBase 的 async dialect 下 SAVEPOINT 方案不安全，这点 RFC 里写过。

所以我把两个原语回填进 `database.py`，并在 docstring 里写清楚为什么不能共用 master 的版本。

`insert_if_absent` 值得多讲两句，因为它是那种「看起来能用 savepoint 重写、其实不能」的函数：

```python
"""Insert validated values without aborting the transaction on a unique-key race.

OceanBase's async dialect deliberately leaves ``do_begin`` empty.  A
zero-row locking update therefore may not establish a server transaction,
which makes a SAVEPOINT-based insert race unsafe.
"""
```

说人话：常见的写法是「先 `SELECT … FOR UPDATE` 探一下有没有人占位，没有就插，撞了 unique key 就滚回 savepoint」。但在 OceanBase 上，那次探测如果返回零行，**可能压根没有在服务端开出一个事务**——而 savepoint 必须在事务里才有意义。于是「滚回 savepoint」变成了一句在真空里说的话，事务状态已经不可预期。

所以这里用方言自带的冲突容忍插入：MySQL 走 `INSERT IGNORE`，SQLite 走 `OR IGNORE`，靠 `rowcount == 1` 判断结果。注意这个返回值的含义——它回答的不是「成功还是失败」，而是**「这次是不是我占的位」**。在唯一键竞争里这两件事正好相反：别人先插进去时，你的插入在数据库意义上失败了，但调用方要走的恰恰是「已经有主」那条分支，而且事务**不能**回滚。

一个函数的返回值语义，比它的实现更容易在迁移里被误解。

这里有件容易被跳过的事：**迁移原语的第一步不是确认它存在，是确认它的语义契约没变。** 名字一样、签名一样、返回值一样，一个时区标记就能让一条 claim 路径在测试之外的地方静默出错。

### 位移三：整套迁移框架被换掉

我们这边是 `migrations/versions/0002_work_ledger.py`——一个 Alembic revision，八个表。

master 这边，`migrations/` 变成了 bundle 体系：`bundle.py` / `domains.py` / `locking.py` / `server_resources/versions/`，而 `__init__.py` 的文档里写着「Nothing in this package runs during startup」。旧的 Alembic runner（`persistence/migration.py`）已经被删掉。

`0002_work_ledger` 必须改写成 master 形态的 bundle，否则 `powercontext server db-migrate` 根本不认识它。这是第六节的事。

### 位移四：你删掉的调度器，master 还在用

| 文件 | 我们 | master |
| --- | --- | --- |
| `runtime/scheduler.py` | **−235（整文件删除）** | +8 / −3，仍在用 |
| `tests/builtin/runtime/test_scheduler.py` | 删除 | 修改 |

分支用 `durable_scheduler.py` + `worker.py` + Work Ledger 取代了 APScheduler 适配器——这是原 PR 的一个核心设计决定。但 master 的 `composition.py` / `factory.py` / `app.py` 全都还在 import 它，而且这 641 个 commit 里又有新的定时任务挂了上去。

**这是一个反向依赖**：删除的前提是「没人再用」，而新挂上去的任务每一个都要改接到 Work Ledger 上。这不是解冲突，这是设计决策，并且它决定 A 桶怎么解。

我这次的取舍是：**先让 `scheduler.py` 回到 master 版本**，把「干掉 APScheduler」这件事放到运行时装配那一步再做。理由很朴素——一次 rebase 里不能同时验证「搬家正确」和「替换正确」，两个变量一起动，失败了你不知道是谁的错。

### 这四件事的共同点

用一句话说完：**它们都不是「两侧都改了」，而是「只有一侧改了，而改的是另一侧依赖的东西」。**

git 的三方合并解决的是「同一处文本两侧都改过」。它不解决「你依赖的接口换了语义」——后者甚至不会产生冲突，只会产生一个能编译、能启动、然后在某个晚高峰才炸的程序。

所以 rebase 的正确起点不是 `git merge`，是一张依赖清单：我 import 的、我调用的、我继承的、我删的，各自在对面是什么状态。

## 五、一张控制表，决定函数是「建」还是「验」

冲突清完，14 个测试挂了，报错完全一样。根因是 `schema.py` 里这 12 行：

```python
async def create_tables(connection: AsyncConnection, tables: Sequence[Table], /) -> None:
    """Create selected tables only for unmanaged storage; managed schemas are read-only."""

    names = await connection.run_sync(lambda sync: set(inspect(sync).get_table_names()))
    if "pc_schema_revision" in names:
        if not {table.name for table in tables} <= names:
            raise MigrationError(
                "unsupported_configuration",
                "The versioned database does not cover the requested feature tables.",
            )
        return
    # 没有 pc_schema_revision：按 metadata 建表
    ...
```

![门禁与指纹：一张控制表把 create_tables 切成创建和校验两种语义](/assets/images/posts/rebase-onto-master-four-displacements/02-gate-and-fingerprint.svg)

说人话：**这张表是数据库的成年礼。成年前你可以给它建表；成年后它只接受体检。**

- 库里有 `pc_schema_revision`（跑过迁移，是「托管库」）→ `create_tables()` 不建表，只断言「你请求的这些表，库里都有」。缺一个就抛错然后返回。
- 库里没有（空库，本地开发和测试用的）→ `metadata.create_all(checkfirst=True)`，真的建。

同一个函数，两种语义，靠一个控制表的存在与否切换。这个设计很干净：调用方只需要表达「这些表我要用」，至于「谁来建」由库自己的状态决定，不由调用方猜。

后果也很直接：我们把八张账本表挂进了 `BUILTIN_TABLES`，却没挂进任何 revision 清单。于是本地全绿、托管库全挂——而那 14 个测试用的是托管库路径。

**这不是 bug，是缺一块迁移 bundle。** 或者更准确地说：schema 的所有权被显式登记了，代码里「这些表是我建的」这句话不再算数。

## 六、写这块 bundle：四件事，一件比一件反直觉

### 5.1 revision 链是线性的

清单里已有的链是 `s1300 → s1400 → s1401 → s1402`，我们加的是 `s1403`，`down_revision = "s1402"`。没有分支、没有合并节点。八张表：`pc_work_lanes` / `pc_work_items` / `pc_work_keys` / `pc_work_attempts` / `pc_scheduler_leases` / `pc_scheduler_scans` / `pc_runtime_members` / `pc_rate_limit_windows`。

### 5.2 冻结 DDL，而不是「用 metadata 建表」

新 revision 不调用 `create_all`，它读一个 JSON，逐条执行里面冻结好的 SQL：

```python
def upgrade():
    resource = Path(__file__).parents[1] / "schemas/sqlite-s1403.json"
    connection = op.get_bind()
    for statement in json.loads(resource.read_text())["create"]:
        connection.exec_driver_sql(statement)
```

为什么冻结：**为了让「托管库升级出来的 schema」和「非托管库 `create_all()` 出来的 schema」逐字节一致。** 只要有一边是从代码现编译的，SQLAlchemy 版本一升级，两边就可能漂移，而漂移只会在某个用户的升级路径上暴露。

有意思的是两个文件的体量差：

| 文件 | 大小 | 存了什么 |
| --- | --- | --- |
| `sqlite-s1403.json` | 5.5 KB | 8 条 `CREATE TABLE` + 7 条 `CREATE INDEX`——**结果** |
| `mysql-s1403.json` | 120 KB（4234 行） | 同样的 DDL，外加每条语句的 parse transition——**过程** |

同一个 schema，一个存结果，一个存过程，成本差 22 倍。原因是两侧的比对方式不同：SQLite 侧比的是 `sqlite_schema.sql` 的原文，MySQL/OceanBase 侧比的是结构，所以每条语句的每一项变更都要能被单独引用和核对。

冻结还有一个连锁后果，我一开始没想到：**这些 SQL 文本不只是被执行，它们还是下游的输入。**

SQLite 侧的向量布局（`ServerVectorProfile.sqlite_layouts`）要从同一批冻结语句里推导出「这条语句登记了哪个对象、归谁所有」：

```python
def registry_row(statement: str) -> list[str]:
    """Map one frozen DDL statement onto the identity row SQLite would record."""

    tokens = statement.split()
    if len(tokens) < 3 or tokens[0].upper() != "CREATE":
        raise MigrationError("invalid_bundle", "A frozen schema statement is not a CREATE.")
    kind = tokens[1].upper()
    if kind == "TABLE":
        name = tokens[2]
        return ["table", name, name, statement]
    if kind == "INDEX" and len(tokens) > 4 and tokens[3].upper() == "ON":
        return ["index", tokens[2], tokens[4], statement]
    raise MigrationError("invalid_bundle", "A frozen schema statement has no registry identity.")
```

于是「改个写法」不再是改个写法。`CREATE INDEX IF NOT EXISTS ix ON t (c)` 会在这里直接抛错——`tokens[3]` 是 `IF`，不是 `ON`。`CREATE TABLE IF NOT EXISTS` 同理。

冻结的含义到这里才完整：**这些文本变成了一份有消费者的契约，不再是一份快照。** 快照可以随便重拍，契约不行。

### 5.3 指纹只能测出来，不能写出来

`manifest.json` 里给每个 revision 记一个 `sqlite_fingerprints`。算法是从实际库里清点出来的：

```python
def sqlite_inventory(connection, *, exclude=frozenset()) -> list[list[Any]]:
    """Include tables, indexes, triggers, views and constraint-bearing SQL.

    This deliberately uses exact SQLite definitions. A semantically equivalent
    but unregistered schema requires review, never an optimistic stamp.
    """
    rows = connection.exec_driver_sql(
        "SELECT type, name, tbl_name, sql FROM sqlite_schema "
        "WHERE substr(name, 1, 7) != 'sqlite_' ORDER BY type, name"
    )
    ...
```

清点 → 规范化 JSON → SHA256。所以新指纹不能凭空写，只能：**从 s1300 真的跑到 s1403，把当时库里的 schema 清点出来算一遍。**

手写一个指纹等于「声称」，而声称无法被验证。这里唯一能自证的办法是：用同一套方法重放历史 revision，看能不能复现清单里已经记录的值。

| revision | 清单记录的指纹（前 16 位） | 重放结果 |
| --- | --- | --- |
| s1300 | `11e8770068a5fe32` | 一致 |
| s1400 | `9064ecb0c6233884` | 一致 |
| s1401 | `8752beed9acfb66e` | 一致 |
| s1402 | `795b6fd01188c15d` | 一致 |
| s1403 | `1f15759e8f4708f4` | 本次新增 |

前四个是我算之前就已经在库里的值，逐字节相同。于是第五个值不是「看起来对」，是**这套方法在这个仓库上已经被验证过四次**。

这条经验可以搬走很远：**任何「记录下来的状态」都应该能被重新推导出来，否则它只是一条注释。** 指纹、checksum、schema hash，都一样——不能重放的记录，保护不了任何东西。

### 5.4 匿名约束没有身份

最后一个是这次唯一改到 schema 本体的地方。`pc_work_keys.work_id` 原本写的是：

```python
Column("work_id", identity_string(36), nullable=False, unique=True)
```

`unique=True` 在库里生成一个**匿名** UNIQUE 约束。而冻结清单要登记约束才能校验，匿名约束没有名字可以引用——MySQL/OceanBase 侧的结构登记和校验都做不了。

改成：

```python
Column("work_id", identity_string(36), nullable=False),
UniqueConstraint("work_id", name="uq_pc_work_keys_work_id"),
```

一行 schema 改动，动机不是「更规范」，是**可被引用**。匿名的东西进不了清单，也就进不了校验。

冻结文件里能直接看到那个名字：

```json
"CREATE TABLE pc_work_keys (\n\tlogical_key VARCHAR(64) NOT NULL, \n\twork_id VARCHAR(36) NOT NULL,
 \n\tlane_key VARCHAR(64) NOT NULL, \n\tcreated_at DATETIME NOT NULL, \n\tPRIMARY KEY (logical_key),
 \n\tCONSTRAINT uq_pc_work_keys_work_id UNIQUE (work_id)\n)"
```

约束名进了 SQL 文本，清单才有一个字符串可以引用，MySQL 侧的结构比对也才有位置可以定位。同一个改动如果写成 `unique=True`，约束在库里存在、能生效、能拦重复插入——**只是没名字**，于是三样东西登记不了：清单、指纹之外的结构比对、以及恢复文档里的列举。

改完之后 `tests/builtin/persistence/` 是 **686 passed / 110 skipped**（skipped 全是 MySQL/OceanBase 实机用例）。被改的断言都是「已发布 head 是 s1402」这类硬编码。另外 `docs/{en,zh}/operate/troubleshoot.md` 的 obloader 恢复层要补上四张 durable 账本表，否则文档测试会数出 54 ≠ 58——这条是测试替我发现的，不是我想到的。

## 七、rebase 会把「删除」变成「没做」

这是我最想写下来的一节。

原分支相对旧 base，删了两个文件：`runtime/scheduler.py` 和 `tests/builtin/runtime/test_scheduler.py`，还有一堆被 master 覆盖的删除，合计 **−2197 行**。

rebase 之后，相对新 master：

```
45 新增 / 66 修改 / 0 删除
−337 行
```

`git diff --diff-filter=D` 是**空的**。

也就是说，这个 PR 现在看起来完全不想动 APScheduler。而「用 Work Ledger 取代 APScheduler」恰恰是它最初的核心设计决定之一——它只是还没搬回来。

而在 diff 里：

> **「删除」和「尚未删除」长得完全一样：都是没有。**

这是 rebase 类 PR 的结构性问题。评审者看到 45 个新增文件、66 个修改文件，会得到两个都不对的印象：要么以为你在加东西、没有替换任何东西；要么以为你把原来的删除偷偷撤销了。

更隐蔽的是中间状态。36 个文件现在与 master 是同一个 blob，因此**不出现在 diff 里**：

```
runtime/composition.py        runtime/application.py     runtime/relational.py
runtime/scheduler.py          runtime/artifact_processing.py
server/{app,factory,cli,mapping,metrics,tracing,mcp}.py
client/client.py               openapi/powercontext.yaml
http/_generated/operations.py  migrations/__init__.py
pyproject.toml                uv.lock                        e2e/bub/uv.lock
```

这些不是「没改」，是「当前版本恰好等于 master」。其中 `composition.py`（16 个冲突块）、`server/app.py`（9 个）、`factory.py`（12 个）、`openapi` 这四处的丢失，各自代表一件还没做完的事：

1. `composition.py` 现在没有 `_work_services` / `_start_work_background` / `WorkExecutionHooks`——账本没有运行时装配。而且 master 把 `_open_runtime_storage` 内联成了三个后端分支，每个分支都有自己的 managed 判断和 `BEGIN` / `BEGIN IMMEDIATE` 选择，要把装配插回去得插三遍。
2. `openapi/powercontext.yaml` 是我们的版本被 master 覆盖了，四个 `/v1/operations*` 端点不在。
3. `_generated/operations.py` 必须由生成器重跑。
4. server 侧 worker / scheduler 角色的启动参数还没接。

**所以 PR 的 diff 不能当进度条读。** diff 小可能意味着「还没做」，也可能意味着「做完了且与上游一致」——两种含义相反的状态，在 diff 里是同一个样子。

结论：rebase 类 PR 的正文必须单独报告**消失的改动**，不能指望 diff 自己说话。我列的「Still missing」三件事，本质上是给 diff 补一份反向清单。

## 八、还有一套没人执行的迁移脚本

诚实报告一个遗留：`migrations/versions/` 下还躺着 8 个 Alembic revision（`0001_baseline` 到 `0008_dream_recurrence`），加上 `env.py` 和 `script.py.mako`。

但 master 上这个目录**根本不存在**，Alembic runner（`persistence/migration.py`）已经被删，`migrations/__init__.py` 也明确写着启动时不跑任何东西。所以这 8 个文件现在是**孤儿**：既不被执行，也不被 import。

测试不会告诉我这件事，因为没有测试 import 它们。冲突解决也不会——它们是从旧 base 一路带过来的新增文件，从来没跟 master 撞过。

> rebase 的完成标准不是「测试绿」，是「没有孤儿」。冲突标记可以 grep，孤儿不能。

## 九、现在的位置

| 检查 | 结果 |
| --- | --- |
| 冲突标记（`git grep '^<<<<<<<'`） | 0 |
| `import powercontext.builtin.runtime` / `…composition` / `…server.app` | 通过 |
| `tests/builtin/persistence/` | **686 passed** / 110 skipped（MySQL、OceanBase 实机） |
| `tests/builtin/runtime/` | 959 passed / 4 failed（merge 既有问题，stash 掉本次改动后同样失败） |

剩下三件事按依赖排序：

| # | 任务 | 卡在哪 |
| --- | --- | --- |
| 1 | `composition.py` 补回账本装配 | master 把存储打开内联成了三分支，`_work_services` / `_start_work_background` 要重新插 |
| 2 | openapi 合并 + `scripts/generate_api.py` 重生成 | 四个 `/v1/operations*` 端点不能手写 |
| 3 | server 侧 worker/scheduler 角色装配 | 依赖 1 的形状 |

## 十、能搬走的五条

1. **冲突是可见成本，地基位移是隐藏成本。** 开工前先问：我 import 的、我调用的、我删的，在对面分别是什么状态。这个清单比 `git merge` 的输出有用得多。
2. **同名函数不代表同一件事。** `database_now` 一边 naive、一边 aware，签名和返回值都一样，爆炸点在 claim 路径上。
3. **冻结 schema 的价值是让两条创建路径产出同一份字节**；配套的指纹必须能重放——不能重放的记录只是注释。
4. **约束要命名。** 匿名的东西无法被引用，也就无法被校验。这一条在「生成文件」和「迁移清单」里都成立。
5. **rebase 之后单独报告消失的改动。** 删除会伪装成「没做」，进度会伪装成「没动」。diff 只讲新增，不讲意图。

## 边界

这篇只讲把 #1446 的分支 port 到当前 master 的过程。它**仍然不能合**：运行时装配缺失、四个端点丢了、`migrations/versions/` 还有 8 个孤儿。

`mysql-s1403.json` 那 4234 行没有在真实 OceanBase / MySQL 上跑过——没有可用后端，CI 之前它属于「未验证」。4 个 runtime 失败是 merge 带来的既有问题（atomic-memory extraction 的契约），不是这次移植引入的。

也不能由此推出「长命分支不值得」。反过来的结论更准确：**分支的寿命越长，它依赖的东西被搬动、改语义、或反过来依赖它的概率越高**，而这些变化在冲突列表里是看不见的。要么缩短分支寿命，要么在每次 rebase 前先做那张依赖清单。

---

相关链接：

- PR #1446（已关闭）：https://github.com/oceanbase/powercontext/pull/1446
- RFC 1430（在分支内）：https://github.com/oceanbase/powercontext/blob/ea3669f168/docs/zh/rfcs/1430_distributed_server_workers.md
- 前一篇：fence 写对了，洞还在——拆一个被关掉的分布式任务系统：https://jasondeng1997.github.io/posts/work-ledger-fence-and-two-holes/
