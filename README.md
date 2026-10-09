# wb-fast-listing-skill-pure

完整本地运行的 Ozon → Wildberries FBS 上架程序及 Antigravity/Codex Skill。Python 源码全部随包交付；不使用原版云端执行、支付宝、卡密、代理商或学习同步服务。只向 Ozon 读取商品资料、向 WB 官方接口提交用户批准的任务。它是本地源码版本，不具备云端核心代码保密的性质。

## 安装（每台电脑当前使用的账号）

要求：Python 3.10+；Windows 10/11、macOS 或带桌面的 Linux。Git 只用于下载/更新，ZIP 安装无需 Git。缺少 Python 时从 [Python 官方下载](https://www.python.org/downloads/) 安装；本包不自动静默安装系统软件。

仓库是私有的，需要仓库读取权限。也可由管理员把 Release 安装包直接交付给客户，不必给客户 GitHub 权限。

Windows，已安装 Git/Python 后在 PowerShell 执行：

```powershell
git clone https://github.com/cnproduct/wb-fast-listing-skill-pure.git
Set-Location wb-fast-listing-skill-pure
py -3 install.py
```

解压 ZIP 的用户在解压目录执行 `py -3 install.py`。可选 `install.ps1` 会检测 Python 实际版本，脚本全 ASCII，不依赖 PowerShell 中文编码。

macOS/Linux：

```sh
git clone https://github.com/cnproduct/wb-fast-listing-skill-pure.git
cd wb-fast-listing-skill-pure
python3 install.py
```

安装器建立隔离环境、安装 Playwright 与 Chromium、注册当前账号的 Skill，并安装本机定时任务。Linux 使用 crontab，凭据库需桌面 Secret Service/KWallet；浏览器缺系统依赖时按 Playwright 的本机诊断安装。可用 `--no-schedule` 选择手动运行 `worker`。`--no-browser` 仅用于安装验证，不代表抓取环境已就绪。

安装位置：`~/.local/share/wb-fast-listing-skill-pure`。数据位置：`~/.wb-pure`。令牌在操作系统凭据库中，不进入 Git 或 SQLite。`WB_API_TOKEN` 环境变量可用于自管运行环境，首次绑定加 `--token-env`；后续每次执行也要提供相同环境变量，后台定时任务不会自动继承终端临时变量。

> 独立软件：安装器不删除或修改旧版。新对话明确使用 `wb-fast-listing-skill-pure`；同一商品不要同时用旧版执行，避免重复操作。

## 客户使用

安装后新开 Antigravity/Codex 对话，发送：

> 使用 wb-fast-listing-skill-pure。绑定我的 WB 店铺和仓库，收到令牌后直接完成绑定。把这些 Ozon SKU 按人民币绿标价 6 倍作为划线价上架，库存5，先折扣30%，次日折扣50%，核实类目、图片和价格后放库存。

令牌可由用户在聊天提供，助手通过标准输入绑定，不要求重复隐藏输入。用户也可主动选择下面的终端隐藏输入方式。

Windows 命令入口：

```powershell
$wb = Join-Path $env:USERPROFILE '.local\share\wb-fast-listing-skill-pure\wb-pure.cmd'
& $wb doctor
& $wb bind --warehouse 你的仓库数字ID
& $wb batch .\skus.txt --multiplier 6 --stock 5 --start
& $wb status
```

`你的仓库数字ID` 要替换成真实数字。`skus.txt` 每行一个 Ozon SKU 或完整商品链接。macOS/Linux 入口为 `~/.local/share/wb-fast-listing-skill-pure/wb-pure`，参数相同。

多店铺使用不同目录，`--home` 放在子命令**前面**：

```powershell
& $wb --home "$env:USERPROFILE\.wb-pure-store2" bind --warehouse 你的仓库数字ID
& $wb --home "$env:USERPROFILE\.wb-pure-store2" schedule
& $wb --home "$env:USERPROFILE\.wb-pure-store2" batch .\skus.txt --multiplier 10 --start
```

一个目录绑定一个真实卖家和仓库，禁止用同一目录切店。更新令牌须对原店重新 `bind`；普通运行不重复调用卖家身份接口。

## 定价与次日调价

| Ozon CNY绿标价 | 划线价倍数 | WB划线价 | 首次折扣30% | 次日折扣50% |
|---:|---:|---:|---:|---:|
| 100 | 5 | 500 | 350 | 250 |
| 100 | 5.5 | 550 | 385 | 275 |
| 100 | 6 | 600 | 420 | 300 |
| 100 | 10 | 1000 | 700 | 500 |

划线价向上取整到元；卖家折后价保留分。指定倍数不会暗改。可用 `--minimum-sale 300` 设置明确的最低售价，如果最终五折低于此数，预检阻止，不自动提高倍数。此数是销售底价，不是利润核算；佣金、物流、税费、退货和平台活动仍需商家核实。

次日指**初始30%折扣被核实当天的下一个莫斯科自然日00:00**，不是24小时后。比如23:59核实，下一分钟就到次日。计划保存在SQLite，重启不丢失；定时器每分钟唤醒，实际处理受队列、平台限流和异步同步影响。

**本地执行条件：电脑开机、联网、当前账号保持登录。关机/退出期间无法改价，恢复后补跑。** Windows 锁屏通常不影响已登录任务；不能将注销当锁屏。`worker` 可前台持续执行；Ctrl+C只停本次进程，已安装的定时器仍会继续已批准任务。彻底停止自动运行先 `unschedule`，并停止前台 worker；恢复执行 `schedule`。

商品核验后仍每15分钟检查受管商品价格。发现低于保存的最终目标价、错币种或会员优惠叠加，尝试清零本任务仓库库存并回读。网络/API故障会使保护延迟；其他仓库、平台资助补贴和净结算收入不在此保护范围。此机制不构成不亏损保证。

## 抓取和精确类目

- 持久本地 Chromium 会话，自动操作当前语言/币种弹窗，保存后刷新再读。平台改版时返回真实阻塞，不用 RUB 换算凑人民币。验证码只在可见浏览器请求人工完成。
- 只读取本 SKU 价格组件、Product 图片、商品与 features 规格。保存完整两页原始 DOM，解析标题、描述、品牌、图集、类型、属性、包装长宽高、包装毛重及可得税号。没有提供的商品事实不能自动产生。
- 所有新建 WB 商品卡的品牌统一填写小写 `generic`。Ozon 原品牌只保留在采集记录中，用于清理标题、描述中的完整品牌词，不写入 WB 品牌字段；缺少原品牌资料不再阻止上架。型号、材质等不作为品牌猜删。
- 升级前已准备、尚未提交建卡的任务也会使用 `generic`。已提交或已上架商品保留原任务品牌核验，不因软件更新自动批量改品牌。
- 官方类目与 Ozon 类型完全一致时自动匹配；不一致时由助手依据真实商品核对 WB 类目，再提供下列显式映射。不按关键词第一项兜底。
- 规格需逐个 SKU 核实。服饰等需要尺寸时提供本变体的 `wb_size`；不把一个 Ozon SKU 擅自展开成其他变体。

先查询官方数据：

```powershell
& $wb categories --query 'Подставки'
& $wb categories --subject 真实类目数字ID
```

补充文件示意（数字/名称需替换成实际查询值，不能照抄示例）：

```json
{
  "123456789": {
    "category_mapping": {
      "source_type": "Подставка кухонная",
      "subject_id": 123,
      "subject_name": "Подставки",
      "confirmed_by": "operator",
      "evidence": "商品用途、材质和WB官方类目核验记录"
    },
    "wb_characteristics": [{"id": 10, "value": ["сталь"]}],
    "mapping_evidence": "来自当前商品规格，属性ID来自WB官方schema"
  }
}
```

可补充字段：`source_brand`、`properties`、`length_cm`、`width_cm`、`height_cm`、`weight_g`、`source_evidence`、`wb_size`；提供这些事实必须另填 `supplement_evidence`。比如 `wb_size: {"techSize":"M","wbSize":"46"}` 必须对应当前商品的真实变体。禁止通过补充文件覆盖绿标价或币种。

```powershell
& $wb prepare "$env:USERPROFILE\.wb-pure\captures\123456789.json" --overrides .\reviewed.json --multiplier 6
& $wb plan 123456789
& $wb start 123456789
```

抓取记录仅24小时内有效；过期需重新读取。v1.0.2 每个 SKU 必须先在当前浏览器选择 CNY、保存、刷新，再重新打开菜单确认保存的 CNY。程序读取刷新后的本 SKU 原生绿标价，同时核对可见价格区域；不会按汇率计算人民币。价格对象的 CNY 声明与 RUB 普通价/原价冲突时拒绝，规格页价格回退或前后不一致时暂停。

宿主浏览器替代采集也须完成相同操作。以下时间字段填写该操作的真实 Unix 秒，价格原文从当前 SKU 可见的 webPrice 区域实际读取，不能从推荐商品或旧记录复制。商品页及规格页须在刷新完成后采集；商品页采集距离保存不超过10分钟。输入格式：

```json
{"sku":"123456789","currency_check":{
  "currency":"CNY",
  "selected_currency_text":"刷新后菜单中实际选中的币种原文，末尾为 CNY",
  "saved_at":"实际保存Unix秒",
  "refreshed_at":"实际刷新完成Unix秒",
  "visible_price_text":"本 SKU 可见价格区域原文，含原生人民币绿标价"
},"pages":[
  {"url":"https://www.ozon.ru/product/123456789/","captured_at":实际Unix秒,"html":"商品页真实完整DOM"},
  {"url":"https://www.ozon.ru/product/123456789/features/","captured_at":实际Unix秒,"html":"规格页真实完整DOM"}
]}
```

## 状态、排错与更新

- `prepared`：预检通过，尚未授权写入。`blocked`：缺事实/采集/类目，补充后重新prepare。
- `card_pending` / `price_pending`：平台异步处理中，不重复建卡或重复发不明结果的改价请求。
- `written`：后台回读通过；买家端是否已可购买还需另外确认。
- `needs_review`：具体错误保存在status，不能删除状态绕过。既有商品修复不自动批量执行；先 `audit` 只读导出，再由商家确认清单。
- `wb_unauthorized` 对应401；`wb_permission_denied` 对应403；`wb_rate_limited` 对应429，遵守等待；不能笼统都说令牌过期。令牌需要商品内容、价格折扣、Marketplace库存与卖家资料相应权限。
- `another_local_operation_is_running`：同店已有本地操作。等待该操作完成，下轮定时器会再试。
- `ozon_currency_layout_changed` / `ozon_currency_unverified`：页面交互未核实，由助手实际浏览器处理并导出capture，不要求用户猜币种。
- `ozon_currency_reverted` / `ozon_green_price_changed`：保存、刷新或规格页价格不一致，重新观察当前页面并采集，不继续使用旧数字。
- `ozon_visible_green_price_unverified` / `ozon_native_cny_evidence_required`：没有核实同 SKU 的原生 CNY 绿标价、可见价格或保存刷新证据。仅把 currency 字段改为 CNY 无效。
- `source_price_plan_mismatch`：缓存计划与有证据的源价、倍数、划线价、30%/50%折扣不一致，停止自动写入。
- 新建卡片最多等待2小时；无法确认的任务转待核实，有已暴露库存时尝试保护。
- 仓库并发写入采用本机锁。不要把同一数据目录同时放在同步盘上由两台电脑执行。

更新：在下载仓库执行 `git pull --ff-only` 后再次运行 `py -3 install.py`（macOS/Linux `python3 install.py`）；先停止前台worker。安装不会清除本地任务和凭据，但旧对话须重新加载本技能说明。

**v1.0.2 旧任务处理**：此前计划没有保存新的币种证明，因此运行检查会将其转为 needs_review，停止继续自动写入和次日调价。更新不能倒推原币种，也不会自动修复历史售价或清零库存。尚未提交的 prepared/blocked 任务重新 capture、prepare；在途或已上架商品先只读 audit 和核对源价，由商家明确确认具体修复清单，不能删除数据库或重复建卡绕过检查。旧版程序、其他账号安装和第三方脚本也须停止使用；本更新无法约束它们的写入。

## 验证与开发

```sh
python3 -m unittest discover -s tests -p 'check_workflow.py'
PYTHONPATH=. python3 tests/check_ozon.py
PYTHONPATH=. python3 tests/check_browser.py
python3 -m wb_pure --help
```

测试使用模拟WB，不触碰真实店铺。真实 Ozon 验证、WB商品可购买、跨日生产调价需在商家授权的单SKU试运行中另外验收；不能拿离线测试冒充实店成功。

API依据：[WB官方商品、图片与价格折扣文档](https://dev.wildberries.ru/en/openapi/work-with-products)。WB建卡及价格任务异步处理，新卡同步可能需要等待，提交HTTP成功不等于字段已经生效。
