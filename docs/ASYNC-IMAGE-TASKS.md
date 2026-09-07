# 异步生图任务（Cloudflare 524）

`gpt-image-2` 图生图经常超过 100 秒。域名如果走 Cloudflare，同步 `POST /v1/images/edits` 会在 origin 仍在出图时被网关切断，客户端看到 **HTTP 524**，管理后台却已经有图。

正确做法：提交立刻返回 `task_id`，后台 worker 生成，客户端轮询 `GET /v1/tasks/{task_id}`。

生产环境 `img.nodelite.top` 已按此部署。同步接口仍保留，给短请求和旧客户端用。

## 何时走异步

满足任一条件即入队，HTTP **202**：

- JSON / form 字段 `async=true`
- 请求头 `Prefer: respond-async`

`stream=true` 仍走 SSE，不入队。

## 提交

### 文生图

```http
POST /v1/images/generations
Authorization: Bearer sk-app-xxxx
Content-Type: application/json
Prefer: respond-async

{
  "model": "gpt-image-2",
  "prompt": "一只橘猫坐在窗台",
  "n": 1,
  "size": "1024x1024",
  "response_format": "b64_json",
  "async": true
}
```

### 图生图

```http
POST /v1/images/edits
Authorization: Bearer sk-app-xxxx
Prefer: respond-async

multipart/form-data:
  image=@source.png
  prompt=保持主体，换成白底电商图
  model=gpt-image-2
  n=1
  async=true
```

立即响应：

```json
{
  "task_id": "job_ab12cd34ef56",
  "status": "submitted"
}
```

不要在这次 POST 上等待图片。Cloudflare 免费套餐 origin 超时约 100 秒；提交必须在几秒内返回。

## 轮询

```http
GET /v1/tasks/{task_id}
Authorization: Bearer sk-app-xxxx
```

| 本接口 `status` | 内部 job 状态 | 含义 |
|---|---|---|
| `submitted` | `queued` | 已入队 |
| `processing` | `running` | worker 正在生成 |
| `completed` | `succeeded` | `result.images[]` 含 `url` 和/或 `b64_json` |
| `failed` | `failed` / `cancelled` | 见 `error.message` |

建议：提交后 2 秒开始轮询，间隔 3 秒，总等待 5–6 分钟。`gpt-image-2` 图生图常见 2–4 分钟。

完成示例：

```json
{
  "task_id": "job_ab12cd34ef56",
  "status": "completed",
  "result": {
    "images": [
      {
        "url": "https://img.example.com/assets/2026/09/07/xxx.png",
        "b64_json": "iVBORw0KGgo..."
      }
    ]
  }
}
```

失败示例：

```json
{
  "task_id": "job_ab12cd34ef56",
  "status": "failed",
  "error": { "message": "no available image quota" }
}
```

## 兼容性

| 客户端 | 行为 |
|---|---|
| 旧 OpenAI SDK / 不带 `async` | 仍同步等待完整 `data[].b64_json` |
| `viskit-studio` `openai_compatible` 适配器 | 默认 `async=true` + `Prefer: respond-async`，轮询 360 秒 |
| 本仓库 Web 画图页 | 提交后轮询 `/v1/tasks/{id}`，对调用方仍返回原来的 `{created, data}` |

旧上游若拒绝 `async` 字段（HTTP 422），`viskit-studio` 会去掉该字段再提交一次。

## 不要做的事

- 对 524 立刻重试同一张图。origin 多半还在画，重试会再造一批图，客户端仍然拿不到。
- 把同步 POST 超时设到 180 秒指望穿过 Cloudflare。网关会先 524。
- 并发打满 4 路同步 `/images/edits`。会把 origin 拖过 100 秒，集体 524。

## Worker 与落盘

- 进程内 `start_image_job_worker` 会起最多 8 条线程，真正干活的数量看设置里的「生图并发路数」（默认 2）。
- 图生图参考图写到 `data/job-inputs/{job_id}/`，任务结束后删除。
- 成功结果走现有 `image_asset_service.archive_result`，所以管理后台看得到图。
- 生产建议 `IMAGE_JOB_QUEUE_BACKEND=redis`；当前 Lightsail 实例用 `storage` 轮询，单进程足够。
