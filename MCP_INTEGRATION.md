# MCP 集成说明

> 参赛必交文件。说明本项目**实际使用**的麦当劳 MCP Server、Tool、调用流程和业务价值。
>
> 状态：**骨架已就位，等选题后补「调用流程」与「业务价值」两节。**

## 1. Server

| 项 | 值 |
|---|---|
| 提供方 | 麦当劳中国（金拱门（中国）有限公司） |
| 服务地址 | `https://mcp.mcd.cn` |
| 传输协议 | Streamable HTTP |
| 鉴权 | 请求头 `Authorization: Bearer <MCD_MCP_TOKEN>` |
| 协议版本 | `2025-06-18` |
| 握手实测 | `serverInfo` = `mcd-mcp` / `1.0.0` |
| 限流 | 600 次 / 分钟，超限返回 `429` |
| Token 申请 | <https://open.mcd.cn/mcp>（手机号登录 → 控制台 → 激活） |
| 客户端接入 | WorkBuddy：连接器 → 自定义连接器 → 配置 MCP → 粘贴下方 JSON → 启用 |

脱敏配置示例见 [`mcp-config.example.json`](mcp-config.example.json)（仅环境变量占位符）。

```json
{
  "mcpServers": {
    "mcd-mcp": {
      "type": "streamablehttp",
      "url": "https://mcp.mcd.cn",
      "headers": { "Authorization": "Bearer ${MCD_MCP_TOKEN}" }
    }
  }
}
```

## 2. Tool 清单（2026-10-10 实测 `tools/list`，共 35 个）

> 下列名称与参数为**线上实测值**，非文档转述。
> ⚠️ 官方 `mcd-mcp-server` README 的清单已滞后：文档写 `query-partystore-date` / `query-partystore-session`，
> 线上实际为 `query-party-store-date` / `query-party-store-session`；且线上多出 `query-survey-coupon`、`query-promotions` 两个文档未收录的工具。

### 2.1 餐品与营养（2）

| Tool | 说明 | 必填参数 |
|---|---|---|
| `list-nutrition-foods` | 常见餐品营养成分（能量/蛋白质/脂肪/碳水/钠/钙） | — |
| `query-meals` | 餐品列表；到店 `orderType=1`、外送 `orderType=2`；团餐含搭配规则 | `storeCode` `orderType` `beType` |

### 2.2 门店与地址（4）

| Tool | 说明 | 必填参数 |
|---|---|---|
| `query-nearby-stores` | 到店场景门店；`beType=1` 到店自提、`beType=5` 得来速 | `beType` `searchType` |
| `delivery-query-addresses` | 用户配送地址列表 | — |
| `delivery-query-stores` | 某地址可配送门店 | `addressId` `beType` |
| `delivery-create-address` | 新增配送地址 | `city` `contactName` `phone` `address` `addressDetail` |

### 2.3 价格与下单（5）

| Tool | 说明 | 必填参数 |
|---|---|---|
| `query-meal-detail` | 餐品/套餐组成，支持换购与特制 | `storeCode` `orderType` `beType` `code` |
| `calculate-price` | 计算金额、配送费、优惠与应付总价 | `storeCode` `orderType` `beType` |
| `create-order` | 创建订单，返回订单详情与支付链接 | `storeCode` `orderType` `beType` |
| `cancel-order` | 取消订单（`cancelReasonCode` 1~5） | `orderId` `cancelReasonCode` |
| `query-meal-assistance` | 仅企业团餐（`beType=6`）助餐服务 | `storeCode` |

### 2.4 优惠券（4）

| Tool | 说明 | 必填参数 |
|---|---|---|
| `available-coupons` | 麦麦省**可领取**的券列表 | — |
| `auto-bind-coupons` | 麦麦省一键领券（领取全部可领券） | — |
| `query-my-coupons` | 已持卡包券；**不校验门店/渠道规则**，不代表本单可用 | — |
| `query-store-coupons` | 指定门店 + 订单类型下**真正可用**的券，下单前应用此工具 | `storeCode` `orderType` `beType` |

### 2.5 订单与促销（4）

| Tool | 说明 | 必填参数 |
|---|---|---|
| `query-order` | 订单详情、状态、配送进度 | `orderId` |
| `order-list` | 历史订单（非商城） | — |
| `query-survey-coupon` | 按订单号查 CSAT 满意度答卷及其关联奖券 | `orderId` |
| `query-promotions` | 团餐场景门店可用满减/满折规则原始数据 | `storeCode` `orderType` `beType` |

### 2.6 积分与麦麦商城（9）

| Tool | 说明 | 必填参数 |
|---|---|---|
| `query-my-account` | 积分账户（可用/累计/冻结/即将过期） | — |
| `mall-points-products` | 商城可兑换商品列表，支持类目筛选（餐品券/实物/周边/礼品卡/派对/体验营/品鉴会/读书会） | — |
| `mall-product-detail` | 商品详情与全部 SKU | `spuId` |
| `mall-create-order` | 积分兑换下单（仅 `shopId=2`；多商品须分别、串行下单） | `skuId` `spuCategory` |
| `mall-order-list` | 商城订单列表 | — |
| `mall-order-detail` | 商城订单详情 | `orderId` |
| `query-lottery-info` | 积分抽奖活动信息与奖品列表（**不执行抽奖**） | — |
| `draw-lottery` | 执行一次抽奖（须先展示 `drawDecision.nextConsumption` 并经用户确认） | — |
| `query-my-prizes` | 我的抽奖奖品记录 | — |

### 2.7 主题活动与派对（6）

| Tool | 说明 | 必填参数 |
|---|---|---|
| `campaign-calendar` | 当月营销活动日历（进行中/往期/未来 + 订阅状态） | — |
| `query-party-city` | 主题活动可参与城市 | `spuId` |
| `query-party-store` | 指定城市下可举办活动的门店 | `code` |
| `query-party-store-date` | 指定门店可预约日期 | `spuId` `storeCode` |
| `query-party-store-session` | 指定门店 + 日期的场次列表 | `spuId` `storeCode` `dateStr` |
| `party-order-create` | 派对下单，支持包场 `partyType=1` / 拼团 `partyType=2` | `partyType` |

### 2.8 通用（1）

| Tool | 说明 | 必填参数 |
|---|---|---|
| `now-time-info` | 服务器当前完整时间（时间戳/年/月/日/时区/UTC） | — |

## 3. 调用流程

本作品只用到 MCP 的两个工具，全部为**只读**调用（不领券、不下单、不写地址）。

### 3.1 采集期（离线，`tools/collect.py`，约 1425 次调用 / 52 秒）

```
对每个城市 C：
  for kw in [C, "麦当劳", *C的区县, *17条通用道路, *25个通用POI]:   # 同构模板，13 城完全一致
    query-nearby-stores {beType:1, searchType:2, city:C, keyword:kw}   # 到店自提
      → data[] 最多 5 家 {storeCode, storeName, address, distance,
                          businessStartTime, businessEndTime, businessStatus, reservation}
      → 按 storeCode 累积去重 → 该城门店集合（采样下界）

  for kw in 同一套关键词:                                            # 与上面严格同构
    query-nearby-stores {beType:5, searchType:2, city:C, keyword:kw}   # 得来速
      → 收集支持得来速的 storeCode（占比才有可比性）

  对该城 3 家样本门店（首 / 中 / 末）:
    query-meals {storeCode, orderType:1, beType:1}
      → data.meals{code: {name, originalPrice, currentPrice, ...}}     # 整店菜单一次拿全
```

关键取舍：

- **`calculate-price` 不用了**。它是逐 SKU 计价（还要拼 `productCode`），而 `query-meals`
  的 `data.meals` 字典里直接带 `name` + `originalPrice`，一次调用顶整店。
  注意菜单在 `data.categories[].meals` 里只有 `code`+`tags`，**必须读顶层的 `data.meals`**。
- **价格只取 `originalPrice`（挂牌价）**。`currentPrice` 含限时活动价与卡券绑定
  （`withOrder` / `discountType`），跨城不可比。
- 菜单**随时段变化**（早餐/正餐不同），必须同批采完。

### 3.2 计算期（离线，`src/mcdindex.py`，纯函数）

```
原始量 → 区分度筛查(relative_spread ≥ 0.30) → 组内百分位 → 加权合成 → 组内排名
```

三个计分维度：门店密度（0.55）、核心区覆盖（0.25，取城市锚点第 5 家距离，反向）、
得来速占比（0.20）。价格篮子与营业时长因区分度不足被降级为"读数"。

### 3.3 运行期（用户提问时，1~2 次调用）

| 用户输入 | 调用 | 门控 |
|---|---|---|
| 「苏州」 | 命中内嵌的城市索引，**不调 MCP** | 无 |
| 「新街口」 | 在已采 559 家门店的 name/address 里检索 → 归属城市 → 出该城指数 | 无 |
| 需刷新数据 | `tools/collect.py` 重跑 | 消耗配额头，需用户显式发起 |

**本作品不触发任何写操作**：不 `create-order`、不 `draw-lottery`、不 `delivery-create-address`、
不 `auto-bind-coupons`。因此不存在需要确认门控的破坏性动作。

## 4. 业务价值

**不用 MCP 做不到什么：**

- 门店供给、营业时间、可得来速与否、整店在售菜单与挂牌价，都是麦当劳自有数据。
  第三方商业数据库要么有授权门槛，要么口径不透明、更新滞后，**且拿不到官方接口的
  `storeCode` / `businessStatus` / `reservation` 这类操作性字段**，无法做到同口径、可复现。
- 本作品的全过程（693 次关键词轮询 → 去重 → 定价 → 打分）**每一层都可复现**，
  换一批城市只需往 `DISTRICTS` 里加名字。

**用 MCP 之后用户少做了什么：**

1. 不用逐个城市手动搜门店、抄地址、比价格 —— 一次采集覆盖 13 城 559 家门店。
2. 不用自己判断"哪个指标有意义" —— 引擎用区分度门槛主动筛掉无效维度，
   并给出了反直觉结论：**江苏 13 城价格几乎无差异（极差 1.66%），差异全在供给密度上（8.7 倍）**。
3. 不用纠结绝对数准不准 —— 产品语义直接定义成"同级别比较"，把已知的采样偏差变成设计前提而非缺陷。

**如实说明的边界：** 本作品**不能**给出门店绝对总数。MCP 门店接口单次上限 5 家、
无分页、经纬度恒为 `0.0` 且入参无坐标字段 —— 这是数据源的硬约束，已在 README「已知局限」中公开。


## 5. 联调记录

| 日期 | 环境 | 结果 |
|---|---|---|
| 2026-10-10 | `tools/mcp_probe.py`，直接 POST `https://mcp.mcd.cn` | 握手成功，`serverInfo=mcd-mcp 1.0.0`；`tools/list` 返回 **35** 个工具；无 `Mcp-Session-Id`（服务端无状态） |

复现方式：

```bash
python tools/mcp_probe.py           # 打印工具清单
python tools/mcp_probe.py --save    # 另存 tools/out/mcp-tools.json
```
