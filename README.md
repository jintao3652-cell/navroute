# navroute · 航路解析服务

基于 Navigraph **DFD v2**（`ng_jeppesen_fwdfd_*.s3db`）的航路反向解析与航路查询服务。  
上传导航数据压缩包后，服务会**一次性预建索引**，之后每次航路解析都是纯内存查表：  
实测 **p50 0.09 ms / p99 0.19 ms**（含 SID/STAR 展开，单核约 1.1 万次/秒）。

| 能力     | 说明                                                         |
| ------ | ---------------------------------------------------------- |
| 航路反向解析 | 航路字符串 → 逐点经纬度（含航路自动展开、SID/STAR 展开、DCT、经纬度点）                |
| 航路查询   | 起降机场 → 建议航路（自动挑选 SID / STAR + 航路图 A* 最短路径，带单向限制感知）         |
| 终端程序   | SID / STAR / **APPROACH** 列表与逐段展开（跑道过渡 / 公共段 / 航路过渡 / 进近过渡 / 复飞，ARINC 424 route_type） |
| 航段表    | 逐段给出"怎么飞 + 圆弧圆心·半径·转向 + 高度/速度限制 + 段长"，`procedure` / `parseRoute` / `route` 三个接口**同源同构**；进近可直接写进航路串，或用 `route` 的 `iap` 接入 |
| 机场跑道   | `airport` / `runways` 单独取某机场全部跑道：跑道号、磁/真航向、长宽、道面、入口标高，以及装在跑道上的 **ILS**（识别、频率、航道、下滑角、类别、DME、指点标） |
| 多周期    | 同一套数据目录并存多个 AIRAC 周期，按 `cycle` 参数切换，LRU 控制常驻内存周期数          |
| 预缓存    | 上传时建好 SQLite 索引 + 内存索引快照（`index.pkl`），启动 ~180 ms 完成预热      |
| 运行方式   | Linux Docker（`python:3.11-slim` + uvicorn），自带健康检查与 Web 控制台 |

---

## 1. 快速开始

```bash
# 构建并启动
docker compose up -d --build

# 打开控制台
open http://localhost:8110        # Windows: start http://localhost:8110
```

第一次使用先上传数据包（Web 页面左上角拖拽，或命令行）：

```bash
curl -X POST http://localhost:8110/api/efb/upload \
     -F "file=@ng_jeppesen_fwdfd_2610.zip" \
     -F "overwrite=true" -F "token=admin123456"
```

上传返回前会自动完成重构建（载入内存 + 建图），新周期无需重启即可解析。

> 数据卷 `./data` 的权限由容器入口自动处理：容器以 root 启动，先把 `/data` 修正为  
> `NRS_UID:NRS_GID`（默认 `1000:1000`）再降权运行 uvicorn。宿主目录属于其他用户时，  
> 把 compose 里的 `NRS_UID/NRS_GID` 改成该用户的 `id -u` / `id -g` 即可，无需手动 chown。
>
> 若入口仍然报 `is not writable by uid 1000`（只读挂载、NFS root squash、SMB 共享等），  
> 说明该文件系统本身不支持写入或忽略 chown，请改用命名卷：  
> `docker run -v navroute_data:/data ...`，或在宿主机执行 `sudo chown -R 1000:1000 ./data`。

不使用 Docker 时：

```bash
python -m pip install -r requirements.txt
NRS_DATA_DIR=./data python -m uvicorn app.main:app --host 0.0.0.0 --port 8110
```

---

## 2. 数据与缓存设计

### 2.1 上传处理流水线（只做一次，决定后续速度）

```
zip 包
 ├─ 解包（防目录穿越，跳过 __MACOSX）
 ├─ 自动识别周期     cycle.json → cycle_info.txt → tbl_hdr_header → 文件名
 ├─ 建立 SQLite 索引  er/ea/pa/d/db/pc/pd/pe 共 8 个索引，消除全表扫描
 ├─ 构建内存索引     航路分链 + 航路点表 + 机场表  →  index.pkl（约 10 MB）
 ├─ 原子落盘         临时目录 → os.replace 到 data/cycles/<cycle>/
 └─ 自动重构建       卸载旧对象 → 载入新快照 → 重建航路图 → 立刻对外服务
```

实测（2610 周期，190 MB 数据库）：**索引建立 ~1.6 s，整体上传导入 ~13 s**（含 55 MB 上传与解压），  
上传后的自动重构建 **~0.8 s**（载入快照 200–360 ms + 建图 470–570 ms）。整个过程不需要重启服务。

### 2.1.1 上传即重构建（默认开启）

| 行为   | 说明                                                                                           |
| ---- | -------------------------------------------------------------------------------------------- |
| 触发   | `POST /api/efb/upload` 成功后自动执行，无需额外参数                                                        |
| 做什么  | `manager.unload(cycle)` 丢弃仍指向旧文件的实例 → `manager.get(cycle)` 重读新 `index.pkl` → `graph()` 重建航路图 |
| 效果   | 新周期在上传响应返回时已经常驻内存，首个 `parseRoute` / `route` 调用没有冷启动                                          |
| 开关   | 环境变量 `NRS_REBUILD_ON_UPLOAD=0` 全局关闭；请求带 `rebuild=false`（兼容旧参数 `preload=false`）仅跳过本次          |
| 手动补救 | `POST /api/efb/cycles/{cycle}/rebuild`，或首页周期列表上的 ⟳ 按钮                                        |
| 返回   | `data.rebuild = {loaded, graph, load_ms, graph_ms, rebuild_ms, airways, fixes, airports}`    |

覆盖同一周期（`overwrite=true`）时同样会重构建，因此换数据不用清库、不用重启。

### 2.2 目录结构

```
data/
├── registry.json                 周期清单 + 默认周期（原子写入）
├── cycles/
│   └── 2610/
│       ├── ng_jeppesen_fwdfd_2610.s3db   原始数据库（已加索引副本）
│       ├── index.pkl                     内存索引快照（pickle v4）
│       ├── meta.json                     周期元数据 + 统计
│       └── cycle.json / cycle_info.txt   原始元数据（若有）
└── tmp/                           上传/导入临时目录（自动清理）
```

### 2.3 内存结构

```python
airways[route_id]  -> [Chain, ...]            # 一条航路的所有物理链
Chain = (route, area, fixes, lats, lons, dirs)
aw_pos[route_id][fix] -> ((chain_idx, pos), ...)   # O(1) 判断"该点是否在该航路上"
fixes[ident]       -> ((lat, lon, kind), ...)      # E=航路点 D=VOR N=NDB R=航路引用 A=机场
airports[icao]     -> (lat, lon, name)
```

`/api/efb/route` 使用的有向图（约 4.8 万节点 / 9.3 万条边）在首次调用时构建（~360 ms）并常驻。

### 2.4 多周期与内存

- 每个周期独立目录、独立索引，互不影响。
- `CycleManager` 用 **LRU** 限制常驻周期数（默认 3，`NRS_MAX_LOADED_CYCLES`），淘汰时关闭 SQLite 连接。
- 请求不带 `cycle` 时使用默认周期：优先取"今天在有效期内"的周期，否则取最新。
- 单周期常驻内存实测约 **105 MB**（内存索引 71 MB + 航路图 32 MB），3 个周期常驻约 350 MB；  
  解析过程不再分配长期对象。

### 2.5 航路分链（保证"段"是连续的）

ER 表中同一 `route_identifier` 可能对应多个互不相连的地理分段（不同 `area_code`、  
同名航路的不同区段、粗编码与细编码并存）。分链规则：

1. 按 `(route_identifier, route_identifier_postfix, area_code)` 分组，按 `seqno` 升序；
2. 断链条件（任一满足即断开）：
   - `seqno` 重复；
   - 相邻两点大圆距离 > 2000 NM；
   - 记录带 `inbound_distance` 且几何距离 > 150 NM 且 > 5 × `inbound_distance`  
     （典型的数据缺口，如记录标注 23 NM 而实际跨 1251 NM）。

结果是 15 776 条链，覆盖 108 468 条航段。

---

## 3. API

统一响应结构：

```json
{ "code": 0, "data": {}, "message": "" }
```

`code = 0` 表示成功，其余为错误码（同时返回对应 HTTP 状态码：400 参数错误 / 401 token 缺失或错误 / 404 周期或机场不存在 / 500 内部错误）。  
所有 `POST` 接口同时接受 **query 参数**、**JSON body** 和 **form body**。

### 3.0 鉴权（POST / DELETE 必带 token）

所有写接口（`POST`、`DELETE`）都必须带 `token`，默认值 `admin123456`（环境变量 `NRS_TOKEN` 可改，  
设为空字符串可关闭校验）。`GET` 接口不需要 token。

三种传法任选其一：

```bash
# query
curl -X POST "http://localhost:8110/api/efb/parseRoute?token=admin123456" \
     -H "Content-Type: application/json" -d '{"cycle":"2610","route":"ZBAA VYK G212 SGM"}'

# JSON / form body 字段
curl -X POST http://localhost:8110/api/efb/parseRoute \
     -H "Content-Type: application/json" \
     -d '{"cycle":"2610","route":"ZBAA VYK G212 SGM","token":"admin123456"}'

# 请求头（x-token / x-auth-token / Authorization: Bearer）
curl -X POST http://localhost:8110/api/efb/route \
     -H "x-token: admin123456" -H "Content-Type: application/json" \
     -d '{"dep":"ZBAA","arr":"ZSPD"}'
```

缺失或错误时返回：

```json
HTTP 401
{ "code": 401, "data": {}, "message": "缺少或错误的 token" }
```

Web 页面已内置 token（浏览器 `localStorage.nrs_token`，默认 `admin123456`），  
遇到 401 会弹框让你输入并记住；也可在浏览器控制台执行  
`localStorage.setItem("nrs_token", "新token")` 后刷新。

### 3.0.1 Web 控制台登录（纯 Python，无 PHP）

HTML 控制台本身需要登录：默认账号 **admin / admin123456**。

| 项    | 说明                                                                             |
| ---- | ------------------------------------------------------------------------------ |
| 登录页  | `GET /login`（公开），未登录访问 `/` 或 `/static/*` 一律 303 跳转到 `?next=`                   |
| 登录接口 | `POST /api/efb/login`，字段 `name` + `psw`（也兼容 `username`/`password`）             |
| 会话   | 服务端内存随机会话 ID + `HttpOnly` `SameSite=Lax` 的 `nrs_sid` cookie，默认 12 小时滑动过期       |
| 退出   | `POST /api/efb/logout`（页面右上角「退出」），清除会话与 cookie                                 |
| 查询状态 | `GET /api/efb/session` → `{authenticated, user}`                               |
| 防爆破  | 同一 IP 连续失败 `NRS_LOGIN_MAX_FAILS`(8) 次后冷却 `NRS_LOGIN_LOCK_SECONDS`(60) 秒，返回 429 |
| 改密码  | 环境变量 `NRS_ADMIN_USER` / `NRS_ADMIN_PASS`；`NRS_SESSION_TTL` 改会话时长               |

JSON API 不受影响：仍然只用 `token` 鉴权，脚本/EFB 端无需登录。  
实现上没有任何 PHP、也没有模板引擎——登录页是一个静态 HTML，  
由 FastAPI 中间件按 cookie 放行，密码比较用 `hmac.compare_digest` 防时序攻击。

```bash
# 命令行验证会话流程
curl -c c.txt -X POST http://localhost:8110/api/efb/login -d 'name=admin&psw=admin123456'
curl -b c.txt -o /dev/null -w '%{http_code}\n' http://localhost:8110/          # 200
curl -b c.txt -X POST http://localhost:8110/api/efb/logout
curl -b c.txt -o /dev/null -w '%{http_code}\n' http://localhost:8110/          # 303 -> /login
```

### 3.1 航路反向解析

```
POST /api/efb/parseRoute?cycle=2610&route=ZBAA%20VYK%20G212%20SGM&token=admin123456
```

| 参数      | 必填 | 说明                                        |
| ------- | -- | ----------------------------------------- |
| `route` | 是  | 航路字符串（可含 SID / STAR / 进近程序名）            |
| `cycle` | 否  | 周期号，缺省用默认周期                               |
| `token` | 是  | 访问令牌（也可放 header）                          |
| `detail` | 否 | 默认 `1`：SID / STAR / 进近附带完整航段表；`0` 只返回程序概要（体积小） |

```json
{
  "code": 0,
  "data": {
    "cycle": "2610",
    "route": "ZBAA VYK G212 SGM",
    "warnings": ["航路 G212 的 VYK→SGM 段带单向限制标注，已按几何走向解析"],
    "waypoints": [
      { "name": "ZBAA", "lat": 40.07333333, "lon": 116.59833333 },
      { "name": "VYK",  "lat": 39.195,      "lon": 116.57166667 }
    ],
    "dep": "ZBAA", "arr": "ZGGG",
    "sid": {
      "name": "DOTR2G", "airport": "ZBAA", "runway": "18L", "transition": null,
      "waypoints": 8, "runways": ["18L","18R","19"], "transitions": [],
      "start": "DE18L", "end": "DOTRA", "distance_nm": 90.6,
      "leg_types": ["CF","DF","IF","TF"],
      "segments": [
        { "path": "CF", "path_name": "Course to fix", "path_cn": "航向至定位点",
          "name": "AA231", "lat": 40.12, "lon": 116.55, "role": "runway",
          "transition": "18L", "open": false,
          "instruction": "沿 181°M 航向飞至 AA231，13.2 NM",
          "course_text": "181°M", "course": 181.0, "course_ref": "M", "turn": "",
          "arc": null, "altitude": "≥ 4930 ft", "altitude_code": "+",
          "speed": "", "speed_code": "", "vertical": null, "rnp": null,
          "length_nm": 13.2, "dme": "", "rho": null, "theta": null }
      ],
      "track": [[40.0712, 116.5713], [40.1214, 116.5512]]
    },
    "star": { "…": "与 sid 完全同构" },
    "iap": {
      "name": "I02LY", "airport": "ZGGG", "runway": "02L", "transition": null,
      "approach": "ILS", "approach_name": "仪表着陆", "final_types": ["ILS"],
      "waypoints": 3, "start": "CI02L", "end": "RW02L", "distance_nm": 12.4,
      "approach_end": { "name": "RW02L", "lat": 23.39, "lon": 113.3, "role": "final" },
      "missed": 3, "leg_types": ["CF","IF"],
      "segments": [ { "path": "IF", "…": "与 procedure 返回的航段完全同构" } ],
      "track": [[23.1, 113.2], [23.3, 113.3]],
      "missed_track": [[23.39, 113.3], [23.5, 113.1]],
      "missed_waypoints": [ { "name": "D017L", "lat": 23.5, "lon": 113.1,
                              "role": "missed", "path": "TF" } ]
    },
    "took_ms": 0.95
  },
  "message": ""
}
```

`warnings` 是**求解过程说明**，不影响结果可用性；出现无法解析的元素时原始点不会被臆造。  
`dep` / `arr` / `sid` / `star` / `iap` 为附加信息（没有对应程序时为 `null`），  
`runway` 是实际采用的跑道过渡，`transition` 是实际采用的航路过渡。

**进近可以写进航路字符串**，三种写法等价：

```
ZBAA VYK G212 SGM ZGGG I02LY      航路 → 机场 → 进近
ZBAA VYK G212 SGM ZGGG/02L I02LY  带跑道
ZBAA VYK G212 SGM APP I02LY ZGGG  显式 APP 关键字（IAP / APCH / APR 同义）
```

* 进近只在**到达机场**上匹配；`waypoints` 到**跑道**为止，复飞不进 `waypoints`
  （`iap.missed_waypoints` / `iap.missed_track` 单独给出）。
* 名字里带连字符的进近（`R01-Y` / `VOR-A`）会被正确还原，不会被拆成两个航路元素。
* 进近名与航路名/小机场代码撞名时（`R14` 既是航路也是进近、`I06` 既是机场也是进近），
  只要紧跟在机场后面且该机场确有同名进近，就按进近解释。
* 进近解析不出时**不会臆造**：机场仍然识别，元素进 `warnings`。

`sid` / `star` / `iap` 里的 `segments`、`track` 与 `GET /api/efb/procedure` 返回的**是同一个对象**
（都由 `app/legtext.py::segments()` 生成），逐字段完全相同：
`instruction`（怎么飞）、`arc`（RF/AF 的圆心·半径·转向·弧度）、`altitude` / `altitude_code`、
`speed` / `speed_code`、`vertical`、`rnp`、`length_nm`、`course_text`、`dme` / `rho` / `theta`、
`role`、`open`、`path` / `path_cn`。所以**只调 `parseRoute` 的客户端也能拿到航段表**，
不必再对每条程序补一次 `procedure` 请求（`detail=0` 可退回到"只有点位"的轻量返回）。

### 3.2 航路查询

```
POST /api/efb/route?dep=ZBAA&arr=ZGGG&cycle=2610
```

| 参数                          | 必填 | 说明                          |
| --------------------------- | -- | --------------------------- |
| `dep` / `arr`               | 是  | 起降机场 ICAO                   |
| `cycle`                     | 否  | 周期号                         |
| `sid` / `star`              | 否  | 指定离场 / 进场程序名；缺省自动挑选         |
| `iap`                       | 否  | 指定进近程序（`I02LY` / `R34L`）；给了就接到航路末端，见下 |
| `dep_runway` / `arr_runway` | 否  | 指定跑道（`18L`、`RW18L`、`18` 均可） |
| `procedures`                | 否  | `0` 关闭 SID/STAR 接入，只走 ER 航路 |
| `detail`                    | 否  | 默认 `1`：`sid` / `star` / `iap` 附带完整航段表（`segments` / `track`），`0` 只给概要 |

```json
{
  "code": 0,
  "data": {
    "cycle": "2610", "dep": "ZBAA", "arr": "ZGGG",
    "route": "ZBAA DOTR2G B334 HUR B339 SOTMU G212 ... I02LY ZGGG",
    "distance_nm": 1210.9,
    "waypoints": [ { "name": "ZBAA", "lat": 40.07, "lon": 116.59 } ],
    "sid":  { "name": "DOTR2G", "runway": "18L", "transition": null,
              "waypoints": 8, "distance_nm": 90.6,
              "leg_types": ["CF","DF","IF","TF"],
              "segments": [ { "path": "CF", "instruction": "沿 181°M 航向飞至 AA231，13.2 NM",
                              "arc": null, "altitude": "≥ 4930 ft", "speed": "", "…": "…" } ],
              "track": [[40.0712, 116.5713], [40.1214, 116.5512]] },
    "star": null,
    "iap": { "name": "I02LY", "type": "iap", "runway": "02L", "transition": null,
             "approach": "ILS", "waypoints": 3, "distance_nm": 12.4,
             "approach_end": { "name": "RW02L", "lat": 23.39, "lon": 113.3, "role": "final" },
             "missed": 3, "leg_types": ["CF","IF"],
             "segments": [ { "path": "IF", "…": "与 procedure 完全同构" } ],
             "track": [ "…" ], "missed_track": [ "…" ], "missed_waypoints": [ "…" ] },
    "warnings": [], "took_ms": 3.5
  }
}
```

`sid` / `star` / `iap` 的 `segments` / `track` 与 `parseRoute`、`procedure` 三个接口**完全同源同构**，
一次查询即可拿到"怎么飞 + 圆心半径转向 + 高度速度限制 + 段长"的完整航段表。

**`iap` 参数**（`approach` / `app_proc` 同义）指定进近后：

* 航路终点从机场参考点改为**进近的 IAF**，A\* 直接把航路接到进近入口，
  输出"跑道 → SID → 航路 → 进近 → 跑道"，`waypoints` 以跑道结束（不再追加机场点）；
* `iap` 字段给出该进近的完整详情（`segments` / `track` / `approach_end` / `missed_waypoints`）；
* **此时不再自动接 STAR**（进近接管终端段）；同时给了 `star` 会提示已跳过；
* 进近名不存在时**不会报错**，退回 STAR 并在 `warnings` 说明，便于容错调用。

算法：把**每个可用的 SID 变体**（程序 × 跑道 × 航路过渡）展开成候选起点，  
把**每个可用的 STAR 变体**（或指定进近的 IAF）展开成候选终点，然后做多源多汇 A*  
（代价 = 程序航段长度 + 接入航路图的连接距离 + 航路图路径 + 进场/进近航段长度）。  
这样返回的航路是"跑道 → SID → 航路 → STAR → 跑道"（或"… → 进近 → 跑道"）的完整可飞航路，  
而不是只连到机场参考点。优先遵守 `direction_restriction`；无解时自动放宽并在 `warnings` 中说明。

控制台的「航路解析 / 航路查询」结果页在点位表下方，按 SID / STAR / 进近各给一个**可折叠的航段表**，
内容就是接口返回的 `segments`；「航路查询」多了「进近程序（可选）」输入框，对应 `iap` 参数。

### 3.3 终端程序（SID / STAR / APPROACH）

```
GET /api/efb/procedures?cycle=2610&airport=ZBAA&type=all
```

| 参数        | 必填 | 说明                                                             |
| --------- | -- | -------------------------------------------------------------- |
| `airport` | 是  | 机场 ICAO                                                        |
| `type`    | 否  | `all`（默认，= SID + STAR + APPROACH）/ `sid` / `star` / `iap` / `sid,star` |
| `runway`  | 否  | 只看某条跑道可用的程序（`18L` / `RW18L`）                                   |
| `approach` | 否 | 只看某种进近方式（`ILS` / `RNAV` / `VOR/DME` …，仅 `type=iap` 时生效）        |
| `cycle`   | 否  | 周期号                                                            |

`type` 接受别名：`approach` / `app` / `apr` → `iap`，`dep`/`departure` → `sid`，`arr`/`arrival` → `star`，
也接受逗号/加号组合（`sid+star`）。

返回机场跑道列表 + 每条程序的跑道、过渡、编码族（`rnav` / `conventional` / `fms` …）、  
航路点数量与首末点，便于前端做选择器；进近程序额外给出 `approach`（`ILS` / `RNAV` / `VOR/DME` …）
与 `approach_name`：

```json
{
  "code": 0,
  "data": {
    "airport": "ZBAA",
    "types": ["sid", "star", "iap"],
    "runways": [ { "designator": "18L", "lat": 40.08945278, "lon": 116.59482222,
                   "heading_mag": 181.0, "heading_true": 173.0,
                   "length_ft": 12467, "length_m": 3800, "surface": "ASPH",
                   "elevation_ft": 110, "ils": ["IOR"], "ils_frequency": [109.3] } ],
    "sid": [ { "name": "DOTR2G", "type": "sid", "runways": ["18L","18R","19"],
               "transitions": [], "families": ["rnav"],
               "waypoints": 8, "first": "DE18L", "last": "DOTRA" } ],
    "star": [ /* 同结构 */ ],
    "iap": [ { "name": "I18LY", "type": "iap", "approach": "ILS", "approach_name": "仪表着陆",
               "runways": ["18L"], "transitions": ["AA122","AA123"],
               "waypoints": 7, "first": "CI18L", "last": "RW18L",
               "leg_types": ["CF","DF","IF"] } ],
    "took_ms": 16.8
  }
}
```

```
GET /api/efb/procedure?cycle=2610&airport=ZBAA&type=sid&name=DOTR2G&runway=18L&transition=DOTRA
GET /api/efb/procedure?cycle=2610&airport=ZSPD&type=iap&name=R34L&transition=PD401
```

| 参数                      | 必填 | 说明                                       |
| ----------------------- | -- | ---------------------------------------- |
| `airport` / `name`      | 是  | 机场与程序名                                   |
| `type`                  | 否  | `sid`（默认）/ `star` / `iap`                |
| `runway` / `transition` | 否  | 指定跑道与过渡（进近的 `transition` 是起始进近点 IAF） |

返回逐段几何，`role` 标明该点属于 `runway` / `common` / `enroute` / `transition`（进近过渡）/
`final`（最终进近）/ `missed`（复飞）哪一段，
`path` 是 ARINC 路径终止符（`IF`/`TF`/`RF`/`DF`…）：

```json
{
  "code": 0,
  "data": {
    "name": "DOTR2G", "type": "sid", "airport": "ZBAA",
    "runway": "18L", "transition": null,
    "runways": ["18L","18R","19"], "transitions": [], "families": ["rnav"],
    "warnings": [],
    "leg_types": ["CF", "DF", "TF"],
    "waypoints": [
      { "name": "DE18L", "lat": 40.07, "lon": 116.57, "role": "runway",  "path": "DF" },
      { "name": "AA231", "lat": 40.12, "lon": 116.55, "role": "runway",  "path": "CF" }
    ],
    "segments": [
      { "path": "RF", "path_name": "Radius to fix", "path_cn": "定半径转弯至定位点",
        "name": "AK622", "lat": 32.3079, "lon": 79.9866, "role": "final", "open": false,
        "instruction": "以 AKC14 为圆心、半径 6.5 NM，左转 12° 画弧至 AK622",
        "course_text": "156°M", "course": 156.0, "course_ref": "M", "turn": "L",
        "arc": { "center_name": "AKC14", "center_lat": 32.25725, "center_lon": 80.09972,
                 "radius_nm": 6.5, "sweep_deg": -12.2, "turn": "L", "kind": "RF" },
        "altitude": "17700 ft", "altitude_code": "@", "speed": "≤ 160 kt",
        "speed_code": "-", "vertical": null, "rnp": 0.3,
        "length_nm": 1.4, "dme": "", "rho": null, "theta": null },
      { "path": "CA", "path_name": "Course to altitude", "path_cn": "航向至高度",
        "name": "CA", "lat": null, "lon": null, "role": "runway", "open": true,
        "instruction": "沿 51°M 航迹爬升至 ≥ 490 ft",
        "course_text": "51°M", "altitude": "≥ 490 ft", "speed": "", "arc": null }
    ],
    "track": [[40.0712, 116.5713], [40.0788, 116.5666], [40.1214, 116.5512]],
    "took_ms": 0.03
  }
}
```

* `waypoints` — 只含**有坐标**的定位点（保持旧版结构，向后兼容）。
  进近程序在这里**只到跑道**：G/A 的点不会混进来。
* `approach_end` — 仅进近：程序的终点定位点（`{"name":"RW15","lat":…,"lon":…,"role":"final"}`），
  前端据此在地图上打"终点"标记。
* `missed_track` / `missed_waypoints` — 仅进近：复飞折线与复飞定位点（从跑道起算）。
  `missed` 是复飞定位点数量。**进近程序以跑道为终点，复飞是并列的独立序列。**
* `segments` — 完整 ARINC 424 航段序列，含 `CA`/`VA`/`CI`/`VI`/`CD`/`VD`/`CR`/`VR`
  这些终点不是定位点的**开放航段**（`open: true`，给出航向、高度、DME 距离而不是编造坐标）。
  进近的复飞段也在其中，`role = "missed"`；`transition` 是该段所属的过渡标识（公共段为 `""`）。
  这一列**与 `parseRoute` / `route` 返回的 `sid.segments` / `star.segments` 是同一个生成器**
  （`app/legtext.py::segments()`），三处字段逐字相同。
  每条航段还带**可读说明**与几何参数（`app/legtext.py` 生成）：
  - `instruction` — 一句话说清怎么飞，例如
    `以 AKC14 为圆心、半径 6.5 NM，左转 12° 画弧至 AK622`、
    `沿 24°M 航向飞至 FI02L，3.6 NM`、`在 AK966 等待，右转，出航 4 NM`。
  - `arc` — 仅 `RF`/`AF`：`center_name` / `center_lat` / `center_lon`（转弯圆心，
    RF 为编码圆心点、AF 为 DME 台）、`radius_nm`（半径）、`sweep_deg`（圆心夹角，
    左转为负）、`turn`。**这是"该绕哪个点、怎么转"的权威字段**。
  - 高度：`altitude`（`≥ 17700 ft` / `≤ 3000 ft` / `11000 – 6500 ft`）、`altitude_code`
    （ARINC 原始码 `+ - @ B`，`G H I J` 为 DFD 扩展码，按公布值显示）、`vertical`
    （编码垂直角，-3.0 即 3° 下降剖面）。
  - 速度：`speed`（`≤ 210 kt`）、`speed_code`（`+ - @`）；`rnp` 为该航段 RNP 值。
  - 其他：`course_text`（`253°M`，磁/真由 `course_ref` 给出）、`length_nm`（与上一
    定位点的大圆距离）、`rho`（DME 距离）、`theta`（径向）、`path_cn`（终止符中文名）。
    直飞类航段没有 `RF`/`AF` 的圆心概念，`arc` 为 `null`。
* `track` — 按航段类型加密后的折线 `[[lat, lon], …]`，`RF`/`AF` 是真实圆弧采样，
  等待（`HM`/`HF`/`HA`）与程序转弯（`PI`）画成跑道形/反转弯轨迹，可直接用于画图。
* `leg_types` — 该程序实际用到的终止符集合（可据此判断是否含 RNP AR 的 `RF`）。

### 3.3.1 机场 / 跑道 / ILS（单独取）

```
GET /api/efb/airport?cycle=2610&airport=ZBAA
GET /api/efb/runways?cycle=2610&airport=ZBAA&designator=18L
```

| 参数                 | 必填 | 说明                                                           |
| ------------------ | -- | ------------------------------------------------------------ |
| `airport`          | 是  | 机场 ICAO（也接受 `icao` / `apt`）                                  |
| `designator`       | 否  | 只取一条跑道（`18L` / `RW18L`），不存在时返回 404                            |
| `procedures`       | 否  | `airport` 接口默认带 SID/STAR/APPROACH 的名字清单，`0` 可关掉让响应更小          |
| `ils`              | 否  | `runways` 接口默认带 ILS 明细，`0` 只留跑道本体                             |
| `cycle`            | 否  | 周期号                                                          |

`airport` 返回机场元数据 + 每条跑道的物理数据 + 装在跑道上的 ILS + 三种程序的名字清单；
`runways` 只返回跑道与 ILS，载荷约小一半：

```json
{
  "code": 0,
  "data": {
    "airport": "ZBAA", "name": "CAPITAL", "city": "BEIJING", "country": "CHINA",
    "iata": "PEK", "elevation_ft": 116, "magnetic_variation": -8.0,
    "transition_altitude_ft": 9850, "speed_limit_kt": 250, "speed_limit_altitude_ft": 10000,
    "counts": { "runways": 6, "landing_directions": 6, "runways_with_ils": 6, "ils": 6 },
    "runways": [
      {
        "designator": "18L", "ident": "RW18L", "kind": "runway",
        "lat": 40.08945278, "lon": 116.59482222,
        "heading_mag": 181.0, "heading_true": 173.0,
        "length_ft": 12467, "length_m": 3800, "width_ft": 197, "width_m": 60,
        "surface": "ASPH", "surface_text": "asphalt",
        "elevation_ft": 110, "displaced_threshold_ft": 0,
        "threshold_crossing_height_ft": 53, "gradient_pct": -0.076,
        "traffic_pattern": "L", "traffic_pattern_text": "left",
        "lights": true, "part_time_lights": false, "llz_ident": "IOR",
        "ils": [
          {
            "runway": "18L", "designator": "18L", "ident": "IOR", "name": "CAPITAL",
            "type": "ILS", "category": "CAT I", "frequency_mhz": 109.3,
            "course_mag": 181.0, "course_true": 173.0,
            "localizer": { "lat": 40.052542, "lon": 116.600633,
                           "width_deg": 2.96, "distance_from_threshold_ft": 1073 },
            "glideslope": { "angle_deg": 3.0, "beam_width_deg": 1.44, "elevation_ft": 125,
                            "lat": 40.086736, "lon": 116.593769,
                            "distance_from_threshold_ft": 945 },
            "declination": -8.0, "dme": true, "markers": []
          }
        ]
      }
    ],
    "ils": [ /* 与上面嵌套的 ILS 同构，按跑道摊平，方便直接遍历 */ ],
    "procedures": { "sid": ["DOTR2G", "…"], "star": ["ENVIP1", "…"], "iap": ["I18LY", "…"] },
    "procedure_counts": { "sid": 40, "star": 17, "iap": 26 },
    "took_ms": 0.4
  }
}
```

要点：

* **单位**：`*_ft` 是源数据原值（英尺），`*_m` 是换算值；`length_ft` 为跑道全长。
* **航向**：`heading_mag` 是跑道入口的磁方位（跑道号 × 10 即其近似值），`heading_true` 为真方位。
  少数小机场源数据未编航向，字段为 `null`，**不做推算**。
* **ILS**：`course_mag` / `course_true` 是航道方位，与跑道航向一致；`category` 由源编码 `1/2/3`
  映射为 `CAT I/II/III`（`0` 或 MLS/GLS 的其他编码原样返回）；`type` 在有下滑台时为 `ILS`，
  只有航向台时为 `LOC`；`dme` 表示该 ILS 与测距台并置；`markers` 是 OM/MM/IM 指点标
  （`{type, ident, lat, lon}`，没有则为 `[]`）。
* **没有 ILS 的跑道**照样返回，`ils: []`，不会因为缺盲降就少一条跑道。
  直升机坪 / 水上跑道设计符不是数字形式，`kind` 为 `other`。
* 数据来源全部是 DFD 原始表（`tbl_pa_airports` / `tbl_pg_runways` /
  `tbl_pi_localizers_glideslopes` / `tbl_pm_localizer_marker` / `tbl_d_vhfnavaids`），
  机场级 LRU 缓存，冷态约 0.5 ms；该缓存与 SID/STAR 程序共用 `NRS_PROC_CACHE` 预算（默认 512 个机场）。

`/api/efb/procedures` 里的 `runways` 也换成了同样的跑道摘要（`designator` / `lat` / `lon` /
`heading_mag` / `length_ft` / `ils` 的 `ident` 列表），选跑道时不用再查一次。

### 3.4 上传导航数据

```
POST /api/efb/upload            (multipart/form-data)
  file=<zip|s3db>  overwrite=true|false  rebuild=true|false
```

压缩包可以只有 `*.s3db`，也可以带 `cycle.json` / `cycle_info.txt`（推荐）。  
返回导入结果：周期、修订号、有效期、数据来源、统计与耗时，以及重构建结果  
`rebuild = {loaded, graph, load_ms, graph_ms, rebuild_ms, ...}`。


|  |  |
| --- | --- |
上传成功后**默认自动重构建**（载入内存 + 建航路图），旧参数 `preload=true|false` 仍然兼容；
传 `rebuild=false` 可只落盘不改内存。

### 3.5 周期管理

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/api/efb/cycles` | 列出全部周期（含是否在内存、是否默认） |
| `GET` | `/api/efb/cycles/{cycle}` | 单周期详情 |
| `DELETE` | `/api/efb/cycles/{cycle}` | 删除周期（磁盘 + 注册表 + 内存） |
| `POST` | `/api/efb/cycles/{cycle}/rebuild` | 重构建（重读 index.pkl + 重建航路图），约 0.8 s |
| `POST` | `/api/efb/cycles/{cycle}/unload` | 仅从内存卸载 |
| `GET` | `/api/efb/health` | 健康检查（含默认周期、已加载周期、运行时长） |

交互式文档：`/docs`。

---

## 4. 解析规则

### 4.1 词法

|  |  |
| --- | --- |
* 分隔符：空白、`,` `;` `-` `>` `|` `..` `...` `/`（`/` 单独保留，用于 `ZBAA/18L`）。
* 关键字：`DCT` `SID` `STAR` `IAP` `DIRECT` `APCH` `APP` `RNAV`（不产生点）。
* 首 token 为机场 → 起点；末尾（允许带 `/跑道`）为机场 → 终点。
* 航路标识中的连字符自动还原（`FANS-2` 不会被拆成两段）。
* ICAO 速度/高度标注在分词前剥离，不当作航路元素、也不产生警告：
  `VIDEK/K0919S1130`（919 km/h、公制高度层 11300 m）、`NIGOR/K0887F380`（887 km/h、FL380）、
  `ORTUS/M080F370`（M.80、FL370）、`VIDEK/FL380`、`VIDEK/N0450F350`。
  速度字母 `K`=km/h、`N`=节、`M`=马赫；高度字母 `F`=高度层、`A`=百英尺、`S`=公制、`V`=VFR，
  也接受 `FL` 写法。SimBrief 飞越中国/俄罗斯等公制高度层空域时会自动加这种后缀。

### 4.2 token 分类优先级

`跑道 → 中途机场 → SID/STAR → 航路 → 航路点/导航台 → 经纬度点 → 警告`

中途机场判定带 `not is_airway` 保护，避免把 `Y31`（既是美国机场又是航路）误判为机场。

### 4.3 航路展开

给定 `进入点 AWY 退出点`：

1. 在 `aw_pos[AWY]` 中查找两点所在链；同一条航路上有多个候选段时按
   **(入口距离最近, 中间点最多, 路径最短)** 打分 —— 这样"粗编码 + 细编码"并存时优先取细编码。
2. 正向/反向由链内下标决定，逐段检查 `direction_restriction`（`F` 只允许正向、`B` 只允许反向）。
3. 全部候选都被方向限制否决时，退回几何最优段并在 `warnings` 中提示（数据里同一航路常被拆成
   两段单向编码，硬拒绝会让大量合法航路无法解析）。
4. 进入点不在航路上（如 `ZBAA A461 ...`）时，在 300 NM 内寻找链端点切入并给出提示。
5. 两点不在同一链上 → 明确告警，绝不跨缺口拼接。

### 4.4 SID / STAR 展开

DFD 把一条终端程序拆成若干 `route_type` 记录，先按 **编码族 + 段落角色** 分组，再按 ARINC 424 顺序拼接：

| `route_type` | SID | STAR |
|---|---|---|
| 0 | engine-out（单序列） | — |
| 1 / 2 / 3 | 跑道过渡 / 公共段 / 航路过渡 | 航路过渡 / 公共段 / 跑道过渡 |
| 4 / 5 / 6 | RNAV 同上 | RNAV 同上 |
| 7 / 8 / 9 | — | 剖面下降 同上 |
| F / M / S | FMS 同上 | FMS 同上 |
| T / V | 向量化 SID 跑道 / 航路过渡 | — |
| H | RNP/直升机单序列（厂商扩展） | 同左 |

拼接顺序固定为 **SID: 跑道 → 公共 → 航路**，**STAR: 航路 → 公共 → 跑道**。五条关键规则：

1. **不跨族拼接。** 同一条程序常同时存在 `1/2/3`（常规）与 `4/5/6`（RNAV）两套编码；
   混拼会得到任何航图上都不存在的路径。族由请求的跑道 / 过渡是否存在于该族决定，同分时按
   RNAV > 剖面下降 > FMS > 常规 > 向量 > RNP 取优。
2. **跑道过渡只在能确定跑道时使用。** 未给出跑道且各跑道过渡几何互不相同 → 丢弃该段并告警
   （HMM 无法凭空选择一条跑道）；若各跑道过渡完全一致（例如 `ENVIP1` 的 5 条），则直接采用。
3. **航路过渡由上下文决定。** SID 用它**后面**那个点（放行点）匹配过渡名或过渡首点；STAR 用它
   **前面**那个点。只有一个过渡时直接采用。
4. **无定位点的航段不产生点。** `CA`/`VA`/`CI`/`VI`/`VM`/`FM`/`CD` 等"航向/上升到高度"记录
   没有坐标，跳过即可 —— 后续航段从同一位置继续。`waypoint_description_code` 以 `G`（跑道作为航路点）
   开头的记录不当作普通航路点。
5. **跑道入口补点。** 纯向量 SID 整条程序没有任何定位点，向量 STAR 也常在离机场几十海里处结束。
   此时用 `tbl_pg_runways` 的跑道入口坐标补一个 `RWxx` 点（仅当首/末点距入口 > 3 NM），
   让航迹闭合到跑道；这是数据库里的真实坐标，不是推算值。

自测覆盖：2610 周期 **3354 个机场 / 67 762 条程序 / 86 787 次展开**，
0 条空展开、0 条 > 400 NM 的异常段、SID 起点与 STAR 终点全部落在机场 40 NM 内。

### 4.4.1 进近程序（APPROACH / IAP）

进近程序在 `tbl_pf_iaps`（2610 周期 392 734 行 / 7 474 机场 / 32 048 条程序），
`route_type` 的含义与 SID/STAR 完全不同：**`A` 是进近过渡，其余字母是最终进近所用的导航设施**，
而且这个字母就是程序名的首字母（`I02LZ` → ILS，`R16` → RNAV，`D20R` → VOR/DME）：

| route_type | 进近方式 | route_type | 进近方式 |
|---|---|---|---|
| `A` | 进近过渡（IAF） | `N` / `Q` | NDB / NDB+DME |
| `B` | 航向道/反航道 | `P` | GPS |
| `D` / `V` / `S` | VOR/DME / VOR / VOR(DME,VORTAC) | `R` / `H` | RNAV / RNP |
| `F` | FMS | `T` | TACAN |
| `G` / `J` | IGS / GLS | `U` / `W` | SDF / MLS |
| `I` / `L` / `X` | ILS / LOC / LDA | `M` / `Y` | MLS / MLS-B,C |

拼接顺序固定为 **进近过渡 → 最终进近 → 复飞**（与 STAR 相反：过渡在前、跑道段在后），
三条关键规则：

1. **一个 family。** 全部记录归入同一个 `approach` 编码族，因此"过渡 + 最终进近"是合法组合；
   实测只有 1 条程序（`ZSWY R03-Y`）的最终段出现两个 `route_type`，按 `transition_identifier`
   分开保存，不会拼出混合路径。
2. **不选过渡 = 只用最终进近。** 这与真实 EFB 一致（等价于雷达引导截获），
   过渡列表仍完整返回给前端选择；指定过渡时才把 IAF 段接在前面。
3. **复飞段保留但单独成段。** 跑道入口之后的航段 `role = "missed"`，进近在这里**结束**：
   - `waypoints` / `track` 只到跑道（`approach_end` 给出终点航路点），
   - 复飞另给 `missed_waypoints` / `missed_track`，从跑道起画，
   - 前端用蓝色实线画进近、粉色虚线画 G/A，终点标在跑道上（跑道符号 + `RW15 · 终点`），
     因此 G/A 的最后一个点（等待/重入点）永远不会被当成程序终点。
   跑道由 `RWxx` 定位点识别，**只有程序完全没有跑道点时**才用 `tbl_pg_runways` 补入口，
   且绝不把入口补到复飞之后。

进近也可以直接写进航路字符串（`ZBAA … ZGGG I02LY`、`ZBAA … ZGGG/02L I02LY`、
`ZBAA … APP I02LY ZGGG`），或用 `route` 的 `iap` 参数接入，见 3.1 / 3.2。

全量自测（`tests/check_iaps.py`，2610 周期）：**7 474 个机场 / 32 048 条进近 / 122 553 次展开**，
0 条空展开、0 条缺最终进近段、0 条复飞段排在跑道之前；单次展开 0.13 ms。
其中 1 233 条是字母后缀的盘旋进近（`VOR-A`、`LOC-B`、`RNP-W` …，本来就没有跑道编号），
另有 5 条源数据本身的长航段（如 `ZWHZ R09` 的 `HZ601` 被编到 1 436 NM 外），
引擎会给出 `warnings` 提示，不改写数据。

### 4.5 航路点消歧

同一标识符在数据库中常有多个坐标（例如 `RJK` 在克罗地亚，而国内航线另有同名点）。
消歧顺序：**离上一点最近** → 类型优先级 `航路点 > VOR > NDB > 航路引用 > 机场 > 终端点`。
终端点（`tbl_pc_terminal_waypoints`）按需从 SQLite 查询并做 LRU 缓存，不占常驻内存。

### 4.6 ARINC 424 航段（路径终止符）

SID/STAR/APPROACH 的每一行都是一条 ARINC 424 航段，`path_termination` 决定它的几何形状。
几何计算见 `app/legs.py`，逐段文字说明见 `app/legtext.py`，24 个终止符全部识别：

| 类别 | 终止符 | 处理方式 |
|---|---|---|
| 点到点 | `IF` `TF` `CF` `DF` | 大圆直线连到该定位点（`CF` 用编码航向，终点仍是该点） |
| 定半径圆弧 | `RF` | 用 `center_waypoint` + `arc_radius` + `turn_direction` 采样真实圆弧（RNP AR 核心） |
| DME 圆弧 | `AF` | 用 `recommended_navaid` + `rho` 作圆心/半径采样圆弧 |
| 等待 | `HM` `HF` `HA` | 以 `course` + 转弯方向 + `distance_time` 画跑道形等待轨迹，末点回到定位点 |
| 程序转弯 | `PI` | 出航 + 180° 反转 + 回航的简化轨迹 |
| 定位点至… | `FC` `FD` `FA` `FM` | 保留定位点与编码航向/高度/DME |
| 开放航段 | `CA` `VA` `CI` `VI` `CD` `VD` `CR` `VR` `VM` | 数据本身没有坐标（雷达引导段），以 `open: true` 报告航向/高度/DME，**不编造坐标** |

数据集实测覆盖（2610 周期，SID 235 429 行 / STAR 206 580 行 / APPROACH 392 734 行）：

```
TF 246108  DF 32295  CF 33043  IF 63569  CA 21069  VA 8568  CI 7171  VI 5218
VM 6918  CD 3854  RF 2449  AF 4436  FD 1551  FA 1091  VD 664  FC 39  CR 423
VR 188  HM 16  HF 12  HA 145
```

规则与坑：

* `RF` 的圆心若与两端点距离和 `arc_radius` 偏差超过 `max(3 NM, 35%)`，判定为数据不自洽，
  退回直线并在 `track` 中体现——宁可直飞也不画一个乱绕的圈。
* `CF` 是"按编码航向飞到点"，不是"从当前位置直飞"；只有当上一段与编码航向差异很大时才会
  在图上看出折角，实现上仍以该定位点为终点（数据库没有中间点）。
* `S`/`M` 单位：`distance_time` 配合 `route_distance_holding_distance_time`
  （`D` = 距离 NM，`T` = 时间分钟，按 240 kt 折算）。
* 磁航向：`course` 是磁航向（`course_flag = M`），而圆心/半径是几何量；圆弧只用几何量，
  所以 `RF`/`AF` 不受磁差影响，等待/程序转弯的画法按真航向近似（仅为示意）。

---

## 5. 性能（实测，单核）

| 指标 | 数值 |
|---|---|
| 索引载入（冷启动预热） | 179 ms（仅索引） / 551 ms（含航路图预热） |
| `parseRoute` p50 / p95 / p99（纯航路段） | 0.045 / 0.112 / 0.183 ms |
| `parseRoute` p50 / p95 / p99（含 SID/STAR 的真实航路，带航段表） | 0.065 / 0.167 / 3.2 ms（p99 落在"首次读该机场程序"） |
| `parseRoute` 吞吐 | ≈ 6 200 次/秒（含终端程序 + 逐段说明）；`detail=0` 约 11 000 次/秒 |
| `parseRoute` HTTP 端到端 | 4–9 ms（含网络与框架开销） |
| `efb/route` p50 / p95（自动 SID/STAR + 4.8 万节点 A\*，热态 18 条航线） | 5 / 33 ms |
| `efb/route` 首次访问某个机场 | 30–190 ms（构建该机场全部程序候选 + 接入点缓存） |
| `efb/route?iap=…`（进近接管终端段） | 24–50 ms（含进近候选接入，与 STAR 同量级） |
| `GET /api/efb/procedures`（单机场 40 条 SID） | 1.7 ms（含全部展开预览） |
| `GET /api/efb/procedures?type=iap`（26 条进近，冷/热） | 20 / 0.8 ms（冷读含 SQLite 取行） |
| `GET /api/efb/procedure`（单条展开） | 0.03 ms |
| `GET /api/efb/procedure?type=iap`（进近 + 过渡 + 复飞 + 加密轨迹 + 逐段说明） | 0.5–1.3 ms |
| `GET /api/efb/airport`（跑道 + ILS + 程序清单，冷/热） | 1.8 / 1.4 ms |
| `GET /api/efb/runways`（只跑道 + ILS，冷/热） | 0.5 / 0.2 ms |
| 全量机场跑道/ILS 遍历（16 029 机场 = 40 335 跑道 + 4 640 条 ILS） | 8.8 s（0.55 ms/机场） |
| 航路图构建（首次） | 357 ms |
| 首次 `efb/route`（含航路图构建） | ≈ 500 ms |
| 上传导入（190 MB 库 / 55 MB zip） | ≈ 13 s |

复现：`python tests/bench.py data 2610 5000`（纯解析）、`python tests/check_procedures.py data 2610 0`（全量程序展开）

---

## 6. 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `NRS_DATA_DIR` | `/data` | 数据根目录 |
| `NRS_UID` | `1000` | 容器内运行服务的 uid（入口脚本据此修正 `/data` 属主后降权） |
| `NRS_GID` | `1000` | 容器内运行服务的 gid |
| `NRS_MAX_LOADED_CYCLES` | `3` | 常驻内存周期数（LRU） |
| `NRS_DEFAULT_CYCLE` | 空 | 强制默认周期；空则按有效期自动选择 |
| `NRS_PRELOAD` | `1` | 启动时预热默认周期 |
| `NRS_PRELOAD_GRAPH` | `1` | 预热时同时构建航路图（首个 `/api/efb/route` 免 360 ms 等待） |
| `NRS_REBUILD_ON_UPLOAD` | `1` | 上传成功后自动重构建（载入内存），新周期立即可用 |
| `NRS_REBUILD_GRAPH_ON_UPLOAD` | `1` | 自动重构建时一并建航路图（2610 周期约 +0.5 s） |
| `NRS_PROC_CACHE` | `512` | SID/STAR 机场级缓存条目上限（每条约 20–200 KB） |
| `NRS_CONNECT_RADIUS_NM` | `250` | 机场与航路点的连接半径 |
| `NRS_ROUTE_MAX_EXPAND` | `400000` | A\* 最大扩展节点数 |
| `NRS_ROUTE_TIME_LIMIT` | `5` | A\* 时间上限（秒） |
| `NRS_MAX_UPLOAD_MB` | `4096` | 上传大小上限 |
| `NRS_TOKEN` | `admin123456` | 写接口（POST/DELETE）令牌；置空关闭校验 |
| `NRS_ADMIN_USER` | `admin` | Web 控制台登录用户名 |
| `NRS_ADMIN_PASS` | `admin123456` | Web 控制台登录密码 |
| `NRS_SESSION_TTL` | `43200` | 登录会话有效期（秒，滑动续期） |
| `NRS_COOKIE_SECURE` | `0` | 置 `1` 时 cookie 只走 HTTPS（反代 TLS 场景） |
| `NRS_LOGIN_MAX_FAILS` | `8` | 同 IP 连续登录失败次数上限，超过进入冷却 |
| `NRS_LOGIN_LOCK_SECONDS` | `60` | 登录冷却时长（秒） |
| `NRS_TRUST_PROXY` | `0` | 置 `1` 时用 `X-Forwarded-For` 作为限流 IP（仅反代后开启） |
| `NRS_LOG_LEVEL` | `INFO` | 日志级别 |

---

## 7. 自测

```bash
python tests/test_services.py                # 导入 / 多周期 / LRU / 覆盖保护 / 解析 / SID-STAR / 航段
python tests/check_chains.py data 2610 500   # 随机 500 条航路链正反双向回环校验
python tests/check_procedures.py data 2610 0 # 全量 SID/STAR 展开校验（0 = 全部机场）
python tests/check_iaps.py data 2610 0       # 全量 APPROACH 展开 + 复飞校验（0 = 全部机场）
python tests/check_legs.py data 2610 0       # 全量 ARINC 424 航段 + 轨迹校验
python tests/check_api_detail.py data 2610 200  # 三接口航段表一致性 + 进近参与航路
python tests/check_airport_info.py data 2610 0   # 全量跑道 / ILS 覆盖与换算校验
python tests/bench.py data 2610 5000         # 延迟基准
```

`check_chains.py` 会随机抽取真实航路链，构造 `首点 航路 末点` 并逐点比对，
当前 2610 周期结果：**1000/1000 完全一致**（正反双向）。

`check_procedures.py` 对每个机场的每条 SID/STAR 用"无跑道 / 各跑道 × 无过渡 / 首个过渡"展开，
检查空展开、相邻点跳距、首末点与机场距离，输出异常清单（当前 2610：全部通过）。

`check_iaps.py` 对每个机场的每条 APPROACH 用"无过渡 / 各过渡 / 指定跑道+过渡"展开，
检查空展开、缺最终进近段、跑道未识别、末点偏离机场、相邻点跳距、复飞段是否排在跑道之前、
以及加密轨迹是否远离机场。

`check_legs.py` 逐个展开所有 SID/STAR 并生成 `track`，校验：无异常、轨迹首末点与首末定位点一致、
轨迹不产生数据里没有的长跳变、`RF`/`AF` 确实是弧而不是弦，同时统计各终止符出现次数。
当前 2610 周期结果：**0 异常、0 首末偏移、0 引擎长跳变、0 细弧**；
另外会单列源数据本身的长航段（如 ZYLD 的 `JYC` 被编到 1350 NM 外），只报不改。

`check_api_detail.py` 校验 `procedure` / `parseRoute` / `route` 三个接口返回的航段表**逐字节相同**：
200 机场 2103 条程序全部可 JSON 序列化且字段齐全、圆弧必有圆心与半径；
25 对起降机场的 `route`/`parseRoute` 行级对等；**200 条真实进近**经航路字符串和 `iap` 参数两种方式
展开后与 `procedure_detail` 一致，且航路终点落在跑道而不是复飞点，`detail=0` 确实不含 `segments`。

`check_airport_info.py` 遍历全部机场，校验跑道与 ILS 与源表**逐行对应**（不漏不重）：
当前 2610 周期 **40 335 行跑道 / 4 640 条 ILS 全部输出**，设计符已归一（无 `RW` 前缀、无重复），
英尺→米换算一致，含多条 ILS 的跑道一并列出，其余 0 问题；冷态约 0.55 ms/机场，
最大载荷 KORD 25.9 KB（9 条跑道 + 11 条 ILS）。

---

## 8. 已知边界

* 航路查询的巡航段只使用 ER 表的公开航路，**不含 NAT / PACOTS 洋区航迹**，
  跨洋航线可能绕行（数据本身的连接关系，不是算法问题）；绕行超过直线距离 1.6 倍时会在 `warnings` 说明。
* 纯向量程序（整条 SID 只有 `VA`/`VM` 航段，如加拿大、非洲部分小机场）没有任何定位点，
  只能给出跑道入口；不指定跑道时该类程序无法展开（会告警，不会臆造航迹）。
* SID/STAR/APPROACH 走 SQLite 主键查询 + 机场级 LRU（`NRS_PROC_CACHE`，默认 512 个机场），
  不进入 `index.pkl`，因此换周期不需要重建程序缓存，代价是每条程序的首次读取约 1 ms。
* 进近程序的**复飞段只按顺序标记**（跑道之后的航段），且**不作为程序终点**——
  终点永远是跑道，G/A 单独成序列（`missed_track`）；不做独立的复飞航路选择，
  同一跑道有多条复飞（例如不同过渡点）时按 `seqno` 顺序全部列出。
  进近现在可以写进 `parseRoute` 的航路字符串，也可以由 `route` 的 `iap` 参数接入；
  但**不会自动挑进近**（进近是管制现场给的程序），且指定了 `iap` 就不再自动接 STAR——
  二者都能给出的话，建议先查 `procedure` 逐条展开，再由调用方决定拼哪一条。
* `direction_restriction` 按"提示 + 兜底"处理，不作为硬拒绝（原因见 4.3）。
* 跑道/ILS 只**照源数据输出**：8 条跑道源表没有编磁/真航向（多为欧美小机场）、
  少数跑道 `length`/`width` 为空，字段返回 `null`，不做插值或按跑道号推算；
  进近灯光（ALS/PAPI）、跑道坡度以外的道面细节 DFD 不提供，也不臆造。
  直升机坪/水上跑道按 `kind: "other"` 保留，避免"少一条跑道"。
* 反经线（180°）跨越的航路在 Web 预览图上不做特殊展开，经纬度值本身不受影响。
* 单进程（`--workers 1`）可保证一份索引；如需多进程请同时调大内存预算。

---

## 9. 常见问题

**`PermissionError: [Errno 13] Permission denied: '/data/tmp'`（容器反复重启）**

宿主机的 bind mount（`./data:/data`）会覆盖镜像里 `/data` 的属主。容器若直接以 uid 1000 运行，
遇到属主为 root 的宿主机目录就写不了。

当前镜像的入口脚本已自动处理：以 root 启动 → 探测 `/data`、`/data/cycles`、`/data/tmp` 是否可写 →
必要时 `chown -R $NRS_UID:$NRS_GID /data` → 降权为 uid 1000 执行 uvicorn。
因此**重新构建镜像即可**：

```bash
docker compose up -d --build
```

若入口仍打印 `is not writable by uid 1000`，说明该文件系统不能写或忽略 chown：

| 场景 | 处理 |
|---|---|
| 宿主机目录属于普通用户 | compose 里设 `NRS_UID` / `NRS_GID` 为该用户的 `id -u` / `id -g` |
| 目录被 root 占用且可写 | `sudo chown -R 1000:1000 ./data` |
| NFS（root squash）/ SMB / Windows 共享 | 改用命名卷 `- navroute_data:/data`，并加 `volumes: navroute_data:` |
| 只读挂载（`:ro`） | 去掉 `:ro`，或改为命名卷 |

**如何判断镜像是不是新的**

新镜像启动时一定会打印入口日志，且 `main.py` 的报错行号不再是 `Path(path).mkdir`：

```bash
docker compose logs --tail=20 navroute | head -3
# 期望：[entrypoint] data dir /data ready (running as uid 1000:1000)
docker compose exec navroute ls -l /usr/local/bin/docker-entrypoint.sh
```

只看到 `INFO: Started server process` + 老堆栈 = 仍在跑旧镜像，用 `docker compose build --no-cache && docker compose up -d --force-recreate`。

**没有新镜像时的临时应急（旧镜像也能生效，不用重建）**

```yaml
services:
  navroute:
    user: "0:0"        # 旧镜像里是 USER nrs，这里以 root 覆盖，/data 即可写
```

或者直接在宿主机 `sudo chown -R 1000:1000 ./data` 后重启容器。

**只读降级模式**

新版启动时会对 `/data`、`/data/cycles`、`/data/tmp` 做真实写探针。若不可写，
服务**不再崩溃重启**，而是以只读模式继续提供服务：

| 接口 | 只读模式下的行为 |
|---|---|
| `parseRoute` / `route` / `procedures` / `procedure` / `cycles` | 正常（读已安装周期） |
| `upload`、`DELETE /cycles/{id}` | `503` + 明确的 chown / NRS_UID / 命名卷提示 |
| `GET /api/efb/health` | `status: "degraded"`、`read_only: true`，便于探活与告警区分 |
