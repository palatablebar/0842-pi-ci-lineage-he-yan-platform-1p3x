# 接口

- `GET /`：工作台。
- `GET /api/catalog`：阶段、字段和样例。
- `POST /api/stages/{stage}`：按阶段返回处理结果。
- `POST /api/runs`：执行完整批次并保存草稿。
- `GET /api/runs/{id}`：读取批次。
- `POST /api/runs/{id}/review`：签发或暂缓，提交 `decision`、`comment`、`expected_revision`。
- `GET /api/runs/{id}/evidence`：下载已签发证据。
- `GET /api/examples/{profile}`：下载样例。
- `POST /api/certification/evaluate`：保守认证原料占比试算。
- `POST /api/certification/sorting-records`：幂等登记分拣/损耗归属记录。
- `GET /api/certification/workbench`：认证承诺工作台。
- `POST /api/runs/{id}/sorting-records`：晚到记录补齐后重算运行。
- `GET /api/runs/{id}/certification`：读取运行的认证承诺和依据。
- `GET /healthz`：健康检查。
