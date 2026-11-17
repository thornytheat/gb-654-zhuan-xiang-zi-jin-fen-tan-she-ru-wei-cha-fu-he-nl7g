# 专项资金分摊与舍入尾差复核

        面向一线复核人员的单服务数据工作台。样例数据随仓库提供，结论保留行号、原因码和原值。

        ## 业务模块

        - M1 预算采信：`/api/budgets/import`
- M2 资格校验：`/api/eligibility/evaluate`
- M3 权重测算：`/api/weights/calculate`
- M4 金额分摊：`/api/allocations/calculate`
- M5 尾差裁决：`/api/remainders/resolve`
- M6 批次复核：`/api/export/allocations.csv`
- M7 模拟支付与撤回对账：`/api/payments/positions`（本地模拟支付端，不接真实银行）

        ## 核心规则

        先迭代封顶再按有效权重分配，最后用最大余额法落到分；输入顺序变化不得改变结果。

## M7 支付与撤回对账

待付指令状态机：`PENDING → SUBMITTED → PAID / FAILED`，撤回路径 `PAID → RECOVERY_PENDING → RECOVERED`、待付直接 `VOIDED`。

- 撤回审批：支付端未受理的待付指令直接作废并释放占款；已实付的扣款如实保留，转为待追回，不抹实付、不按回执恢复审批。
- 迟到回执：撤回后到达的成功回执进入待追回；乱序回执不倒退、重复回执幂等；无受理记录的回执拒绝入账。
- 待追回款未全额到账不释放、不调剂给其他批次；全额追回登记后才回到可用资金。
- 发送中断/超时：受理记录先落库，重试先查受理状态并复用同一请求，绝不重复扣款；同库持久化，进程重启可恢复。
- 跨批次并发：`BEGIN IMMEDIATE` 串行化建批次，持锁核对实时可用资金，合计占款不超预算。
- 头寸守恒：`budget = available + occupied + paid - recovered`，`/api/payments/positions` 可核对可用、占用、实付、待追回与已追回。
- 模拟场景由 `/api/payments/scenarios` 控制：`ok` / `reject` / `timeout` / `interrupt`。M7 不修改 M2 资格评分。

        ## 启动

        ```bash
        docker compose up --build
        ```

        浏览器访问 `http://localhost:18213`。运行测试：

        ```bash
        docker compose exec -T app pytest -q
        ```

        ## 样例流程

        依次运行六个模块；最后在批次页复核输入、有效、隔离与输出合计。同一输入摘要重复执行时复用原 `run_id`。

        ## 已知不覆盖

        不处理加密文件与需要外部密钥的对象；不连接真实生产存储。
