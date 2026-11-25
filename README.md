# dupfind

Windows 单盘重复文件查找与清理工具。先看清楚，再动手。

它不会一上来就全盘哈希。先用「同大小 + 文件名包含关系」筛出可疑副本，再对这批候选做头尾采样和全量哈希确认，最后把确认重复的文件**移入隔离区**而不是直接删除——随时可以还原。

```
遍历 → 排除过滤 → 按 size 分桶 → 文件名词元包含判定 → 头尾采样 → 全量哈希 → 确认分组
```

## 为什么这样设计

磁盘里的重复文件，绝大多数是「另存为」「复制备份」「微信/QQ 自动重命名」的产物，它们有个共同特征：**大小完全一致，文件名之间存在包含关系**（`报告.pdf` / `报告-副本.pdf`）。利用这一点，可以把需要哈希的文件从几十万个降到几百个。

三条原则贯穿始终：

1. **默认只读** — 扫描阶段不修改任何文件
2. **误报优先于漏报** — 宁可少报，不可误删
3. **过程透明** — 跳过、拒绝访问、疑似、确认，全部计数并可见

## 安装

需要 Python 3.11+，仅支持 Windows。

```powershell
pip install -e .
```

可选依赖：

```powershell
pip install -e ".[blake3,web]"
```

- `blake3` — 更快的哈希算法（缺失时自动退回 SHA-256）
- `web` — 本地可视化界面

## 快速开始

```powershell
dupfind scan E:\ --out report.json
```

看摘要，确认没问题后先 dry-run 一遍：

```powershell
dupfind apply report.json --trash E:\_dupfind_trash --keep shortest --yes
```

确认输出符合预期，再去掉 dry-run：

```powershell
dupfind apply report.json --trash E:\_dupfind_trash --keep shortest --yes --no-dry-run
```

想反悔：

```powershell
dupfind restore E:\_dupfind_trash --no-dry-run
```

确认不再需要，真正释放空间：

```powershell
dupfind empty E:\_dupfind_trash --no-dry-run
```

## 命令

### `scan <root>`

扫描单个卷，产出 JSON 报告。只读。

| 参数 | 默认 | 说明 |
|---|---|---|
| `--out` | `dupfind-report.json` | 报告输出路径 |
| `--min-size` | `1KB` | 小于该大小不参与（`0` 表示不限） |
| `--max-depth` | 不限 | 递归深度 |
| `--exclude-add` | 无 | 追加排除 glob，可多次 |
| `--no-exclude` | false | 关闭全部排除规则 |
| `--show-excluded` | false | 逐条打印被跳过的路径 |
| `--exclude-dry-run` | false | 只展示将被跳过的内容，不执行扫描 |
| `--sample-size` | `4KB` | 头尾采样大小 |
| `--no-hash` | false | 只做名字+大小筛选，结论为 `suspect` |
| `--content-only` | false | 忽略名字规则，所有同大小文件进入哈希 |
| `--threads` | 4 | 哈希并发数 |
| `--cache` / `--no-cache` | `.dupfind-cache` | 哈希缓存（path+size+mtime） |
| `--json` | false | 只输出 JSON 摘要到 stdout |
| `--config` | 工作目录 `dupfind.toml` | 配置文件路径 |

### `apply <report>`

基于报告批量处置。**默认 dry-run**。

| 参数 | 默认 | 说明 |
|---|---|---|
| `--trash` | `<盘符>:\_dupfind_trash` | 隔离区目录 |
| `--keep` | `ask` | `shortest` / `newest` / `ask` |
| `--dry-run` | **true** | 只打印计划动作 |
| `--yes` | false | 跳过逐页确认 |
| `--page` | 10 | 每页分组数 |
| `--min-savings` | `0` | 只处理可节省空间达阈值的组 |
| `--no-verify-content` | false | 跳过移动前的头尾采样复核 |

交互模式下按页展示，输入单字符指令：`k` 本页全部处理 / `s` 本页跳过 / `1-9` 逐组选择 / `a` 全部剩余 / `p` 上一页 / `q` 退出。

### `restore <manifest|目录>`

把隔离区里的文件还原到原位置。传目录时处理其中全部 manifest。

| 参数 | 默认 | 说明 |
|---|---|---|
| `--dry-run` | true | 只打印计划动作 |
| `--on-conflict` | `skip` | 原位置已有文件时：`skip` / `rename` / `overwrite` |

还原是幂等的，重复执行不会出错。apply 期间每个动作先写 journal 再移动，所以即使进程被强杀，也能从 journal 完整还原——包括「移动已完成、状态还停在 pending」这种最窄的时间窗口。传目录时也会一并发现只有 journal、快照尚未落盘的 manifest。

### `empty <trash>`

清空隔离区，真正释放磁盘空间。

| 参数 | 默认 | 说明 |
|---|---|---|
| `--dry-run` | true | 只列出将删除的文件 |
| `--purge-manifests` | false | 连 manifest 一并删除（默认保留便于追溯） |
| `--yes` | false | 跳过 `DELETE` 确认 |

拒绝清空盘符根目录与系统目录；目录里没有 `manifest-*.json` 时也拒绝——那不是隔离区，多半是路径写错了。payload 删除失败时会保留全部 manifest，那些文件还能靠它还原。

## 报告格式

```json
{
  "version": 1,
  "scan": {
    "root": "E:\\",
    "startedAt": "2026-11-06T10:00:00+08:00",
    "durationMs": 227136,
    "filesScanned": 208075,
    "excludedDirs": 72,
    "excludedFiles": 25064,
    "accessDenied": 0,
    "errors": 0,
    "partial": false
  },
  "groups": [
    {
      "id": 1,
      "status": "confirmed",
      "size": 812565376,
      "savingsBytes": 812565376,
      "files": [
        {
          "path": "E:\\docs\\supermap.zip",
          "normalizedName": "supermap-idesktop-11.0.2-win64-bin",
          "mtime": "2023-11-01T17:13:30+08:00",
          "fileId": "0xD4A3662C000600000001D9D9",
          "shared": false,
          "unstable": false,
          "sizeOnDisk": 812565376
        }
      ]
    }
  ]
}
```

`status` 有三种：

- `confirmed` — 采样一致且全量哈希相同
- `suspect` — 仅通过名字+大小筛选（`--no-hash` 模式，或扫描被中断）
- `shared` — 硬链接，不占额外空间，无需处置

`groups` 按 `savingsBytes` 降序。

## 排除规则

三层，均可配置：

1. **目录黑名单** — `<盘符>:\Windows`、`Program Files`、`ProgramData`、`$Recycle.Bin` 等；任意层级的 `.git`、`node_modules`、`.venv`、`__pycache__`、`_dupfind_trash` 等
2. **文件级过滤** — `.dll .exe .sys .msi .cab .mui .pdb .tmp .log .etl .dmp`；隐藏+系统属性同时具备的文件；`pagefile.sys` 等系统根文件
3. **用户自定义** — `dupfind.toml` 或 `--exclude-add <glob>`

```toml
[exclude]
systemDirs = ["Windows", "Program Files"]
anyLevelDirs = [".git", "node_modules"]
extensions = [".dll", ".exe"]
rootFiles = ["pagefile.sys"]
globs = ["*.bak"]

[files]
copyMarkers = ["副本", "复制", "copy", "_1"]

[scan]
minSize = "1KB"
sampleSize = "4KB"
threads = 4

[apply]
page = 10
keep = "ask"
```

## 安全机制

**扫描阶段**不写任何文件。**处置阶段**有以下保护：

1. **隔离而非删除** — 文件按 `<trash>/<盘符>/<相对路径>` 镜像存放，manifest 记录每个动作。隔离区本身被默认排除规则跳过，且**隔离区里的副本永不被选作保留项**——否则「活文件被移进隔离区、保留项又在隔离区」，随后 `empty` 会把唯一的数据删光
2. **系统目录保护** — `C:\Windows`、`Program Files`、`ProgramData` 及系统根文件永不移动，优先级高于 `--keep` 策略
3. **内容复核** — 移动前对「待删 vs 保留」各做一次头尾 4KB 采样比对，挡住扫描后被改动的情况
4. **路径校验** — 拒绝 `..` 逃逸、非本地绝对路径，以及 dst 落在 trash 之外、或 src 指向系统目录的伪造 manifest；trash 根目录一律由 manifest 文件的实际位置推导，不采信文件里声明的值
5. **不跟随链接** — `empty` 既不跟随也不删除隔离区内的符号链接/junction（包括清理空目录时），避免删到隔离区之外
6. **清空保护** — `empty` 拒绝清空盘符根目录、系统目录、junction，以及根目录下没有 dupfind manifest 的普通目录；实际执行要求输入 `DELETE`

系统目录的判定基于**规约后的真实路径**，因此下列写法同样会被识别并拦截：

| 写法 | 说明 |
|---|---|
| `C:\WINDOWS` | 大小写不敏感 |
| `C:\Windows ` | 尾随空格，系统会静默剥掉 |
| `C:\Windows.` | 尾随点，同上 |
| `C:\PROGRA~1` | 8.3 短名，指向 `Program Files` |
| `\\?\C:\Windows` | 扩展长度前缀 |

## 退出码

| 码 | 含义 |
|---|---|
| 0 | 成功，且存在确认重复 / 有文件被处理 |
| 1 | 成功，未发现重复 / 没有可处理的动作 |
| 2 | 参数错误、运行失败 |
| 3 | 报告文件格式/版本不匹配 |
| 130 | 用户中断（已保存部分结果） |

## 硬链接与可节省空间

同一物理文件通过硬链接出现在多个路径时，只占一份磁盘空间。工具用 `VolumeSerialNumber + FileId` 识别它们：

- 整组都是同一物理文件 → `status=shared`，`savingsBytes=0`
- 混合场景（1 个独立副本 + 若干互为硬链接的文件）→ 按**物理簇**计算，保留一簇、移走其余簇，可节省空间按实际占用计算

硬链接簇要么整簇保留、要么整簇移走，不会只移动其中一部分。

## Web 界面

```powershell
dupfind-web --port 8123
```

打开 http://127.0.0.1:8123 。当前版本只读：能扫描、浏览报告、切换保留策略预览，不会移动或删除文件。

## 开发

```powershell
python -m pytest
```

测试分两类：`tests/test_*_acceptance.py` 是规格第 10 节的验收用例（黑盒调用 CLI）；`tests/test_p0_regressions.py` 和 `tests/test_restore.py` 是单元级回归测试。

## 已知限制

- 仅支持 Windows，仅扫描单个卷，不跨盘
- 不提供真删除（只能隔离后清空）
- 不支持云盘/网络驱动器
- 大报告（数万组）在 Web 界面加载较慢，尚未分页

## 许可

[MIT](LICENSE)
