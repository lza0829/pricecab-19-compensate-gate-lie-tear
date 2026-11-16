# 06-pricecab（价签柜）

Pricecab — 改价单按预约时刻生效，现行价签可查，并与销售夹具对账。

与 Feedstitch（多源归并）、Batchgate（作业编排）、Syncledger（双端对账）、Changecast（CDC 投影）、Proofpack（采样放行）主链路不同。  
不是电商订单/购物车，不是仓库进出库 CRUD，不是考勤 OA。夹具全本地。

## 启动

```bash
docker compose up --build
```

| 入口 | 地址 |
| --- | --- |
| 前端 | http://localhost:5600 |
| API | http://localhost:10600 |

## 主链（0-1）

选商品建改价单（写明生效时刻）→ 拨业务钟或点「应用到期」→ 现行价签更新 → 销售对账看一致/不一致

## 误用纠偏（向前补偿，禁止回滚）

误应用后**不允许把价签本拨回冒充没发生过**：已成功批次的价签写入（`price_book`）与历史行（`price_history`）只追加、不删除、不改写，系统不提供任何 delete / rollback / revert 入口；历史链与销售对账的 as-of 查询因此不会出现空洞。纠偏只能新开补偿改价单，与普通单走同一套到期应用规则。

- **误用标记**：只能针对状态为 `applied` 的改价单或已成功批次创建（`POST /api/compensation/markers`），记录涉及 SKU、原因与当时的策略快照。
- **补偿发起策略（全局互斥，二选一，`/api/compensation/strategy`）**
  - `auto_draft` 自动草稿：标记后系统生成一张补偿单草稿，明细为反向/目标价（误用前价签），生效时刻取当前业务钟（≥ 当前业务钟，绝不回溯），仍须经「应用到期」才写入价签；草稿未应用前相关商品禁止再应用其他非补偿单。
  - `manual` 仅人工：系统不造草稿，被标记商品加禁应用闸，直至存在一张指向该标记且已应用的补偿单才解除。人工补偿单的生效时刻必须 ≥ 当前业务钟，且明细 SKU 必须落在标记范围内。
- **禁应用闸（单一事实收口，全局只贯彻一种规则）**：禁闸**没有独立状态表/开关**（旧的 `gate_shadow` 布尔表已在 schema 初始化时 `DROP`），每次都由事实重算：
  - **无已应用补偿**覆盖该标记 → 标记强制为 open，禁闸保持关闭，普通应用一律拒绝；即便有人直接把标记状态拨成 resolved，读取禁闸或下一轮应用到期时也会按事实改回 open（`reopened_marker_ids` 可观测）；
  - **已有已应用补偿单**覆盖标记的全部 SKU（且每个 SKU 在 `price_history` 中有该补偿单写入的价签来源行）→ 标记 resolved、禁闸解除；把标记拨回 open 也会在下次读取时按事实改回 resolved（`resolved_marker_ids` 可观测），解除始终能指向已应用补偿证据。
  - 建普通单时命中闸 → 直接 409 拒绝；
  - 到期应用时，闸状态在本轮开始时按事实快照，命中的普通单**跳过不应用**（保持 scheduled、不进批次、在 `skipped_gated` 返回），价签不会被普通单改写；补偿单免检并优先处理；本轮末尾统一用补偿应用事实 + 价签来源重算全部标记，被跳过的普通单下一轮再处理。
  - `GET /api/compensation/gates/integrity` 只读互证标记表、补偿单状态、价签来源（`price_book`/`price_history`）三者一致性，报出「无补偿却 resolved」「已补偿仍 open」「已应用补偿无价签来源行」「禁闸后普通单仍写入价签」等分叉。
- **部分补偿**：一张标记含多个 SKU 时，须等指向它的已应用补偿单覆盖全部 SKU 才解除；未覆盖完保持 open。
- 补偿单与普通单在同一张 `price_tickets` 表（`kind='compensation'`、`compensates_marker_id` 指向标记），应用后同样追加价签与历史行，对账的 as-of 价格链完整可追溯。

## 测试

```bash
cd backend
pip install -r requirements.txt httpx pytest
DATA_DIR=/tmp/pricecab-test python -m pytest tests/ -q
```

## 技术栈

Python 3.12 + FastAPI + SQLite；Vue 3 + Vite + Nginx。
