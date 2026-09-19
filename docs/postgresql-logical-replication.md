# IoT 数据库同步方案

## 目标

192 服务器负责接收、解析和计算 DLT645 数据，192 本机 PostgreSQL 是写入主库。Monitor Center PostgreSQL 通过 PostgreSQL 原生逻辑复制获得业务表副本，104 页面继续通过 Monitor Center 查询接口展示数据。

## 数据流

```text
电表/ESP8266
    -> 192 /api/v1/dlt645/frame
    -> 192 PostgreSQL
       - iot_dlt645_frames
       - iot_dlt645_metrics
       - iot_dlt645_daily_energy
    -> PostgreSQL logical replication
    -> Monitor Center PostgreSQL
    -> 104 查询 API / 页面
```

接收 API 不直接连接 Monitor Center 数据库，也不再需要 `iot_sync_outbox`。逻辑复制由 PostgreSQL 根据 WAL 自动断点续传。

## 192 本机配置

```env
POWER_MONITOR_DATABASE_URL=postgresql://iotmonitor:<password>@127.0.0.1:5432/iotmonitor
POWER_MONITOR_RAW_ARCHIVE=/var/lib/iotmonitor/dlt645-frames.jsonl
```

`POWER_MONITOR_DATABASE_URL` 是 192 接收服务的主库连接串。旧的 `POWER_MONITOR_CENTER_DATABASE_URL` 不应再配置在接收服务上。

## 源库配置

在 192 的 `postgresql.conf` 中启用逻辑复制：

```conf
wal_level = logical
max_replication_slots = 4
max_wal_senders = 4
```

在 `pg_hba.conf` 中只允许 WireGuard 对端地址使用复制用户：

```conf
host replication monitor_repl MONITOR_CENTER_WG_IP/32 scram-sha-256
host iotmonitor monitor_repl MONITOR_CENTER_WG_IP/32 scram-sha-256
```

创建复制用户和发布：

```sql
CREATE ROLE monitor_repl WITH LOGIN REPLICATION PASSWORD '<strong-password>';
GRANT CONNECT ON DATABASE iotmonitor TO monitor_repl;
GRANT USAGE ON SCHEMA public TO monitor_repl;
GRANT SELECT ON TABLE
  iot_dlt645_frames,
  iot_dlt645_metrics,
  iot_dlt645_daily_energy
TO monitor_repl;

CREATE PUBLICATION iotmonitor_pub
FOR TABLE
  iot_dlt645_frames,
  iot_dlt645_metrics,
  iot_dlt645_daily_energy;
```

如果某张表尚未创建，应先完成表迁移，再创建 publication，避免初始化时缺表。

## Monitor Center 订阅

在创建订阅前，必须先让 192 和 Monitor Center 的发布表拥有兼容的列顺序、类型和约束。当前仓库中两端的 `iot_dlt645_frames` 还不是同一 schema：Monitor Center 版本包含 `source_id`、`event_id`、`raw_archive_path`，而 192 接收服务的简化表没有这些字段。因此不能直接创建订阅。

迁移顺序：

1. 在 192 上为本地表补齐中心端字段和类型，给现有数据填充固定 `source_id`、稳定 `event_id` 和归档路径。
2. 更新接收服务 INSERT，使每条记录生成稳定的 `event_id`，并写入完整字段。
3. 在 Monitor Center 上确认目标表 schema 与 192 完全一致；首次复制前暂停中心端对这些表的应用写入。
4. 完成一次快照校验后，再创建 subscription。

逻辑复制只复制行数据，不会自动解决两端 schema 差异，也不会复制 DDL。

在 schema 对齐后，在 Monitor Center 数据库预先创建同名、同结构的表和索引，然后执行：

```sql
CREATE SUBSCRIPTION iotmonitor_sub
CONNECTION 'host=192-wg-address port=5432 dbname=iotmonitor user=monitor_repl password=<strong-password> sslmode=prefer'
PUBLICATION iotmonitor_pub
WITH (copy_data = true, create_slot = true, slot_name = 'iotmonitor_sub_slot');
```

`copy_data = true` 会先复制已有数据，再持续接收增量。首次迁移数据量很大时，可以先使用 `copy_data = false` 并单独导入快照。

## 分区变更操作规程

逻辑复制只复制行数据，不复制 `CREATE TABLE`、`ALTER TABLE`、分区挂载和 publication 变更。对发布表做分区迁移时，必须把源库和中心库当作一个维护单元处理，不能只在 192 上执行脚本。

推荐顺序如下：

1. 确认当前复制健康，记录源库和中心库的最新 `captured_at`、行数和 `event_id` 范围。
2. 停止接收 API 写入，并在中心库禁用订阅：

   ```sql
   ALTER SUBSCRIPTION iotmonitor_sub DISABLE;
   ```

3. 在两端按完全相同的列类型、约束和分区范围创建或迁移目标表。下一个月份的分区应提前在两端创建。
4. 在源库完成 publication 变更；确认 `pg_publication_tables` 已列出所有目标分区。
5. 在中心库重新加载 publication 映射，不做重复快照：

   ```sql
   ALTER SUBSCRIPTION iotmonitor_sub
   REFRESH PUBLICATION WITH (copy_data = false);

   SELECT srrelid::regclass AS relation, srsubstate
   FROM pg_subscription_rel
   ORDER BY srrelid;
   ```

   每个分区都必须存在且为 `srsubstate = 'r'`（ready）。如果映射为空或目标分区不存在，不得恢复 API 写入。
6. 启用订阅，确认 `pg_stat_subscription` 的 LSN 和时间持续更新后，再启动 API。
7. 用同一个 `event_id` 对比两端，确认没有漏数和重复数据。

维护期间如果需要回填，使用 `source_id + event_id` 做幂等键，不要用自增 `id` 判断重复。中心端的自增序列和源端不是同一个序列，回填时应由中心端生成新的 `id`。

## 运维检查

### 分区和历史归档

`iot_dlt645_frames` 计划按 `captured_at` 按月分区，在线表保留最近 12 个月；超过一年的分区转移到历史表或历史库。分区迁移必须在源库和 Monitor Center 同步执行，并在订阅暂停、API 停写的维护窗口内完成。执行脚本见 `docs/sql/iot_dlt645_frames_partition_migration.sql`。

月度维护只负责提前创建未来分区，见 `docs/sql/iot_dlt645_frames_monthly_maintenance.sql`。归档前必须校验两端行数和 `event_id` 集合，归档后再删除旧分区；不能只在 192 单方面删除，否则会破坏复制一致性。

源库：

```sql
SELECT slot_name, active, restart_lsn
FROM pg_replication_slots;

SELECT application_name, state, sent_lsn, write_lsn, flush_lsn, replay_lsn
FROM pg_stat_replication;
```

中心库：

```sql
SELECT subname, pid, received_lsn, latest_end_lsn, latest_end_time
FROM pg_stat_subscription;

SELECT subname, apply_error_count, sync_error_count
FROM pg_stat_subscription_stats;

SELECT srrelid::regclass AS relation, srsubstate
FROM pg_subscription_rel;
```

重点关注：订阅进程是否存在、`latest_end_time` 是否持续更新、`apply_error_count` 是否增加、每个分区是否为 `ready`、复制槽是否持续增长、WireGuard 连通性以及中心库磁盘空间。

设备接口返回 `202 Accepted` 只表示 192 已接收并写入本地库，不表示中心库已经完成复制。监控必须同时比较 192 和中心库的最新 `captured_at`，并对复制延迟设置告警。

## DDL 和故障恢复

- 逻辑复制不会自动复制 DDL。新增字段时先在订阅端执行兼容 DDL，再在源库执行，最后更新发布对象。
- 192 短时断网时，WAL 保留在复制槽中，恢复连接后自动继续。
- 长时间中断前必须检查源库磁盘和复制槽 WAL 占用；如果 WAL 已被清理，需要删除订阅、重新初始化表，再创建订阅。
- 删除、更新业务数据会同步到中心库；业务表主键必须稳定，且订阅端不能同时人工修改同一批数据。
- 复制链路只应通过 WireGuard 暴露，禁止将 PostgreSQL 端口直接暴露到公网。

## 2026-09-13 复制中断复盘

### 现象

兼容入口持续返回 `202`，192 本地 PostgreSQL 仍有 9 月 14 日数据，但 `iot.ohmyskills.top/dashboard/frames.html` 只能查到 9 月 13 日 `17:59:27`（北京时间）。因此入口服务和设备上报链路正常，问题位于 192 到 Monitor Center 的复制链路。

### 根因

9 月 13 日晚执行了 `iot_dlt645_frames` 分区迁移，并同时删除、重建了 publication 和 subscription。两端的 publication、分区表和订阅映射没有按同一顺序完成，中心端先后出现：

- publisher 上不存在 `iotmonitor_pub`；
- 订阅目标缺少 `iot_dlt645_frames_2026_09`；
- 订阅映射为空，apply worker 连续失败；
- 迁移期间还出现权限和表结构不一致错误。

复制进程和 WireGuard 隧道仍然存活，但没有有效应用新记录，形成“进程在线、数据不复制”的假正常状态。

### 修复

1. 在中心库执行 `REFRESH PUBLICATION`，恢复 2026-08、2026-09 和 default 分区的订阅映射。
2. 从 192 本地库按 `source_id + event_id` 幂等回填中心缺失的 5,540 条原始帧。
3. 验证中心 API 已可查询到 2026-09-14 14:12:25 的数据，且 2026-09-14 的日报统计已恢复。

历史 `apply_error_count` 应保留用于审计；不能通过重置计数来掩盖故障。修复后仍需持续观察该计数和两端最新数据时间。

## 验证步骤

1. 在 192 发一条测试 DLT645 报文。
2. 确认 192 的 `iot_dlt645_frames` 出现新记录。
3. 检查源库 `pg_stat_replication` 为 `streaming`。
4. 检查 Monitor Center 的同一 `request_id` 已出现。
5. 临时断开 WireGuard，继续发送报文，再恢复连接，确认数据补齐且没有重复记录。
