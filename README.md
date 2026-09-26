# 电台播出与版权窗口排程

一个不依赖第三方包、使用 SQLite 和标准库 HTTP 服务的电台排程项目。系统把“计划排期”和“实际播出”分开保存，支持地区授权、日期窗口、禁播时段、节目冷却、赞助商间隔、直播临时替换、实播对账与版权越界检查。

## 运行

需要 Python 3.11+。

```bash
python app.py
```

默认端口为 `8111`，页面地址是 <http://127.0.0.1:8111>。第一次启动会创建 `radio.db` 并写入三条演示排期。也可以设置端口和数据库位置：

```bash
PORT=9000 RADIO_DB=/tmp/radio.db python app.py
```

## 测试

```bash
python -m unittest discover -s tests -v
```

测试覆盖完整流程：排期、临时替换、播放日志、按日期对账；同时覆盖时间重叠、未授权地区和实播错节目等失败场景。

## 主要 API

- `GET /api/state`：节目、排期和最近对账异常
- `POST /api/programs`：创建节目并授权地区
- `POST /api/programs/{id}/regions`：追加地区授权
- `POST /api/programs/{id}/license`：收窄/修改授权地区与起止日期（先校验未实播排期）
- `POST /api/schedule`：创建排期
- `POST /api/slots/{id}/replace`：替换计划节目并重新校验
- `POST /api/playout`：登记实播记录
- `POST /api/reconcile`：按日期生成漏播、错播、时长偏差和超授权异常

准备排期时填写 `air_date`、`start_time`、`program_id`、`region`。页面会直接显示校验错误，不会保存失败的排期。

## 授权收窄

版权授权临时收窄时，在页面选择节目并填写新的地区与起止日期后提交。系统会先检查该节目下**没有实播记录、状态仍为 planned 或 replaced** 的排期：只要有一条会落到新日期窗口外或其地区被移除，整次修改不会保存，接口返回 `409` 和逐条冲突清单（播出日期、开始时间、排期编号、地区、原因）；全部合规才更新节目。已登记实播的排期（无论计划或替换状态）以及已取消排期继续保留，不参与阻断。
