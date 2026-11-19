# C Build Cache Service

面向 CI 并行构建场景的小型 C 工程编译服务：调用方通过 JSON 按相对路径提交
`.c` 源码和 `.h` 头文件，服务用固定 GCC 参数编译，按"源文件 + 实际引用的各级
项目头文件（路径和内容）"复用目标文件，只链接本次提交对应的目标文件，返回
可执行文件和复用数量。

## 功能

- **JSON 提交整个工程**：`POST /build`，`files` 为 `{path, content}` 列表，
  路径为项目相对 POSIX 路径（仅 `.c` / `.h`）。
- **固定 GCC 参数**：`-O2 -Wall -Wextra -std=c11`，并用 `-I<项目根目录>` 把
  项目根加入头文件搜索路径。
- **目标文件级缓存**：缓存键 = 固定编译参数 + 源文件路径与内容 + 该源文件
  传递闭包内所有项目头文件的路径与内容 的 SHA-256。四者一致才复用。
- **Singleflight**：相同编译输入同时到达时，按缓存键加锁，全进程只编译一次，
  其余请求等待后直接复用结果（`/stats` 中 `singleflight_waits_total` 可见）。
- **同名头文件遮蔽**：`#include` 解析规则与 GCC 一致（引号包含先查包含指令
  所在文件的目录，再查项目根；尖括号只查项目根）。新增同名头文件改变引用
  指向时，受影响单元的依赖闭包变化 → 缓存键变化 → 自动重编译，未受影响
  的单元继续命中缓存。
- **只链接本次提交的目标文件**，每次构建产出独立可执行文件。
- **失败诊断**：编译/链接失败返回 `400`，`detail.diagnostics` 为 GCC 的
  stderr；只有成功编译的目标文件才进入缓存（失败不会污染缓存）。
- **缓存持久化**：对象缓存落在磁盘目录，Docker 部署时挂载命名卷，重启后
  继续复用。

## 快速开始（Docker Compose）

```bash
docker compose up --build
```

服务监听 `http://localhost:8000`：

- Swagger UI：<http://localhost:8000/docs> —— `POST /build` 已内置可运行的
  三文件示例，点 **Try it out → Execute** 即可发起构建，响应区出现
  **Download file** 链接下载可执行文件；复用数量在响应头
  `X-Compiled-Count` / `X-Reused-Count` / `X-Unit-Count` 中。
- `GET /stats`：缓存对象数、累计编译/复用次数、singleflight 等待次数。
- `GET /healthz`：健康检查。

对象缓存保存在命名卷 `cbuild-cache` 中，容器重启后仍然有效。

## API

### `POST /build`

请求体：

```json
{
  "output": "app",
  "files": [
    {"path": "main.c", "content": "#include <stdio.h>\n#include \"util.h\"\nint main(void){ printf(\"%d\\n\", util_answer()); }"},
    {"path": "util.c", "content": "#include \"util.h\"\nint util_answer(void){ return UTIL_ANSWER; }"},
    {"path": "util.h", "content": "#ifndef UTIL_H\n#define UTIL_H\n#define UTIL_ANSWER 42\nint util_answer(void);\n#endif"}
  ]
}
```

- `files[].path`：项目相对路径，只允许 `.c` / `.h`，拒绝 `..`、绝对路径、
  重复路径；最多 128 个文件，单文件最大 256 KB。
- `output`：可选，下载文件名（默认 `app`）。

成功（`200`）：`application/octet-stream` 的可执行文件，响应头：

| 头 | 含义 |
| --- | --- |
| `X-Compiled-Count` | 本次请求实际编译的单元数 |
| `X-Reused-Count` | 本次请求复用的单元数（含 singleflight 等待） |
| `X-Unit-Count` | 本次提交的编译单元总数 |

失败（`400`）：

```json
{
  "detail": {
    "stage": "compile",
    "message": "compilation of main.c failed",
    "unit": "main.c",
    "diagnostics": "main.c: In function 'main':\nmain.c:2:5: error: ..."
  }
}
```

`stage` 取值：`validation`（请求非法）/ `compile` / `link`。

### curl 示例

```bash
curl -sS -X POST http://localhost:8000/build \
  -H 'Content-Type: application/json' \
  -d @request.json \
  -o app -D -        # 响应头里看 X-Compiled-Count / X-Reused-Count
chmod +x app && ./app
```

## 缓存设计要点

```
key(unit) = SHA256( 固定GCC参数
                   ‖ 源文件路径 ‖ 源文件内容
                   ‖ 排序后的每个依赖头文件(路径 ‖ 内容) )
```

- 依赖闭包由静态解析 `#include` 得到，解析规则与 GCC 一致（引号包含先查
  指令所在文件目录，再查 `-I` 项目根；尖括号查项目根）。解析不到项目文件
  的包含视为系统头，不参与缓存键。
- 未引用的文件不影响缓存键：提交里新增无关文件不会造成失效。
- 新增同名头文件改变解析结果时，闭包集合变化 → 键变化 → 仅受影响单元
  重编译。
- 并发同一键：每键一把锁，双重检查 + 原子 rename 发布，编译只发生一次；
  编译失败抛异常，缓存不写入任何内容。

## 本地开发与验证

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt httpx
CACHE_DIR=./data/cache WORK_DIR=./data/work \
  .venv/bin/python -m uvicorn app.main:app --port 8000

# 端到端验证（需要服务已启动；假设缓存为空）
.venv/bin/python tests/e2e_test.py http://127.0.0.1:8000
```

`tests/e2e_test.py` 覆盖：首次构建全编译、原样重提全复用、头文件变更只
重编译受影响单元、同名头遮蔽、8 路并发相同构建全局只编译一次、6 路并发
不同工程互不干扰、编译/链接失败诊断、失败不入缓存、路径校验。

## 遗留事项 / 限制

- 依赖闭包是**静态解析**的保守近似：`#if/#ifdef` 条件包含、宏拼接包含
  （`#include FOO`）不展开，可能把未实际参与编译的头文件算进键里（只会
  多重编译，不会错用旧产物）。如需精确依赖，可在首次编译时用 `gcc -MMD`
  校准。
- 只链接 libc，未暴露额外链接库（如 `-lm`）或自定义编译参数的入口——
  参数按需求固定。
- 单请求内各单元串行编译（工程小、并发主要来自多请求）；如需可改为
  请求内并行。
- 服务会执行调用方提交的 C 代码编译产物之外的编译器本身，请只在可信
  网络内对可信代码开放；如需对外，应加认证、资源限额与沙箱（如
  seccomp/只读根fs/超时已部分具备）。
- 缓存目录无容量上限与 LRU 淘汰，长期运行需自行清理或加配额。
- 本环境没有 Docker，镜像未实际构建；`Dockerfile` / `docker-compose.yml`
  已经过静态检查，行为与本地验证的 uvicorn 运行方式一致。
