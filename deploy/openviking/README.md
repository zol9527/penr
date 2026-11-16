# 本仓内置 OpenViking（长期知识库）

本仓库**不使用**你的个人/全局 OpenViking（如 `viking.0xyz.dev`）。  
长期知识库是 compose 里的独立 `openviking` 服务，数据落在 `data/openviking/`。

## 两层知识

| 层 | 存储 | 生命周期 |
|----|------|----------|
| **task** | PostgreSQL `knowledge_docs.scope=task` | 单次渗透任务内 |
| **long-term** | 本仓 OpenViking | 任务完成后提炼写入，跨任务复用 |

## 启动

```bash
# 1) 准备配置（首次）
make ov-init

# 2) 编辑 data/openviking/ov.conf —— 填入 embedding + VLM 密钥
# 3) 编辑 data/openviking/ovcli.conf —— 填入 User/Admin API key（首次可先用 root，再在 Studio 建用户）

make ov-up          # 仅起本仓 OpenViking
# 或
docker compose up -d openviking postgres
```

- API / Studio: http://127.0.0.1:1933 （Studio: `/studio`）
- penr 服务通过 `OPENVIKING_URL` + `OPENVIKING_API_KEY` 连接**本实例**

## 与 penr 的关系

任务 `done` 时，`KnowledgePromoter` 会：

1. 收集该任务的 task 层文档、facts、findings  
2. 提炼知识点  
3. 写入本仓 OpenViking：`viking://resources/penr/knowledge/tasks/{task_id}/...`  
4. 可选 `add-memory` 便于语义检索  

Worker / Planner 检索时：**先查 task 层，再查本仓 OpenViking 长期层**。
