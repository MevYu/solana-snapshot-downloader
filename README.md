# Solana Snapshot Downloader

[English](README.en.md)

自动寻找 Solana 快照节点、测速，下载完整快照和匹配的增量快照，并显示实时进度。

**Linux amd64 / arm64，Python 3.9+，仅用标准库，无需 pip 或其他下载工具。**

## 下载并运行

用 `python3 --version` 确认版本，然后获取源码并启动：

```bash
git clone https://github.com/MevYu/solana-snapshot-downloader.git
cd solana-snapshot-downloader
python3 snapshot_downloader.py --output /mnt/ledger/snapshots
```

将输出目录换成有写入权限、空间充足的目录；`--output` 也可简写为 `-o`。
不加参数时，默认下载 mainnet 快照到当前目录下的 `./snapshots`：

```bash
python3 snapshot_downloader.py
```

没有 Git？打开 [仓库](https://github.com/MevYu/solana-snapshot-downloader)，选择 **Code → Download ZIP**，
解压后进入项目目录运行同一命令。请保留整个项目，包括 `downloader/`，不能只下载入口脚本。

下载前请停止使用同一输出目录的其他下载或验证节点快照拉取任务。

## 会看到什么

程序依次显示搜索节点、测速、下载三个阶段。以下为**示意输出，数字不是实际下载记录**：

```text
[1/3] Searching nodes 120/800 | usable 20
[2/3] Testing speed 3/5 | 32.4 MB/s
[3/3] full        [====>.....] 45%  4.5/10.0 GiB   32.4 MB/s  ETA 03:02
[3/3] incremental [==>.......] 20%  200/1000 MiB   20.0 MB/s  ETA 00:42
```

终端中会动态刷新当前进度行；重定向时会定期输出纯文本。
下载中按 **Ctrl+C** 会保留部分数据；再次运行同一命令会在服务端支持且文件身份未变时尝试续传。
请保留下载目录中的文件和当前任务的隐藏状态文件 `.download-state.json`；它不会累积下载历史。

## 常用命令

只查看可用节点，不下载快照：

```bash
python3 snapshot_downloader.py --list
```

指定自己的节点（请替换示例地址）：

```bash
python3 snapshot_downloader.py --node http://your-node.example:8899
```

查看常用参数或全部高级参数：

```bash
python3 snapshot_downloader.py --help
python3 snapshot_downloader.py --help-all
```

## 更多说明

[高级指南](docs/advanced.md) 包含完整参数、JSON 输出、续传限制、文件与校验说明，以及构建和发布包用法。
下载发布包请查看 [Releases](https://github.com/MevYu/solana-snapshot-downloader/releases)；`.pyz` 仍需要 Python 3.9+。

[MIT License](LICENSE)。功能思路参考 [solana-snapshot-finder](https://github.com/coderigo/solana-snapshot-finder)，代码独立实现。
