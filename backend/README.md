# Matrix server-chat backend 0.4.9

Python/SQLite/Docker 后端，实现 MatrixChatBridge 协议 v2。

项目统一称安装在 Leaf/Paper 中的 Minecraft 插件为“插件端（前端）”，称本
Docker/Python 服务为“机器人后端（后端）”。Matrix Homeserver 是外部聊天
平台，“机器人账号”是后端控制的 Matrix 用户，二者都不与机器人后端混称。

## 持久化与服务器注册

`/data/bridge.db` 使用 SQLite WAL，保存 Matrix 登录会话、同步位置、绑定、
事件去重记录，以及以下服务器注册信息：

```text
server_id / display_name / token_hash / enabled / relay_group / last_seen
```

每台服务器必须使用自己的 256 位随机 token。推荐先在 Minecraft 游戏 OP 或
服务器控制台执行：

```text
/matrixbridge token generate
```

插件会自动写入 `config.yml` 并只向执行者显示 token。然后在后台使用隐藏输入
完成首次登记：

```bash
sudo docker compose exec matrix-serverchat \
  python -m app.admin add-server --id vanilla --name 原版服 \
  --group main --port 25565 --prompt-token
```

提示出现后粘贴游戏中生成的 token；输入不会回显或进入 shell 历史。数据库
只保存 SHA-256 哈希。轮换时再次在游戏执行生成命令，然后运行：

```bash
sudo docker compose exec matrix-serverchat \
  python -m app.admin set-token --id vanilla
```

查看或禁用服务器：

```bash
sudo docker compose exec matrix-serverchat python -m app.admin list-servers
sudo docker compose exec matrix-serverchat \
  python -m app.admin set-enabled --id vanilla --enabled false
```

把生成值分别写入对应插件的 `bridge.token`，并确保插件的
`bridge.server-id`、`bridge.server-name`、`bridge.relay-group` 与后台登记一致。

## 路由与防回环

同一 `relay_group` 的已启用服务器互相接收玩家聊天。后台发送结构化
`relay_message`，沿用来源事件的全局 ID，排除来源服务器并固定 `hop: 1`。
插件直接广播该消息，不模拟玩家聊天事件，因此不会二次上传。后台同时按
全局 ID 持久去重，并忽略机器人自己发出的 Matrix 事件。

当前单个 Matrix 房间由 `MATRIX_RELAY_GROUP` 指定连接哪个组，默认 `main`。

## 小游戏与在线榜

Matrix 房间和 Minecraft 聊天框均可输入 `#game help`。目前支持
`#game roll`（掷骰子）、`#game coin`（抛硬币）、`#game guess start`、
`#game guess <1–100>`、`#game guess status`。同一 `relay_group` 共用一局
15 分钟内有效的猜数字游戏；结果只向所在组广播，单人操作有 3 秒限速。
普通帮助和错误仅回复发起者。游戏状态暂存内存，机器人重启后未完成的局会结束。
聊天室中参与游戏的成员优先显示其已绑定游戏 ID；未绑定时显示聊天室昵称。

Matrix 房间输入 `#online` 或 `#在线榜`，后端向各已连接的服务器查询在线玩家，
最多等待 5 秒，生成 PNG 并在原指令消息列回复。未连接、超时及已禁用的服务
器也列出状态；同一用户每 15 秒只能查询一次。浅色 PNG 按服务器分组，
玩家按累计游玩时长降序排列，显示缓存头像、游戏 ID、时长和当前在线状态；
离线玩家也会进入排行。新插件仅在榜单查询时读取 Paper 持久保存的玩家
`PLAY_ONE_MINUTE` 统计，换算为累计秒数，插件或机器人重启不会清零。
每服最多展示前 60 名。旧插件仍可查询，但只能列出当前在线玩家且时长显示
为 `—`。Docker 镜像安装中文字体，保证图片在移动端可读。

## 多管理员

机器人采用双重鉴权。发言者的完整 Matrix ID 必须登记在 SQLite 管理员表中，
同时其房间权限等级必须达到 `ADMIN_POWER_LEVEL`（默认 `50`）。昵称、消息正文
和普通的账号绑定均不能授予管理权限。

首次部署或热修后必须从服务器控制台登记第一个 `owner`：

```bash
sudo docker compose exec matrix-serverchat \
  python -m app.admin add-admin \
  --matrix-id '@name:matrix.example.org' --role owner
```

查看及撤销身份：

```bash
sudo docker compose exec matrix-serverchat python -m app.admin list-admins
sudo docker compose exec matrix-serverchat \
  python -m app.admin remove-admin --matrix-id '@name:matrix.example.org'
```

`owner` 可以在聊天室维护多人名单；`admin` 只能管理服务器：

```text
!admin list
!admin add @helper:matrix.example.org admin
!admin add @owner2:matrix.example.org owner
!admin remove @helper:matrix.example.org
```

系统禁止删除或降级最后一名 `owner`。管理员身份和变更审计均持久化在 SQLite。
多人修改由 SQLite 事务串行处理。

```text
!server list
!server info vanilla
!server rename vanilla 新原版服
!server reset-name vanilla
!server port vanilla 25565
!server reset-port vanilla
!server group vanilla main
!server enable vanilla
!server disable vanilla
!server audit vanilla
```

同样支持 `#server` 和 `#admin`。整个 `server` 命令族（包括列表、详情和帮助）
都需要通过双重鉴权。token 永远不通过未加密的 Matrix 房间分发。

## 绑定模式与提及

`BINDING_MODE` 有两种配置：

- `scoped`（默认）：按服务器隔离，适合离线服或同名账号环境。
- `online_uuid`：按正版 UUID 全局绑定，所有服务器共用身份。

游戏内可使用 `@Steve`。若同一组中同名账号有歧义，使用
`@vanilla:Steve`。机器人向 Matrix 发送标准 `m.mentions`；Matrix 消息中被
提及用户的长 Matrix ID，在进入游戏前会替换为其玩家 ID。跨服 scoped 身份
会显示成 `@vanilla:Steve`，避免冒充目标服同名玩家。

Matrix 公共命令为 `!bind`/`#bind`、`!list`/`#list` 和
`!help`/`#help`。帮助内容会按发言者的机器人管理身份显示：普通成员只看到
公共命令，admin/owner 才看到对应管理用法。绑定验证码用可复制代码块显示。
未绑定提醒默认每 300 秒最多一次，可通过
`UNBOUND_REMINDER_COOLDOWN_SECONDS` 修改。

机器人对 `#`/`!` 命令的回复使用 Matrix `m.thread` 关系进入该命令的消息列，
避免管理结果占用房间主时间线。`#list` 的多服务器异步返回也会关联到同一消息
列；在线程内继续发送命令时沿用原线程。普通聊天和 Minecraft 事件仍发送到主
时间线。未绑定提醒也固定发送到主时间线，避免用户错过。不支持 Matrix 线程的
旧客户端可能按兼容回退方式显示回复。

## 玩家头像

Minecraft 玩家事件在 Matrix 正文开头嵌入一个 24×24 像素头像。机器人后端
按玩家 ID 异步查询 Mojang 公开档案和皮肤纹理，截取面部与帽子层生成 32×32
PNG，再上传到 Matrix 媒体库。Matrix 消息的纯文本 `body` 始终保留，因此不
支持正文图片的客户端仍能正常阅读消息。

头像索引和 Matrix `mxc://` 地址保存在 `/data/bridge.db` 的
`player_avatars` 表中，PNG 文件保存在 `/data/avatars/`。新玩家首次发言不会
等待外部网络：当次先显示内置 Steve 头像，后台完成获取后，后续消息自动使用
正版皮肤头像。查询失败时也使用 Steve；该玩家下一次产生事件时会再次尝试。
如果已有成功缓存，刷新失败不会覆盖旧头像。

机器人会在每周日北京时间 00:00 刷新已经见过的全部玩家。可通过以下环境变量
调整或关闭功能：

```yaml
AVATAR_ENABLED: "true"
AVATAR_DISPLAY_MODE: "text"
AVATAR_DIRECTORY: "/data/avatars"
AVATAR_LOOKUP_TIMEOUT_SECONDS: "8"
```

此功能只修改机器人后端，不改变插件协议，也不要求重启 Minecraft 服务端。

## 自动重连保护

机器人按 `server-id + 玩家 ID` 观察上线与下线事件。默认情况下，同一玩家在
10 分钟内出现两次持续 60 到 180 秒的短会话时，会被判断为可能陷入 AuthMe
未认证和自动重连循环。机器人随后持久化屏蔽该玩家的上线、下线动态，并在
Matrix 房间提醒其绑定用户；聊天、死亡、成就和绑定功能不受影响。
此外还有快速抖动检测：默认在 60 秒内出现三次不超过 30 秒的“上线→下线”
会话便会触发，因此几秒内反复手动断线重连同样会被识别。
屏蔽期间每次新的上线或下线都会刷新静默计时；连续 15 分钟没有新事件时，
机器人会自动解除屏蔽。机器人重启后也会继续依据数据库中的时间判断。

玩家回到电脑并完成登录后，可在 Matrix 房间输入：

```text
#reconnect clear
```

管理员可使用 `#reconnect list` 查看全部记录，并使用
`#reconnect clear <server-id> <玩家ID>` 代为解除。详细说明可通过
`#reconnect help` 查看。检测参数可以调整：

```yaml
RECONNECT_GUARD_ENABLED: "true"
RECONNECT_GUARD_WINDOW_SECONDS: "600"
RECONNECT_GUARD_MIN_SESSION_SECONDS: "60"
RECONNECT_GUARD_MAX_SESSION_SECONDS: "180"
RECONNECT_GUARD_THRESHOLD: "2"
RECONNECT_GUARD_RAPID_WINDOW_SECONDS: "60"
RECONNECT_GUARD_RAPID_MAX_SESSION_SECONDS: "30"
RECONNECT_GUARD_RAPID_THRESHOLD: "3"
RECONNECT_GUARD_QUIET_SECONDS: "900"
```

## 连接状态通知与实时检查

机器人会监视每台已启用 Minecraft 插件与后台之间的 WebSocket 连接。连接持续
异常 30 秒后在 Matrix 主时间线发送一次警告；连接重新稳定 30 秒后发送一次
恢复通知。同一服务器的状态通知至少间隔 60 秒，短暂闪断不会反复刷屏。
新版插件在游戏服正常关闭或重启前主动发送停服信号，机器人会立即在主时间线
通报一次；同一 `server-id` 完成认证、重新发送 `hello` 后，再通报一次重启
成功。正常停服不会同时触发“连接异常”告警。强制结束进程、断电或断网无法
提前发送停服信号，仍按上述延迟异常告警处理。

任何房间成员都可以发送 `#ping` 或 `!ping`，实时查看机器人、Matrix、WebSocket
监听端口及各服连接状态。后端会向每台已连接插件发查询：查询往返耗时显示为
“机器人↔插件”延迟，包含服务端主线程调度；插件同时报告每位在线玩家到
游戏服的 ping、1 分钟 TPS 与 MSPT。查询超过 3 秒则显示超时；尚未更新的
插件缺少指标时显示“未提供”，不会伪造 0 ms。这些都是本次采样，不等于
长期稳定率。可通过以下环境变量调整连接异常通知：

```yaml
SERVER_HEALTH_ENABLED: "true"
SERVER_HEALTH_GRACE_SECONDS: "30"
SERVER_HEALTH_MIN_NOTICE_INTERVAL_SECONDS: "60"
```

如果 `survival` 服每天早上 06:00 重启网络，可在 `.env` 中加上：

```dotenv
SERVER_HEALTH_QUIET_START=05:55
SERVER_HEALTH_QUIET_END=06:15
SERVER_HEALTH_QUIET_TIMEZONE=Asia/Shanghai
SERVER_HEALTH_QUIET_SERVER_ID=survival
```

运行 `sudo docker compose up -d --build matrix-serverchat` 使配置生效。
静默窗口每天重复，支持跨午夜。窗口内恢复的连接不会触发异常通知；窗口
结束时仍断开的连接会按原有规则告警。如果窗口开始前已经发出异常告警，
窗口内恢复时会把恢复通知延至窗口结束。自动重连、`#ping` 和插件主动发送
的正常停服/重启通知照常运行。开始和结束均留空时禁用静默窗口；服务器 ID
留空时作用于此后台的所有服务器。

本项目默认 WebSocket 地址为 `ws://127.0.0.1:8766/minecraft`。8765 不再由本
项目使用，以免与同机其他桥接机器人冲突。

Matrix 房间中的媒体进入游戏时使用简短占位文字，避免下载文件或把较长的
`mxc://` 地址发送给玩家：图片显示为 `[图片]`、视频为 `[视频]`、语音为
`[语音]`、文件为 `[文件]`，`m.sticker` 贴纸事件显示为 `[贴纸]`。发送者仍按
既有绑定规则显示为玩家 ID。

## 部署

首次部署先复制并填写公开配置模板；真实值保存在不会提交的 `.env` 中：

```bash
cd /opt/matrix-serverchat
cp .env.example .env
chmod 0600 .env
editor .env
```

```bash
cd /opt/matrix-serverchat
sudo bash install.sh
```

### 开机自动启动

生产服务器建议同时使用两层恢复机制：Compose 中的
`restart: unless-stopped` 负责容器进程异常退出后的自动拉起，systemd 负责主机
重启后等待 Docker 和网络就绪，再启动 `/opt/matrix-serverchat` 项目。仓库提供：

```powershell
sudo bash install-0.4.9.sh matrix-serverchat-backend-0.4.9.tar.gz
```

安装后常用命令：

```bash
systemctl status matrix-serverchat --no-pager
systemctl restart matrix-serverchat
systemctl is-enabled matrix-serverchat
cd /opt/matrix-serverchat && docker compose logs -f matrix-serverchat
```

该 unit 只操作 Compose 中的 `matrix-serverchat` 服务，不会清理孤儿容器，也不会
管理或停止占用 8765 的其他桥接机器人。

跨机器部署必须在后端前使用 TLS 反向代理并让插件连接 `wss://`；不要把明文
`ws://` 暴露到公网。升级时保留 `data/` 和 `secrets/matrix_password` 即可，
旧版绑定表会自动迁移为全局绑定。
