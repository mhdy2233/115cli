# 115cli

[![PyPI version](https://img.shields.io/pypi/v/115cli.svg)](https://pypi.org/project/115cli/)
[![test](https://github.com/Xavier-Lam/115cli/actions/workflows/test.yml/badge.svg)](https://github.com/Xavier-Lam/115cli/actions/workflows/test.yml)
[![codecov](https://codecov.io/gh/Xavier-Lam/115cli/branch/master/graph/badge.svg)](https://codecov.io/gh/Xavier-Lam/115cli)

**115cli** 是一个非官方的 [115.com](https://115.com) 网盘命令行工具和 *Python* 库, 提供常用的文件操作命令行接口, 同时提供一个高级API封装供 *Python* 项目使用.

在使用本项目前, 请仔细阅读[免责声明](#免责声明).

## 安装

推荐使用 pip 安装:

```bash
pip install 115cli
```

## 快速上手(CLI)

先用 `115cli login` [登录](#认证) (目前只支持从浏览器复制的 cookie 登录), 认证成功后就可以用 `115cli` 命令操作你的 115 云盘了.

常见示例:

```bash
# 使用 cookie 登录
115cli login cookie "UID=xxx; CID=xxx; SEID=xxx; KID=xxx"

# 账户信息
115cli account

# 目录
115cli ls /
115cli ls /path/to/dir -l
115cli ls --id 1234567
# 创建时间倒叙
115cli ls -l --sort created --desc

# 文件操作
115cli mkdir /new-folder
115cli cp /src/file.txt /dst/
115cli mv /old/path /new/path
115cli rename /old/path/file.txt new-file.txt
115cli rm /path/to/file
115cli rm -r /path/to/dir
115cli find /search/path keyword

# 查看文件信息和获取下载地址
115cli stat /path/to/file
115cli id 1234567
115cli url /path/to/file
115cli url --format aria2c /path/to/file

# 下载
115cli fetch /path/to/file.mp4
115cli fetch /path/to/file.mp4 -o /local/save/path.mp4
115cli fetch --id 1234567 -o /local/save/path.mp4
# 递归下载文件夹, 并进行完整性校验
115cli fetch /path/to/dir/ -o /local/save/dir/ --check-integrity
# 同时下载最多 4 个文件
115cli fetch /path/to/dir/ -o /local/save/dir/ -j 4 --check-integrity
# 本地同路径文件已存在就跳过（不比较大小和内容）
115cli fetch /path/to/dir/ -o /local/save/dir/ -T -j 4 --dedup-by-name
# 下载文件黑白名单
115cli fetch /path/to/dir/ -o /local/save/dir/ --include "**/*.mkv" --include "**/*.mp4" --exclude "secret/*"

# 上传(支持秒传)
115cli upload /local/file.txt /remote/dir/file.txt
# 仅秒传
115cli upload --instant-only /local/file.txt /remote/dir/file.txt
# 文件夹上传
115cli upload /local/folder/ /remote/dir/
# 文件黑白名单
115cli upload /local/folder/ /remote/dir/ --include "**/*.mkv" --include "**/*.mp4" --exclude "secret/*"
# 多文件并发上传与自定义分片大小
115cli upload /local/folder/ /remote/dir/ -j 4 --part-size 32M
# 获取一次远端目录树，仅按目标相对路径同名去重（忽略大小和内容差异）
115cli upload /local/folder/ /remote/dir/ -T -j 4 --dedup-by-name
# 开启 Debug 模式打印详细上传/秒传/OSS链路日志
115cli upload /local/folder/ /remote/dir/ --debug

# 离线下载
115cli download quota
115cli download list
115cli download list --filter completed
115cli download add "https://example.com/file.mp4"
115cli download delete <info_hash>
115cli download status <info_hash>
115cli download retry <info_hash>
115cli download clear --filter failed

# 文件分享
115cli share info https://115.com/s/1234567 -p 6666
115cli share list https://115.com/s/1234567?password=6666
115cli share list https://115.com/s/1234567?password=6666 /dir/
115cli share stat https://115.com/s/1234567?password=6666 /path/to/file.txt
115cli share save https://115.com/s/1234567?password=6666 --dest /my/dir/
115cli share save https://115.com/s/1234567?password=6666 /path/in/share/ --dest /my/dir/ --include *.mp4 --include *.mkv --exclude *.txt

# 导出目录树
115cli export /path/to/dir -o ./tree.txt
115cli export --id 1234567 -o ./tree.txt

# 串流
115cli stream /path/to/video.mp4
# 转码(使视频可用于串流)
115cli transcode /path/to/video.mp4
```

> 注意: 某些创建云下载任务的操作可能会触发图形验证码, 目前客户端不支持处理验证码.

上传和下载的 `-j N` 控制同时传输的文件数，默认均为 1；可在配置中设置
`upload.max_workers` 和 `download.max_workers`。默认同一路径只有大小和 SHA-1
都一致才跳过；上传遇到同名不同内容、文件与目录冲突会报错。
下载会跳过已校验相同的本地文件，不同内容仍在下载成功后替换。
进度和成功、跳过、失败统计输出到 stderr，终端显示总览及活跃文件进度；
重定向时只保留摘要。`--silent` 关闭进度与摘要，上传 `--format json` 的 stdout 保持纯 JSON。

`upload --dedup-by-name` 对目录只获取一次服务端导出树，在内存中按完整相对路径
跳过同名项，不再逐子目录查询元信息，也不比较大小或 SHA-1；不同子目录的同名文件
不会混淆。只有待上传文件所需的父目录才按需解析或创建。导出包含提交、轮询及下载文本，
因此不是零 API 请求，小目录也不一定更省请求；服务端还会在目标目录生成导出文本文件。
该模式不会识别同名文件的内容变化，导出文本也不能可靠区分叶节点文件和空目录。
默认的大小/SHA-1 模式保留文件类型和内容检查。

`fetch --dedup-by-name` 同样可用：本地目标文件已存在就跳过，不读取大小或计算 SHA-1；
远端文件信息和目录列表查询仍然需要。`--check-integrity` 只校验新下载文件，不校验已跳过文件。

完整目录扫描（默认上传去重、递归下载和本地目录树导出）每页最多请求 1150 项，
普通 `ls` 的取页容量仍为 200；服务端若返回更小容量，后续分页采用返回值。
搜索和分享分页在一次操作内复用目录 ID，单文件上传复用目标预检和父目录 ID，避免重复查询。

### 认证(Cookie)

本项目目前只支持通过浏览器拿到的 cookie 登录.登录时需要提供 `UID`, `CID`, `SEID` 和 `KID` 四个 cookie 值.

```bash
115cli login cookie "UID=xxx; CID=xxx; SEID=xxx; KID=xxx"
```

## Client API

本项目提供一个高级的 *Python* API 客户端,你可以在自己的项目中直接使用:

```python
from cli115.auth import CookieAuth
from cli115.client import create_client

auth = CookieAuth(
	uid="xxx",
	cid="xxx",
	seid="xxx",
	kid="xxx"
)
client = create_client(auth)

# 列目录
entries = client.file.list("/")
for entry in entries:
	print(entry.name, entry.id)

# 文件信息
info = client.file.stat("/path/to/file.txt")
print(info.name, info.size, info.sha1)

# 获取下载信息
dl = client.file.url("/path/to/file.txt")
print(dl.url)

# 下载
with client.file.open("/path/to/file.txt") as rf:
    data = rf.read(1024)  # 仅下载前1024字节

# 上传
result = client.file.upload("/remote/dir/", "/local/file.txt")

# 添加云端下载任务
client.download.add_url("https://example.com/file.mp4")
tasks = client.download.list()
```

## 未来计划

项目仍在早期,计划包括但不限于:

- **二维码登录** - 实现基于二维码的认证, 方便登录而无需手动提取 cookie.

## 致谢
[**p115client**](https://github.com/ChenyangGao/p115client) 提供了一些非web端API实现, 本项目的非web端接口来源于该项目.

* ECDH-AES 加密/解密文件内容和元数据.
* 通过计算文件哈希并与对应API交互实现秒传支持.
* 大文件的分片上传支持.

## 免责声明

这是一个非官方的工具, 与 *115.com* 或其母公司无任何关联. 使用风险自负, 作者不对账号被封, 数据丢失等后果负责.

你可能会遇到*阿里云 WAF* 的封禁 (机制和后果尚不明确), 被封禁后可能需要等待一段时间再重试, 网页端也会受到影响.
