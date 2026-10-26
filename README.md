# Solana Snapshot Downloader

[English](README.en.md)

使用 Python 标准库发现 Solana 快照节点、筛选候选节点并下载快照。默认下载完整快照（full snapshot）及与其基础 slot 匹配的增量快照（incremental snapshot）；支持测速、低速切换和符合条件时的断点续传。

运行目标为 **Linux amd64 / arm64，Python 3.9 或更新版本**。运行时不需要安装第三方 Python 包，也不依赖 `pip`、虚拟环境、`aria2` 或 `wget`。

## 下载源码并运行

先检查 Python 版本：

```bash
python3 --version
```

输出应为 Python 3.9 或更新版本。部分 Linux 系统已预装 Python；若没有，可使用系统包管理器安装，例如 Ubuntu/Debian 的 `sudo apt install python3` 或 Fedora 的 `sudo dnf install python3`，然后再次确认版本。

### 方式一：使用 Git

```bash
git clone https://github.com/MevYu/solana-snapshot-downloader.git
cd solana-snapshot-downloader
python3 snapshot_downloader.py --output /mnt/ledger/snapshots
```

将 `/mnt/ledger/snapshots` 换成有写入权限、空间充足的快照目录。首次使用也可以选择自己的目录：

```bash
python3 snapshot_downloader.py --output "$HOME/solana-snapshots"
```

### 方式二：从浏览器下载 ZIP

1. 打开 [GitHub 仓库](https://github.com/MevYu/solana-snapshot-downloader)，选择 **Code → Download ZIP**。
2. 解压整个压缩包，在终端进入解压后的项目目录。
3. 运行：

   ```bash
   python3 snapshot_downloader.py --output /mnt/ledger/snapshots
   ```

请保留整个源码目录，包括 `solana_snapshot_downloader/` 包；仅下载 `snapshot_downloader.py` 不能运行。

在项目目录下也可以使用模块入口：

```bash
python3 -m solana_snapshot_downloader --output /mnt/ledger/snapshots
python3 snapshot_downloader.py --help
```

下载前请停止使用同一输出目录的其他下载任务或验证节点快照拉取任务。使用自己有权限的目录即可，无需将目录权限改成 `777`。

## 常用操作

只发现、探测节点，不下载快照，也不创建快照输出目录：

```bash
python3 snapshot_downloader.py --list
python3 snapshot_downloader.py --list --json > candidates.json
```

`--list` 的候选表格或 JSON 写入标准输出，日志和下载进度写入标准错误，便于重定向结果。

选择网络，或仅下载完整快照：

```bash
python3 snapshot_downloader.py --network testnet --output "$HOME/testnet-snapshots"
python3 snapshot_downloader.py --full-only --max-slot-age 50000 --output /mnt/ledger/snapshots
```

`--full-only` 会按完整快照自身的 slot 检查新鲜度。它可能比最新增量快照旧，默认的 `2500` slot 上限可能筛掉候选节点；可按需要放宽，例如上面的 `50000`。`--max-slot-age 0` 会关闭该限制。

直接探测指定节点，跳过节点发现；请替换示例地址。程序仍通过所选网络的 RPC 获取当前 slot：

```bash
python3 snapshot_downloader.py \
  --node http://your-node.example:8899 \
  --node http://another-node.example:8899 \
  --output /mnt/ledger/snapshots
```

`--rpc` 可重复指定发现入口或备用 RPC，显式指定后会替代内置 RPC。`--network` 仅选择默认 RPC，不验证自定义 RPC 或节点所属的集群；请自行确保它们属于同一目标网络：

```bash
python3 snapshot_downloader.py \
  --network mainnet \
  --rpc https://your-rpc.example \
  --rpc https://backup-rpc.example \
  --output /mnt/ledger/snapshots
```

按下载速度筛选并在持续低速时重试；下面的 `10` 表示十进制 10 MB/s：

```bash
python3 snapshot_downloader.py \
  --min-speed 10 --speed-window 30 \
  --output /mnt/ledger/snapshots
```

默认对最多 5 个候选节点分别采样测速 2 秒，每个候选最多读取 16 MiB。`--speed-test-count 0` 可关闭采样测速。测速和实际下载速度会受节点负载、链路和磁盘影响，采样结果不保证后续吞吐量。

## 参数

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--output PATH` | `snapshots` | 快照输出目录，相对当前工作目录 |
| `--network mainnet\|testnet\|devnet` | `mainnet` | 选择默认 RPC 的网络；不校验自定义端点的集群 |
| `--rpc URL` | 所选网络的内置入口 | 发现入口或备用 RPC；可重复指定，替代内置 RPC |
| `--node HTTP_URL_OR_IP:PORT` | 自动发现 | 直接探测指定节点，跳过发现；可重复指定，仍需 RPC 获取当前 slot |
| `--workers N` | `32` | 并发探测节点数，范围为 `1`–`128` |
| `--timeout SECONDS` | `10` | 网络请求超时（秒） |
| `--max-slot-age N` | `2500` | 候选快照相对当前 slot 的最大落后量；`0` 关闭限制 |
| `--full-only` | 关闭 | 仅下载完整快照；按其 slot 检查新鲜度 |
| `--list` | 关闭 | 仅发现和探测，不下载快照、不创建输出目录 |
| `--json` | 关闭 | 与 `--list` 一起使用，输出 JSON |
| `--speed-test-count N` | `5` | 采样测速的候选数；`0` 关闭采样 |
| `--speed-test-seconds SECONDS` | `2` | 每个候选的采样时长；同时受 16 MiB 上限约束 |
| `--min-speed MBPS` | `0` | 最低速度，单位为 MB/s（1 MB = 1,000,000 字节）；`0` 关闭。作用于采样和持续下载 |
| `--speed-window SECONDS` | `30` | 持续下载的速度检测窗口 |
| `--retries N` | `3` | 每个文件首次尝试之外的额外重试次数；默认最多尝试 4 次 |
| `--retry-delay SECONDS` | `2` | 文件重试指数退避的初始秒数，上限 60 秒；扫描轮之间等待此秒数 |
| `--attempts N` | `3` | 发现、探测和下载的扫描轮数 |
| `--verbose` | 关闭 | 输出详细日志 |
| `--version` | — | 显示版本 |

## 下载文件与断点续传

程序按顺序下载完整快照和匹配的增量快照。下载中按 **Ctrl+C** 会以状态码 `130` 退出并保留已下载部分；再次执行同一命令会尝试续传。

续传要求服务端支持 HTTP Range，且源地址、归档和身份元数据没有变化。身份依据为强 ETag，或没有 ETag 时满足时间条件的 Last-Modified 与 Date。条件不满足时会重新下载，不会跨节点拼接已下载片段。HTTP 错误响应、错误网页、长度不足或身份不一致的下载不会被当作完成的快照文件。为检查传输是否完整，服务器需要提供有效的 Content-Length。

输出目录中可能包含：

| 文件 | 用途 |
| --- | --- |
| `snapshot-….tar.zst`、`incremental-snapshot-….tar.zst` 等 | 完成的快照归档，保留节点提供的文件名和压缩格式 |
| `<归档文件名>.part` | 尚未完成的下载 |
| `<归档文件名>.part.json` | 续传元数据 |
| `<归档文件名>.complete.json` | 本程序记录的完成凭据，用于后续复用检查 |
| `.snapshot-downloader.lock` | 输出目录锁；程序退出后保留空锁文件是正常现象 |

程序可通过完成凭据复用自己从相同 URL 下载的完整文件。不同节点即使提供同名归档，也不会自动复用该文件或拼接片段。对于没有匹配凭据的已有归档，程序会拒绝覆盖；遇到这种情况，请指定原节点继续，或改用单独的空输出目录。不要在程序运行时删除锁文件，也不要让其他程序修改正在下载的文件或元数据。

本程序不清理旧快照、不解压归档，也不停止或重启 Solana 验证节点。

## 校验范围

完成检查针对 HTTP 传输长度和资源身份，**不构成 Solana 状态的密码学验证**。快照文件名中的 hash 不是归档文件的 SHA-256 校验和。请按自己的信任要求选择节点；最终快照加载和状态校验由 Solana 验证节点执行。

## 发布文件与开发

[Releases](https://github.com/MevYu/solana-snapshot-downloader/releases) 页面列出实际已发布的版本。源码可按上面的方式直接运行；发布的 `.pyz`（如有）是 Python zipapp，仍需要 Python 3.9 或更新版本，不是独立可执行二进制文件。

版本发布后，可在浏览器中从 Releases 下载同一版本的 `.pyz`、源码 `.tar.gz` 和 `SHA256SUMS`，将三者放在同一目录，再校验并运行。以下以 `0.1.1` 文件名为例，可用版本以页面为准：

```bash
sha256sum -c SHA256SUMS
python3 solana-snapshot-downloader-0.1.1.pyz --output /mnt/ledger/snapshots
```

在源码目录运行测试和构建发布文件：

```bash
python3 -m unittest discover -s tests -v
python3 tools/build_release.py
```

版本 `0.1.1` 的构建产物为 `dist/solana-snapshot-downloader-0.1.1.pyz`、`dist/solana-snapshot-downloader-0.1.1-source.tar.gz` 和 `dist/SHA256SUMS`。zipapp 通用于 amd64、arm64，无需分别构建。`SHA256SUMS` 用于检查发布包的文件一致性，不是 Solana 快照状态证明。构建后可运行：

```bash
cd dist
sha256sum -c SHA256SUMS
python3 solana-snapshot-downloader-0.1.1.pyz --output /mnt/ledger/snapshots
```

## 许可证与致谢

本项目采用 [MIT License](LICENSE)。功能思路参考 [solana-snapshot-finder](https://github.com/coderigo/solana-snapshot-finder)；当前代码为独立实现，未复制该项目代码。
