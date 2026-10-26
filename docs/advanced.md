# 高级指南

[返回快速开始](../README.md) · [English](advanced.en.md)

运行环境为 Linux amd64 / arm64、Python 3.9+，不需要第三方 Python 包、虚拟环境、`aria2` 或 `wget`。
若缺少 Python，可使用系统包管理器安装，例如 Ubuntu/Debian 的 `sudo apt install python3` 或 Fedora 的 `sudo dnf install python3`，再运行 `python3 --version` 确认版本。
在源码目录也可使用 `python3 -m downloader`，参数与 `python3 snapshot_downloader.py` 相同。

## 参数

`--help` 显示常用参数，`--help-all` 显示完整参数。原有高级参数仍可直接使用。

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `-o PATH`、`--output PATH` | `snapshots` | 快照目录，相对当前工作目录 |
| `--network mainnet\|testnet\|devnet` | `mainnet` | 选择默认 RPC 的网络，不校验自定义端点的集群 |
| `--rpc URL` | 所选网络的内置入口 | 发现入口或备用 RPC；可重复指定，替代内置 RPC |
| `--node HTTP_URL_OR_IP:PORT` | 自动发现 | 可重复；直接探测指定节点，跳过发现，仍需 RPC 获取当前 slot |
| `--workers N` | `32` | 并发探测节点数，范围 `1`–`128` |
| `--timeout SECONDS` | `10` | 网络请求超时（秒） |
| `--max-slot-age N` | `2500` | 候选快照相对当前 slot 的最大落后量；`0` 关闭限制 |
| `--full-only` | 关闭 | 只下载完整快照，按其 slot 检查新鲜度 |
| `--list` | 关闭 | 只发现和探测，不下载快照、不创建输出目录 |
| `--json` | 关闭 | 与 `--list` 一起使用，输出 JSON |
| `--speed-test-count N` | `5` | 采样测速的候选数；`0` 关闭采样 |
| `--speed-test-seconds SECONDS` | `2` | 每个候选的测速时长，同时受 16 MiB 上限约束 |
| `--min-speed MBPS` | `0` | 采样和持续下载的最低速度；MB/s 为十进制，1 MB = 1,000,000 字节；`0` 关闭 |
| `--speed-window SECONDS` | `30` | 持续下载的速度检测窗口 |
| `--retries N` | `3` | 首次尝试之外的额外文件重试次数；默认最多尝试 4 次 |
| `--retry-delay SECONDS` | `2` | 文件重试指数退避的初始秒数，上限 60 秒；扫描轮之间等待此秒数 |
| `--attempts N` | `3` | 发现、探测和下载的扫描轮数 |
| `--verbose` | 关闭 | 详细日志及诊断信息 |
| `-h`、`--help` | — | 显示常用参数 |
| `--help-all` | — | 显示全部参数 |
| `--version` | — | 显示版本 |

## 网络、完整快照和速度

`--network` 仅选择默认 RPC。自定义 RPC 和节点的所属集群不会被验证，请自行确保它们属于同一目标网络。示例地址需要替换：

```bash
python3 snapshot_downloader.py --network testnet -o "$HOME/testnet-snapshots"
python3 snapshot_downloader.py --rpc https://your-rpc.example --rpc https://backup-rpc.example
```

默认先下载完整快照；有匹配的增量快照时，再下载增量快照。`--full-only` 按完整快照自身的 slot 检查新鲜度，它可能比最新增量快照旧；默认 `2500` slot 上限可能筛掉候选，可按需要放宽：

```bash
python3 snapshot_downloader.py --full-only --max-slot-age 50000 -o /mnt/ledger/snapshots
```

默认对最多 5 个候选分别采样测速 2 秒，每个最多读取 16 MiB；`--speed-test-count 0` 关闭采样。需要筛掉低速源时，例如要求 10 MB/s：

```bash
python3 snapshot_downloader.py --min-speed 10 --speed-window 30 -o /mnt/ledger/snapshots
```

节点负载、链路和磁盘会影响实际速度；采样结果不保证后续吞吐量。

## 进度与 JSON 输出

进度和日志写入 **stderr**。连接终端（TTY）时动态刷新当前行，重定向时定期输出纯文本。
搜索阶段显示已探测节点数和可用数，测速阶段显示候选进度和速度，下载阶段显示完整/增量快照的进度条、百分比、大小、速度及 ETA。

`--list` 的候选表格或 `--list --json` 的 JSON 写入 **stdout**，不会被进度文本混入：

```bash
python3 snapshot_downloader.py --list --json > candidates.json
```

此命令不下载快照数据，也不创建快照输出目录；重定向文件 `candidates.json` 由 shell 创建。

## 文件与恢复限制

选择有写入权限、空间充足的目录，不必使用 `chmod 777`。启动前停止使用同一目录的其他下载或验证节点快照拉取任务。
按 **Ctrl+C** 会以状态码 `130` 退出并保留部分数据，再执行同一命令会尝试续传。

续传要求 HTTP Range 支持，以及未变的源地址、归档和身份元数据。身份依据为强 ETag，或没有 ETag 时满足时间条件的 Last-Modified 与 Date。条件不满足时重新下载，不跨节点拼接片段。
服务端需提供有效的 Content-Length。HTTP 错误响应、错误网页、长度不足或身份不一致的文件不会成为完成的快照归档。

| 文件 | 用途 |
| --- | --- |
| `snapshot-….tar.zst`、`incremental-snapshot-….tar.zst` 等 | 完成归档，保留源文件名和压缩格式 |
| `<归档文件名>.part` | 部分下载 |
| `.download-state.json` | 当前任务状态，最多记录 1 个完整快照和 1 个增量快照，不保存下载历史 |
| `.snapshot-downloader.lock` | 目录锁；退出后保留空文件正常 |

下载中只有归档的 `.part` 文件，完成后变为正式归档。请保留 `.download-state.json`，续传和完成文件复用依赖其中的记录。
切换新快照会替换旧状态记录；程序不会自动删除已完成归档，也不会扫描用户目录来建立历史记录。
兼容旧版逐文件的 `.part.json` / `.complete.json`：用到对应归档时，先将记录写入统一状态并持久化，再删除旧状态文件，保留原有续传信息，无需手工清理。

程序通过当前任务中匹配的完成记录复用自己从相同 URL 下载的完整文件，不自动复用其他节点的同名归档。已有归档没有匹配记录时会拒绝覆盖，请改用单独的空输出目录。
不要在运行时删除锁文件，也不要让其他程序修改下载文件或元数据。本程序不清理旧快照、不解压归档、不停止或重启验证节点。

## 校验范围

完成检查针对 HTTP 传输长度和资源身份，**不构成 Solana 状态的密码学验证**。文件名中的 hash 不是归档的 SHA-256 校验和。
请按信任要求选择节点；最终快照加载和状态校验由 Solana 验证节点执行。

## 发布包与开发

在 [Releases](https://github.com/MevYu/solana-snapshot-downloader/releases) 查看实际已发布版本。`.pyz` 是通用于 amd64 / arm64 的 Python zipapp，仍需 Python 3.9+。
从浏览器下载同一版本的 `.pyz`、源码 `.tar.gz` 和 `SHA256SUMS` 到同一目录，校验后运行。
下列 `<VERSION>` 是占位符，请替换为下载文件的实际版本号：

```bash
sha256sum -c SHA256SUMS
python3 "solana-snapshot-downloader-<VERSION>.pyz" --output /mnt/ledger/snapshots
```

在源码目录测试和构建，无需安装依赖：

```bash
python3 -m unittest discover -s tests -v
python3 tools/build_release.py
```

输出为 `dist/solana-snapshot-downloader-<VERSION>.pyz`、`dist/solana-snapshot-downloader-<VERSION>-source.tar.gz` 和 `dist/SHA256SUMS`。
可进入 `dist` 后执行上面的校验和运行命令。`SHA256SUMS` 检查发布文件一致性，不是快照状态证明。

本项目采用 [MIT License](../LICENSE)。功能思路参考 [solana-snapshot-finder](https://github.com/coderigo/solana-snapshot-finder)；当前代码为独立实现，未复制该项目代码。
