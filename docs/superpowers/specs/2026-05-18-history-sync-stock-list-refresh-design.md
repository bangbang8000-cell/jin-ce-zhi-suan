# History Sync Stock List Refresh Design

## 1. Background

当前 `history_sync` 的股票列表解析顺序是：

- 请求体显式传入的 `codes`
- 本地文件 `data/stock_list.csv`
- 配置中的 `targets`

这意味着，只要 `data/stock_list.csv` 被及时更新，现有历史增量同步链路就能自动使用最新股票池，而不需要修改 `history_sync` 核心执行逻辑。

现状缺口是：

- 项目没有专门的“股票列表更新脚本”
- 没有一条稳定的“免费数据源优先、收费数据源兜底”的股票池刷新链路
- `history_sync` 可以自动执行，但不能自动刷新全 A 股票池

## 2. Goals And Non-Goals

### 2.1 Goals

- 新增一个独立脚本，用于更新 `data/stock_list.csv`
- 默认生成“全 A 股票”清单，覆盖沪市、深市、北交所 A 股代码
- 默认使用 `AkShare` 作为免费优先源
- 当 `AkShare` 不可用、返回空结果或字段异常时，自动回退到 `TuShare`
- 输出格式兼容现有 `history_sync` 读取逻辑
- 在双源都失败时保留旧的 `data/stock_list.csv`，不写入空文件

### 2.2 Non-Goals

- 不修改 `history_sync` 的股票解析优先级
- 不把股票池刷新逻辑塞进 `history_sync` 主流程
- 不新增服务端 API 或前端按钮
- 不在本次实现行业、板块、自定义市场分层过滤
- 不在本次实现股票池数据库持久化

## 3. Scope

本次改动范围限定为：

- 新增脚本：`scripts/update_history_sync_stock_list.py`
- 如有必要，新增一个轻量工具模块用于代码标准化与 CSV 写入
- 补充脚本使用说明文档

本次不改动：

- `src/utils/history_sync_service.py`
- `dashboard.html`
- 定时调度逻辑
- 现有 provider 的分钟/日线拉取逻辑

## 4. Approaches

### 4.1 Recommended Approach: Standalone Refresh Script

新增一个独立脚本，执行流程如下：

1. 读取命令行参数，默认 `provider=auto`
2. 当 `provider=auto` 时优先调用 `AkShare`
3. 若 `AkShare` 拉取失败或数据非法，则自动切换到 `TuShare`
4. 将结果标准化后写入 `data/stock_list.csv`
5. 写入成功后输出统计日志
6. 若双源都失败，则保持旧文件不变并返回非零退出码

优点：

- 与现有同步主链路解耦
- 可手动执行，也可被 Windows 计划任务调用
- 风险低，符合最小侵入原则

缺点：

- 股票池更新与同步执行仍是两个步骤，需要由人工或调度编排起来

### 4.2 Rejected Approach: Refresh Inside History Sync

在每次 `history_sync` 启动前自动刷新股票池。

不采用原因：

- 会把“股票池生成”和“历史同步”强耦合
- 免费源异常时会直接影响同步主流程
- 不利于单独排查股票池问题

### 4.3 Rejected Approach: Add New API First

先新增一个服务端接口触发股票池刷新。

不采用原因：

- 首版目标只是打通可用能力
- API 方案会扩大改动面，需要同步修改路由、文档和前端交互
- 当前独立脚本已经能满足手动和自动调用需求

## 5. Detailed Design

### 5.1 Script Entry

新增脚本：

- `scripts/update_history_sync_stock_list.py`

支持的参数建议如下：

- `--provider auto|akshare|tushare`
- `--output data/stock_list.csv`
- `--overwrite true|false`，默认 `true`

默认行为：

- `provider=auto`
- `output=data/stock_list.csv`
- 允许覆盖旧文件，但仅在新结果有效时覆盖

### 5.2 Data Sources

#### AkShare First

优先使用 `AkShare` 的全 A 股票基础信息接口拉取代码和名称。

脚本只依赖“股票列表”能力，不复用分钟/日线行情逻辑。这样能避免把 `AkshareProvider` 中的行情抓取职责扩展成股票池管理职责。

AkShare 返回的数据至少需要满足：

- 可提取股票代码
- 可提取股票名称
- 返回结果非空

若字段缺失、结果为空、请求异常或标准化后记录数为 `0`，则判定本源失败。

#### TuShare Fallback

当 `AkShare` 失败时，回退到 `TuShare` 股票基础信息接口。

回退条件包括：

- AkShare 调用抛错
- AkShare 返回空表
- AkShare 无法提取出有效代码列
- AkShare 标准化后没有任何有效 A 股代码

TuShare 作为兜底源，要求本地已有可用 token。若 token 未配置或接口失败，则该回退也视为失败。

### 5.3 Code Normalization

输出代码统一标准化为项目当前通用格式：

- `600000.SH`
- `000001.SZ`
- `430001.BJ`

标准化规则：

- `60`、`68` 开头归为 `SH`
- `00`、`30` 开头归为 `SZ`
- `4`、`8` 开头归为 `BJ`

过滤规则：

- 仅保留 6 位纯数字股票代码
- 仅保留可映射到 `SH`、`SZ`、`BJ` 的 A 股代码
- 过滤空代码、重复代码和无法识别市场的记录

### 5.4 Output File Schema

输出文件默认写为 `data/stock_list.csv`，字段建议为：

- `code`
- `name`
- `market`
- `source`
- `updated_at`

兼容性说明：

- `history_sync` 当前只强依赖 `code`
- 其余字段用于人工校验、问题排查和后续扩展

写入策略：

- 先写临时文件
- 校验通过后再原子替换目标文件
- 避免写一半导致 `stock_list.csv` 损坏

### 5.5 Failure Safety

需要严格遵守以下保护规则：

- 若新拉取结果为空，不覆盖旧文件
- 若标准化后有效记录数异常为 `0`，不覆盖旧文件
- 若 AkShare 和 TuShare 都失败，不覆盖旧文件
- 脚本应返回非零退出码，方便被调度系统识别失败

### 5.6 Logging

脚本应输出清晰日志，至少包含：

- 实际使用的数据源
- 原始记录数
- 标准化后记录数
- 输出文件路径
- 是否发生回退
- 是否保留旧文件

示例日志语义：

- `股票池更新开始：provider=auto output=data/stock_list.csv`
- `股票池更新回退：akshare失败，切换到tushare`
- `股票池更新完成：source=akshare codes=5513 output=data/stock_list.csv`

## 6. Data Flow

完整流程如下：

1. 用户手动执行脚本，或由计划任务调用脚本
2. 脚本按 `auto -> AkShare -> TuShare` 顺序尝试获取全 A 股票列表
3. 结果被标准化为统一 `code` 格式
4. 新列表写入 `data/stock_list.csv`
5. 后续 `history_sync` 执行时，按现有逻辑自动读取最新股票池

## 7. Error Handling

- AkShare 失败时，自动记录错误并尝试 TuShare
- TuShare 失败时，输出最终失败原因并退出
- 如果配置中没有 TuShare token，不把它视为脚本错误实现，而是视为运行环境限制，并在日志中明确提示
- 如果输出目录不存在，脚本负责创建目录

## 8. Testing Strategy

首版测试以高价值单元测试为主：

- 验证 AkShare 返回有效列表时能成功生成 `stock_list.csv`
- 验证 AkShare 失败时会切换到 TuShare
- 验证双源都失败时不会覆盖旧文件
- 验证代码标准化结果符合 `XXXXXX.SH/SZ/BJ`
- 验证去重、空值过滤和非法代码过滤

不在本次首版中引入真实联网集成测试，避免免费接口波动导致 CI 不稳定。

## 9. Risks

- AkShare 免费接口存在字段变动和访问不稳定风险，因此必须做严格字段校验
- TuShare 兜底依赖 token，某些环境下可能不可用
- 北交所、特殊证券、退市股票在不同源中的字段命名可能不完全一致，标准化逻辑要尽量保守
- 若未来要支持板块过滤、行业过滤、停牌过滤，应在后续单独设计，而不是塞进首版脚本

## 10. Acceptance Criteria

满足以下条件则视为完成：

- 存在可直接运行的 `scripts/update_history_sync_stock_list.py`
- 默认运行可生成或更新 `data/stock_list.csv`
- 默认策略为 `AkShare` 优先，`TuShare` 兜底
- 输出代码格式与现有项目兼容
- 双源失败时不会破坏旧股票池文件
- 有明确日志说明最终使用的数据源和记录数量
