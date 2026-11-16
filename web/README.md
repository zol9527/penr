# penr/web — Next.js 前端

本目录从前 AntiFox `web/`（Next.js App Router）迁入，对接本仓 penr Python API（默认 `:8787`）。

- 根仓库许可证：AGPL-3.0（见仓库根 `LICENSE`）
- 本目录模板版权：见本目录 `LICENSE`（MIT）

## 开发：rewrite 到 Python

`next.config.mjs` 使用 `AUTOPENTEST_API`（默认 `http://localhost:8787`）把 `/api/*` 反代到后端：

```bash
# 仓库根目录
make web-install
make web-dev
# 等价于：
#   cd web
#   export AUTOPENTEST_API=http://127.0.0.1:8787
#   export NEXT_PUBLIC_SSE_BASE=http://127.0.0.1:8787
#   npm run dev
```

浏览器验收建议路径：

1. `/api/auth/status` → `/setup` 或 `/login`
2. 登录 → 任务列表 → 任务详情（图 / activity / SSE）
3. 流量、发现、报告等依赖后端已挂路由；未实现路径会 404

## 静态导出挂到本服务

```bash
make web-export
# 将 web/out/ 同步到 data/webui/
```

Python 服务在 `data/webui`（或 `ANTIFOX_WEB_DIST`）存在时会用 `StaticFiles` 挂载根路径；API 仍走同源 `/api/*`。跨端口开发请继续用 `NEXT_PUBLIC_SSE_BASE`。

## 刻意不移植 / 前端需容忍

| 项 | 说明 |
|----|------|
| `/api/update/*`（selfupdate） | Python 侧不实现；设置页已隐藏更新卡片 |
| Go `embedui` build tags | 本仓不嵌入前端二进制 |
| 部分管理面细路径 | 以 Python 已挂路由为准；未实现路径返回 404 |

## 联调前检查

```bash
make migrate && make run
curl -sf http://127.0.0.1:8787/health
curl -sf http://127.0.0.1:8787/api/setup/status
# 或 compat：/api/auth/status
```
