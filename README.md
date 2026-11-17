# Codex Provider Continuity

修复本地 Codex 切换 provider 后旧聊天不可见、无法恢复的问题，并提供 CC Switch 模板适配。

通过统一当前配置、SQLite 线程索引和 rollout 会话头部的 provider ID，让支持该配置的客户端可以找到原有聊天。提供 Windows 双击菜单、只读预览、备份与回滚。

> 社区工具，与 OpenAI / Codex / CC Switch 官方无关联。恢复本地会话元数据，不保证所有服务间的模型、工具、加密推理内容和认证兼容；尤其是 ChatGPT 订阅 / OAuth 切换尚未做端到端验证。

## 下载

- 普通用户：[下载最新发布包](https://github.com/lcq666666/codex-provider-continuity/releases/latest)，解压完整 ZIP 后运行 `START.cmd`。
- 开发者：`git clone https://github.com/lcq666666/codex-provider-continuity.git`。
- Python 推荐 3.14；不需要管理员权限，无内置运行时，不是免安装 EXE。

## 问题与处理方式

历史正文可能还在磁盘上，但当前 `model_provider` 与 SQLite / rollout 中保存的 provider 不同，客户端可能过滤这些聊天或拒绝恢复。CC Switch 又会用保存的完整配置覆盖当前 `config.toml`，只修改当前文件会被下一次切换覆盖。

工具用稳定短 ID（默认 `shared`）统一这三处，并修改 CC Switch 已有的 Codex 模板，让模板继续保留各自的端点、模型和凭据。另可修复数据库指向 `.jsonl`、实际文件为同名 `.jsonl.zst` 的路径漂移。不会为未索引文件重建聊天，也不能恢复已删除的正文。


将整个压缩包发给其他用户即可，不要附带你自己的 `.codex` / `.cc-switch` 数据、配置、密钥或备份。工具使用接收者自己的用户目录；没有硬编码作者的用户名、接口地址或凭据。

## Windows 用户：首次使用

1. 安装 Python 3.14：https://www.python.org/downloads/ 。工具包没有内置 Python，也不是免安装 EXE。
2. 解压整个工具包到任意可写目录，不能只单独复制某一个脚本。
3. 双击 `START.cmd`，选择 **1：只读诊断**，查看扫描数量、provider 不一致和文件缺失数量。
4. 完全退出 Codex Desktop、Codex CLI/app-server，以及 CC Switch（包括托盘进程）。在独立窗口再次双击 `START.cmd`，选择 **2：首次配置与历史修复**。
5. 查看预览，输入 `APPLY`。工具会备份并统一当前 Codex 配置、已索引历史元数据及已安装的 CC Switch Codex 模板，默认身份为 `shared`。
6. 出现 `VERIFIED` / `DONE` 后重新打开 CC Switch 和 Codex。检查侧边栏，打开一条旧聊天确认内容，再按自己的意愿发送消息检查服务端兼容性。

未安装 CC Switch 的用户也能选择 2，只处理 Codex。

## 日常切换与新增 provider

- 已统一的 CC Switch 模板之间切换：通常不需要重新迁移历史，端点、模型和认证仍各用各的。
- 新增或导入 Codex provider 后：退出 CC Switch，运行 `START.cmd`，选择 **3：更新 CC Switch 模板**，预览后输入 `APPLY`，然后重新打开 CC Switch。
- 新 provider 已经被启用并生成了其他 provider ID 的聊天：先完全退出两个应用，再选 **2**，将这些聊天也纳入迁移。
- 这是按需运行的工具，**没有后台监听**；CC Switch 以后新增、编辑或恢复模板，仍可能改变 provider ID。不会自动拦截它的每次写入。

## 工具具体修改什么

`shared` 是 Codex 配置中的 provider ID，CC Switch 自己的 provider 记录 ID、名称和排序保留。工具修改当前 `config.toml` 的 provider 选择与相应 provider 表名，保留连接字段和其他 TOML 配置。它同时更新 SQLite `threads.model_provider` 与每个 rollout 第一行的 provider，修复可唯一验证的同名 `.jsonl` / `.jsonl.zst` 路径；不更改线程 ID、正文、标题、置顶和归档标志。

CC Switch 适配仅修改 `providers` 表中 `app_type = codex` 的 `settings_config.config`。`auth.json`、模板里的认证对象、Claude/Gemini 等其他应用记录保持原值。备份留在用户本机；包内不含个人数据。

## 支持范围与已知限制

- 主要面向 Windows 本地 Codex 与当前可识别的 CC Switch SQLite 数据结构。检测到 schema 不匹配、provider 重名冲突、活动 profile、历史文件缺失或 header 空间不足时停止，请保留错误信息进行诊断。
- SQLite 可从 Codex home 根目录或 `sqlite/` 子目录发现；若存在多个候选，需用 `--db` 明确指定。不会猜测哪个数据库应当覆盖另一个。
- 只处理数据库已经索引的本地聊天。不能恢复已经删除的正文、云端聊天或自动同步其他电脑的会话。
- 原生未压缩 `.jsonl` 和 Zstandard `.jsonl.zst` 均支持。Python 3.11–3.13 需自行执行 `python -m pip install zstandard`；Python 3.14 无需此依赖。
- 官方 OpenAI API 的内置配置可转成自定义 provider，并明确指向 `https://api.openai.com/v1`。**ChatGPT 订阅/OAuth、不同 Codex 版本对自定义 provider 的认证及桌面功能支持，尚未做端到端验证**；保留登录备份并实际验证。统一 ID 不能保证不同服务的模型、上下文长度、工具或加密推理内容都兼容。
- 工具不自动测试外部 API，不会自动登录、升级程序、改代理或安装常驻服务。
- Codex 历史迁移和 CC Switch 模板更新分别备份、分别提交，不是跨两个应用的一次原子事务。任何步骤报错都不要假定全部成功，按输出检查完成阶段。进程检测是辅助检查，仍应完全退出相关应用。

## 命令行与自定义路径

在解压目录中执行。以下命令默认均只预览；添加 `--apply` 才写入。

```sh
python scripts/portable.py --action doctor
python scripts/portable.py --action setup
python scripts/portable.py --action setup --apply
python scripts/portable.py --action cc-switch --apply
```

Windows 有多个 Python 版本时将 `python` 换成 `py -3.14`。路径可通过参数或环境变量指定，不需要编辑脚本：

```sh
python scripts/portable.py --action setup --home "/path/to/codex-home" --db "/path/to/state_5.sqlite" --cc-db "/path/to/cc-switch.db" --target shared
```

支持 `CODEX_HOME` 和 `CCSWITCH_HOME`。默认分别为当前用户的 `~/.codex` 与 `~/.cc-switch`。换到别人的电脑时无需继承你的环境变量。

`scripts/Repair-CodexProvider.ps1` 和 `scripts/CCSwitch-StableProvider.ps1` 是兼容入口，也会使用分发版的运行时探测和修复流程。

## 备份和回滚

- 历史备份：`<CODEX_HOME>/backups/provider-migration-*/manifest.json`，附一致的 SQLite 快照、rollout 原件和迁移暂存件。
- 配置备份：`<CODEX_HOME>/config.toml.before-stable-*`。
- CC Switch 备份：`<CCSWITCH_HOME>/cc-switch.db.before-stable-provider-*.bak`，使用 SQLite backup API，包含已提交的 WAL 内容。

完全退出应用后，可按具体清单恢复历史的 provider 字段、路径及 rollout：

```sh
python scripts/provider_history.py --home "/path/to/codex-home" --restore "/path/to/manifest.json" --apply --offline
```

若迁移后又新增了对话，恢复会拒绝覆盖新内容。配置可从对应配置备份还原；CC Switch 数据库备份应使用 SQLite 工具按需恢复，避免覆盖之后新建的 provider。历史恢复命令不自动恢复配置或 CC Switch 数据库。

## 验证情况

本分发版通过 27 项自动化测试：包括其他用户名/空格路径、单引号/带引号 TOML 段、默认 OpenAI 配置、压缩正文保持、路径修复后回滚、同 ID 的其他应用数据隔离、凭据保持、WAL 备份和运行中应用写入阻止。Windows PowerShell 入口已做只读试运行。尚未在其他用户真实设备上验证，也未进行官方 OAuth 与各家服务的端到端切换测试。

```sh
python -m unittest discover -s tests -v
```

## 排错

| 提示 / 情况 | 处理 |
| --- | --- |
| 找不到 Python | 安装 Python 3.14，然后重新运行 `START.cmd`。 |
| Close these apps / 文件被占用 | 退出 Codex 与 CC Switch，包括托盘和后台 CLI/app-server，再运行。不要只最小化窗口。 |
| 多个 state 数据库 | 先确认实际 `CODEX_HOME`，使用 `--db` 明确指定，不要删除其他数据库。 |
| provider 冲突 / profile | 暂停应用，检查配置及有效启动 profile；脚本不会猜测该覆盖哪个定义。 |
| header 空间不足 | 停止并保留原数据；较长 provider ID 无法在保持正文偏移时写入。可审阅后选择更短、配置一致的 ID。 |
| 修改完成但客户端没变化 | 重启应用，再运行只读诊断；确认没有被 CC Switch 旧模板、profile 或启动参数覆盖。 |
| 旧聊天能打开但发消息失败 | 检查当前 provider 的认证、模型和协议支持，元数据迁移并不验证外部服务。 |

`--offline` 是执行者声明已停止写入，并非脚本替你关闭应用。默认菜单会辅助检查进程。不要用其他正在运行的 Codex 对话直接调用底层写入命令。

## 项目结构

```text
START.cmd / Start.ps1          Windows 菜单与 Python 运行时探测
scripts/portable.py           统一诊断 / 首次修复 / CC Switch 入口
scripts/provider_history.py   本地历史迁移、路径修复和清单恢复
scripts/ccswitch_stable.py    CC Switch Codex 模板规范化
tests/                       使用合成数据的自动化测试
SKILL.md                     可选的 Codex skill 操作说明
```

## 反馈与贡献

请在 Issues 中提供操作系统、Python / Codex / CC Switch 版本和已脱敏的错误文本。不要上传 `auth.json`、完整 `config.toml`、数据库、rollout、备份清单或访问令牌。提交修复前运行上述测试；GitHub Actions 在 Windows 与 Linux 的 Python 3.14 上执行合成数据测试。

## 许可证

MIT，见 [LICENSE](LICENSE)。
