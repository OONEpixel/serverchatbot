# Matrix server-chat backend 0.4.0

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
AVATAR_DIRECTORY: "/data/avatars"
AVATAR_LOOKUP_TIMEOUT_SECONDS: "8"
```

此功能只修改机器人后端，不改变插件协议，也不要求重启 Minecraft 服务端。

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

跨机器部署必须在后端前使用 TLS 反向代理并让插件连接 `wss://`；不要把明文
`ws://` 暴露到公网。升级时保留 `data/` 和 `secrets/matrix_password` 即可，
旧版绑定表会自动迁移为全局绑定。
