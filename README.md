# 06-shiftboard（交班台）

交班台 — 班次责任与在途批次交接：班次登记 → 在途批次归属 → 交班互斥办理 → 交班前后对账。

**不是** 多源 Feed 归并，**不是** 作业依赖编排，**不是** CDC 变更流，**不是** 质检签发，**不是** 数仓贴源/明细/汇总分层。  
**也不是** OA 考勤排班后台、电商订单、报表 CSV 看板。

## 启动

本地开发：

```bash
# 后端
cd backend
pip install -r requirements.txt
set DATA_DIR=%CD%\data
set FIXTURES_DIR=%CD%\..\fixtures
uvicorn app.main:app --host 127.0.0.1 --port 10600

# 前端
cd frontend
npm install
npm run dev
```

| 入口 | 地址 |
| --- | --- |
| 前端 | http://127.0.0.1:5600 |
| 接口 | http://127.0.0.1:10600 |

## 主链

登记班次与值班人 → 在途批次归属当前班次 → 发起交班（全仓同时仅一单进行中）→ 完成交班整批过户 → 交班对账报告与清单/批次同源

## 界面约定

页面文案全部中文；入口：总览 / 班次 / 在途批次 / 交班办理 / 交班记录 / 交班对账。

## 与已有仓差异

| 仓 | 骨架 |
| --- | --- |
| Feedstitch | 多源解析→冲突隔离→水位 |
| Batchgate | 作业依赖→重试→水位 |
| Changeflow | 事件序→缺口→Sink 游标 |
| Checklane | 入检→规则→隔离签发 |
| Tiercraft | 贴源→明细→汇总→层间对账 |
| **本仓** | **班次归属→交班互斥→批次过户→交班对账** |
