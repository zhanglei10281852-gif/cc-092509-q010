# 专利与技术秘密档案管理服务

这是一个面向研发机构、法务部门和保密办公室的模块化后端，集中管理专利交底资料、技术秘密载体、移交批次、受控副本签发、查阅借阅、对外披露、归还、合规处置、载体盘点、版本与载体来源、密级库位、泄密事件、登录权限、审计以及可恢复后台任务。项目使用 FastAPI 与 SQLite，所有运行数据保存在单个本地数据库文件中，不依赖另行部署的数据库、缓存或消息队列。

## 已有能力

- 身份与权限：支持引导管理员、登录、会话、用户、角色和细粒度权限。
- 批次与二维码：移交批次保存项目、数量和稳定二维码载荷。
- 档案登记：登记专利交底、工艺文档、源代码介质等资产，保存密级库位和生命周期状态。
- 受控副本签发：一次事务内扣减来源载体、创建副本、记录损耗和版本来源事件。
- 查阅借阅归还：保存查阅用途、到期时间、部分归还和最终归还状态。
- 对外披露登记：使用幂等键登记合作方、披露范围和载体消耗，防止重复请求二次扣减。
- 位置脱敏：普通权限只能看到受限库位的替代码，授权人员可查看精确位置。
- 双人审批：合规处置、敏感库位解密等高风险操作要求申请人与审批人分离，并累计不同审批人的决定。
- 泄密事件追踪：事件可以关联档案或移交批次，保存严重度、调查状态和处置结果。
- 审计与任务：关键身份及业务操作留痕，后台任务支持去重、领取与完成。
- 专利家族关系维护：同一技术在多国的申请、继续申请、分案与优先权主张统一建模；关系类型校验（继续/分案要求同辖区、在先申请日不得晚于在后申请、禁止循环）、优先权日期沿生效关系继承、合并前冲突预演、撤销后影响查询；所有关系变更经审核后才生效，重复导入保持原成员，家族汇总排除已撤销关系，重启后拓扑与审计顺序确定。

## 专利家族接口

- `POST /api/patent-families/applications`：登记申请（按申请号幂等，重复登记保持原家族成员关系）。
- `POST /api/patent-families/import`：批量导入申请与关系提案，重复导入不产生重复成员或重复变更。
- `POST /api/patent-families/changes`：提交 link/revoke/merge 变更，处于待审核状态。
- `POST /api/patent-families/changes/{id}/decisions`：审核变更（申请人与审核人分离）。
- `POST /api/patent-families/changes/{id}/apply`：审核通过后执行变更，生效前重新校验。
- `POST /api/patent-families/merge/rehearse`：成员合并冲突预演（未生效变更、秘密资产与已公开申请混排、连接关系合法性）。
- `GET /api/patent-families/relations/{id}/impact`：撤销影响查询，生效中关系给出预演、已撤销关系给出回溯。
- `GET /api/patent-families/{id}/topology|summary|events`：确定序拓扑、排除已撤销关系的汇总、按主键排序的家族事件。
- `GET /api/patent-families/applications/{id}/lineage`：解释一份申请与家族中其他申请的关系与优先权继承链。

## 运行环境

- Python 3.11
- SQLite 3，由 Python 标准库提供
- Linux、macOS 或 Windows

## 安装

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

默认数据库位于 `./data/archives.db`，可用 `ARCHIVE_DATABASE_PATH` 指定其他路径。

## 初始化与完整性检查

```bash
python -m app.cli init-db
python -m app.cli check-db
```

## 启动 API

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## 测试

```bash
python -m pytest
```

## 编译检查

```bash
python -m compileall -q app tests
```

## API 冒烟

```bash
python -m app.cli smoke
```
