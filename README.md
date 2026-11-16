# penr — AI 自主渗透测试平台

受 [AntiFox](https://github.com/AntiFox-AI/antifox)（AGPL-3.0）启发的 Python 实现（包名 / CLI：`penr`）：

- **LangGraph**：任务探索环（debounce → plan → workers）
- **CrewAI**：Planner / Worker / Main 角色协作（在 LangGraph 节点内调用）
- **FastAPI**：REST + SSE，尽量对齐原 `/api/*` 面，便于复用原 `web/` 前端

## 要求

- Python **3.13**（CrewAI 当前不支持 3.14）
- PostgreSQL 16+
- 可选：LLM API Key（Anthropic / OpenAI）；无密钥时走 fallback 规划/执行环，便于本地冒烟

## 快速开始

```bash
cp .env.example .env
cp config.example.json config.json
# 启动 Postgres（或 docker compose up -d postgres）
make sync
make migrate
make run
curl -s http://127.0.0.1:8787/health
```

首次使用：

```bash
curl -s -X POST http://127.0.0.1:8787/api/setup \
  -H 'content-type: application/json' \
  -d '{"username":"admin","password":"adminadmin"}'
```

创建并启动任务：

```bash
TOKEN=...  # setup/login 返回
curl -s -X POST http://127.0.0.1:8787/api/tasks \
  -H "authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' \
  -d '{"description":"demo","goal":"侦察 http://example.com","auto_start":true}'
```

## 知识库两层

| 层 | 存哪 | 何时用 |
|----|------|--------|
| **task** | PostgreSQL `knowledge_docs`（`scope=task`） | 单次任务内的授权范围、笔记、临时材料 |
| **long-term** | **本仓内置 OpenViking**（`docker compose` 的 `openviking`，数据在 `data/openviking/`） | 任务 `done` 后自动提炼写入；跨任务检索 tradecraft |

不连个人全局 OpenViking（如 `viking.0xyz.dev`）。初始化：`make ov-init && make ov-up`，详见 [deploy/openviking/README.md](deploy/openviking/README.md)。

## 目录

```
src/penr/
  api/             FastAPI
  orchestration/   LangGraph TaskEngine + SSE broadcast
  crews/           CrewAI Planner/Worker/Main
  db/              asyncpg + schema stores
  tools/           Bash/Web/MCP/图工具
  guard/           HITL intercept
  traffic/         mitmproxy addon + SQLite 索引
  knowledge/       task 层 + 本仓 OpenViking 长期层 + promote
  report/          Markdown + python-docx
deploy/openviking/ 本仓 OV 配置模板
skills/            skill 内容（源自 AntiFox）
schema.sql         PG schema（源自 AntiFox）
```

## 前端对接（M8）

前端源码已迁入本仓 [web/](web/)（Next.js）。详见 [web/README.md](web/README.md)。摘要：

```bash
make web-install
make web-dev
# AUTOPENTEST_API / NEXT_PUBLIC_SSE_BASE 默认指向 http://127.0.0.1:8787
```

静态导出并挂到本服务：`make web-export` → `data/webui/`（服务自动挂载）。

## Compose 全栈冒烟清单

四件套：`postgres` + `openviking` + `penr` + `mitmproxy`。

```bash
# 1) 本仓 OV 配置（勿指向个人全局 viking.0xyz.dev）
make ov-init
# 编辑 data/openviking/ov.conf：embedding/VLM + root_api_key（可先空跑 PG 主环）

# 2) 起依赖与 API
docker compose up -d postgres mitmproxy openviking
make migrate
# 本地进程（推荐开发）或容器：
make run
# 或：docker compose up -d --build penr

# 3) 健康
curl -sf http://127.0.0.1:8787/health
curl -sf http://127.0.0.1:1933/health || true   # OV 未配 embedding 可能 degraded
# mitmproxy 监听宿主机 8788 → 容器 8080

# 4) 主环（无 LLM key 走 fallback）
curl -s -X POST http://127.0.0.1:8787/api/setup \
  -H 'content-type: application/json' \
  -d '{"username":"admin","password":"adminadmin"}'
# 用返回 token 建任务 → GET /api/tasks/{id}/activity 应有 planner/worker 事件
# SSE：GET /api/tasks/{id}/events?token=...

# 5) 本仓 web（可选）
# make web-install && make web-dev
# 或 make web-export 后访问 http://127.0.0.1:8787/
```

跳过项：个人 OpenViking 云；未填 embedding 时长期库检索可延后。

## 验收清单（对照计划 M0–M8）

| 里程碑 | 验收要点 |
|--------|----------|
| M0 | `make migrate` + fallback 任务：`created`→`running`→≥1 intent/fact；SSE 有事件 |
| M1 | `/api/auth/*`、`POST /api/tasks/{id}/control`、`/api/exploration/*`、SSE `?token=` |
| M2 | exploration 缺页、`/api/stats`、companies CRUD 不 404 |
| M3 | steer/kill/deadline/rerun；运行中 intent 可 kill |
| M4 | `/api/traffic` 分页/host；finding↔traffic；mitmproxy 可录包 |
| M5 | RBAC 非 admin 403；intercept rules；settings/LLM pool |
| M6 | conversations 续聊；MCP CRUD+worker 挂载；skills 上传/列表 |
| M7 | report job/docx；enrich；knowledge search；promote 后本仓 OV 可检索 |
| M8 | 原 web 指到 8787；上表 compose 冒烟；已知非移植项见下 |

## 刻意不移植

| 项 | 原因 |
|----|------|
| Go `selfupdate` / `/api/update/*` | 用 compose/进程滚动更新即可 |
| Go `embedui` tags | 前端独立部署或静态挂 `data/webui` |
| 个人全局 OpenViking | 长期库只用本仓 `deploy/openviking` |
| Go `engine/*` 逐行移植 | 继续 CrewAI + LangGraph |

## License

AGPL-3.0-only（继承 AntiFox）。本仓库为二次开发/移植，请遵守 AGPL 义务。
