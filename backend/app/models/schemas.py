"""请求与响应数据契约。  —— v0.2（2026-10-07 契约冻结）

位置：项目最底层，只依赖 pydantic，不 import 本项目任何模块。
作用：定义所有模块之间传递的数据形状。字段名与 frontend/src/types/trip.ts
      逐字对应（不做驼峰转换），改一边必须改另一边。

核心原则「输入严、输出宽」：
    TripRequest 的字段大多必填 —— 缺了说明用户填错，应该拦住（422）。
    其余类的字段大多有默认值 —— 缺了说明某路数据降级，应该放行 + 写 warnings。

v0.2 的四项冻结决策（详见 docs/需求文档.md 第 12 章）：
    1. 天数 / 晚数 / 日期：start_date + travel_days 是**唯一权威输入**；
       end_date 可选（提供了就必须与天数一致，否则 422）；nights 可选（默认 天数-1）。
       —— 修掉了旧的「三个字段说同一件事且互相不校验」这个契约 bug。
    2. 住宿是「行程级」不是「天级」：删掉 DayPlan.hotel，改成 TripPlan.hotel。
       3 天 2 晚住一家，没有游客天天换酒店。
    3. 锚点模型：用户可指定已定酒店（fixed_hotel）与必去景点（must_visit），
       两者都是硬约束；来源标记新增 "user"。
    4. 约束可对账：新增 ConstraintReport，说明硬约束满足了哪些、没满足哪些、为什么。
"""

from typing import Dict, List, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator

# 字段级的数据来源标记。用 Literal 而不是 str：
# 写错了（比如 llm_estiamted）在构造对象的瞬间就报错，不会一路跑到前端才发现标签没显示。
# "user" 是 v0.2 新增：用户自己指定的锚点，既不是高德实查、也不是模型估算，
#          前端要单独显示「你指定的」。
DataSource = Literal["amap", "llm_estimated", "degraded", "user"]


# ================================================================ 输入


class TripRequest(BaseModel):
    """一次规划请求的入参。

    这是全文件唯一「以必填为主」的类。因为输入数据的错和输出数据的错性质不同：
    输入缺字段是用户填错了，要拦；输出缺字段是数据源降级了，要放行。

    天数 / 晚数 / 日期的三者关系（v0.2 冻结）：
        start_date + travel_days  权威输入
        end_date   可选；提供则必须等于 start_date + travel_days - 1，
                   否则 422 DATE_RANGE_MISMATCH（防止三个字段各说各话）
        nights     可选；默认 travel_days - 1。
                   住几晚才决定住宿费：3 天行程通常住 2 晚（最后一晚返程），
                   但「红眼航班到、红眼航班走」确实住 3 晚，所以允许覆盖。
    """

    model_config = ConfigDict(
        # 只影响 FastAPI 的 /docs 页面：给这个接口一个可点击的示例 JSON，
        # 调试时不用手打请求体
        json_schema_extra={
            "example": {
                "city": "杭州",
                "start_date": "2026-10-01",
                "travel_days": 3,
                "transportation": "公共交通",
                "accommodation": "经济型酒店",
                "preferences": ["自然风光", "美食"],
                "must_visit": ["西湖", "灵隐寺"],
                "fixed_hotel": "",
                "free_text_input": "想有一天去周边，不要太赶",
            }
        }
    )

    # ---- 行程骨架（天数在这里定，它是所有约束的分母）----
    city: str  # 必填。解析不出坐标就返回 400 CITY_NOT_FOUND，不进后续节点
    start_date: str  # 必填。YYYY-MM-DD 格式
    travel_days: int = Field(ge=1, le=30)  # 用 Field 声明约束，比手写 validator 好：约束会进 OpenAPI schema
    end_date: Optional[str] = None  # v0.2：可选。提供则须与 travel_days 一致，否则 422
    nights: Optional[int] = None  # v0.2：可选。默认 travel_days - 1；住宿费按它算

    # ---- 偏好（软约束，填不满可以让位）----
    transportation: str  # 公共交通 / 自驾 / 步行 / 混合；用于估算交通费
    accommodation: str  # 经济型酒店 / 舒适型酒店 / 豪华酒店 / 民宿；会被映射成搜索关键词
    preferences: List[str] = Field(default_factory=list)  # 可多选，空则用「景点」兜底；每个标签各搜一次再合并
    free_text_input: Optional[str] = Field(default="")  # 前端可能传 null 也可能不传这个字段，两种都要接住

    # ---- 锚点（v0.2 新增，硬约束，优先级高于 preferences）----
    must_visit: List[str] = Field(default_factory=list)
    # ↑ 用户必须去的景点（在城市范围内按名字解析）。硬约束的含义有两层：
    #   ① 必须注入景点候选池（偏好标签搜不到它也要强制解析进来）；
    #   ② 必须真的出现在 days[].attractions 里，由 ConstraintReport 对账。
    #   原理：约束越结构化越可靠，靠 prompt 表达的约束遵守率会掉，所以硬约束必须配校验代码。
    fixed_hotel: Optional[str] = None
    # ↑ 用户已订好的酒店名。解析必须和 city 一起用 —— 只给「如家酒店」全国有几千家。
    #   解析不到 → HOTEL_NOT_FOUND；命中多个无法确定 → HOTEL_AMBIGUOUS。
    fixed_hotel_address: Optional[str] = None
    # ↑ 可选，用于消除重名歧义。


# ================================================================ 基础类型


class Location(BaseModel):
    """经纬度。"""

    longitude: float  # 经度。高德返回的是 "120.15,30.25" 字符串，在 services/amap.py 里切开转 float
    latitude: float  # 纬度


class Attraction(BaseModel):
    """景点。Agent 1 从高德 POI 搜索得到。"""

    name: str  # 必填。来自高德 pois[].name
    address: str = ""  # 来自高德 pois[].address，偶尔为空
    location: Location  # 必填：没有坐标的景点 M3 阶段在地图上画不出来，适配层应直接丢弃而非填 (0,0)
    visit_duration: int = 120  # 建议游览时间（分钟）。高德不提供，由模型给；v0.2 起它还是选址的权重
    description: str = ""  # 由模型写
    category: Optional[str] = "景点"  # 来自高德 pois[].type，取最后一级
    typecode: Optional[str] = None
    # ↑ v0.2 新增：高德 pois[].typecode，如 "110202"。
    #   为什么必须留它：需求文档 3.2 要求「按 typecode 过滤掉餐饮与住宿」，
    #   而 category 只保住了 type 的最后一级，做过滤需要的分类码就丢了。
    #   实测前缀：05=餐饮服务、10=住宿服务、11=风景名胜、14=科教文化、06=购物服务；
    #   typecode **可能是多个码用 "|" 拼接**（如 "060101|061101|061000"），判断前要先 split。
    rating: Optional[float] = None  # 来自高德 biz_ext.rating；覆盖不稳定，经常是 None
    photos: List[str] = Field(default_factory=list)  # 高德 pois[].photos[].url 自带图片，所以本项目不需要 Unsplash
    poi_id: Optional[str] = None  # 高德 POI ID。去重要按它做，不能按 name（「西湖」和「西湖风景区」是两条）
    image_url: Optional[str] = None  # 主图，通常取 photos[0]
    ticket_price: int = 0  # 门票价。0 有两种含义：真免费 / 高德没给，靠 sources 区分，不能自己猜
    required: bool = False  # v0.2：是否来自用户的 must_visit（前端给「必去」打标，也用于满足性对账）


class Hotel(BaseModel):
    """酒店。由 resolve_hotel 节点产出（v0.2：不再是「每天一个」，而是行程级一家）。

    价格类字段高德不提供，全部是模型估算，最终会被标成 llm_estimated。
    """

    name: str  # 必填。来自高德 pois[].name
    address: str = ""  # 来自高德 pois[].address
    location: Optional[Location] = None  # 允许为空：个别酒店 POI 缺坐标，不该因此整条丢弃
    price_range: str = ""  # 价格区间。模型估算
    rating: str = ""  # 高德 biz_ext.rating，注意这里是 str 不是 float（高德返回的就是字符串）
    distance: str = ""
    # ↑ v0.2 语义收窄：不再是「距市中心」的近似说明，而是
    #   「本酒店到行程中（或候选中）各景点的加权距离」的说明。
    #   选址算法给出，用高德距离测量接口真实计算，不让模型猜。
    type: str = ""  # 酒店类型。名字与 TS 那边保持一致，不因为遮蔽内置 type 而改名
    estimated_cost: int = 0  # 预估每晚费用（元）。模型估算
    nights: int = 0  # v0.2：住几晚。住宿费 = estimated_cost × nights，不是 × 天数


class Meal(BaseModel):
    """一餐。由 Agent 4 编排时生成。"""

    type: str  # breakfast / lunch / dinner / snack；前端按它显示中文标签
    name: str  # 必填。餐厅或菜品名，模型给
    address: Optional[str] = None  # 可选
    location: Optional[Location] = None  # 可选；有坐标才能在地图上显示
    description: Optional[str] = None  # 可选
    estimated_cost: int = 0  # 预估费用（元）。既然是「估算」，就必然是 llm_estimated


class WeatherInfo(BaseModel):
    """某一天的天气。Agent 2 从高德天气接口得到。

    这是全文件唯一带 validator 的类，因为高德返回的温度格式脏。
    """

    date: str  # YYYY-MM-DD
    day_weather: str = ""  # 白天天气，来自 forecasts[0].casts[].dayweather
    night_weather: str = ""  # 夜间天气
    day_temp: Union[int, str] = 0  # 白天温度。用 Union 而不是 int：高德偶尔给 "25-28" 这种范围值，转不了 int 就原样留着，不能炸
    night_temp: Union[int, str] = 0  # 夜间温度，同上
    wind_direction: str = ""  # 风向，如「南风」
    wind_power: str = ""  # 风力，如「1-3级」
    is_forecast: bool = True  # True=预报，False=实况。高德 extensions=all 只给今天起 4 天的预报，出行日期已过时才会走到实况
    beyond_window: bool = False  # v0.2：True 表示这一天超出预报窗口（天数 > 4 时必然出现）。
    # ↑ 存在的意义是「诚实性可对账」：超窗口的天必须留空并标 True，
    #   绝不能编造天气。这是诚实性实验要测的那一类字段。

    @field_validator("day_temp", "night_temp", mode="before")
    @classmethod
    def parse_temperature(cls, value: object) -> object:
        """清洗温度单位：'25°C' / '25℃' / '25°' 一律变成 25。

        mode="before" 不能省：默认的 after 模式是在类型转换「之后」跑，
        那时 '25°C' 已经按 Union[int, str] 落成字符串了，改动没意义。
        before 才能拿到原始字符串。

        转不了数字就返回 0，不抛异常：高德数据脏是常态，
        不能因为一个温度字段格式怪就让整个 6 天行程的请求 500。
        """
        if isinstance(value, str):
            cleaned = value.replace("°C", "").replace("℃", "").replace("°", "").strip()
            try:
                return int(cleaned)
            except ValueError:
                return 0
        return value


# ================================================================ 行程结构


class Budget(BaseModel):
    """预算汇总。由 agents/budget.py 纯代码计算，不过 LLM。

    加法是确定性的，模型做加法会错，而且这种错误很难被发现——
    预算卡上写着「总计 2060」，你不会真去逐项加一遍。
    """

    total_attractions: int = 0  # 各天 Attraction.ticket_price 求和
    total_hotels: int = 0
    # ↑ v0.2 修正：= 每晚预估单价 × nights，**不是**「各天 Hotel.estimated_cost 求和」。
    #   因为住宿已经改成行程级一家；3 天行程住 2 晚，按天求和会多算一晚。
    total_meals: int = 0  # 各天三餐 Meal.estimated_cost 求和
    total_transportation: int = 0  # 由模型按交通方式与天数估算（这一项是估算，不是求和）
    total: int = 0  # 四项之和。必须代码算，禁止模型生成


class ConstraintReport(BaseModel):
    """硬约束的对账报告（v0.2 新增）。

    为什么必须有它：「必须去某个景点」这种功能如果不自带可验证性，
    就只是「尽力而为」。有了它，用户能知道你满足了他几个要求、没满足哪几个、为什么。

    它是「两层校验」的第二层产物：
        第 1 层 结构校验（Pydantic）—— 字段类型 / 必填 / 日期连续
        第 2 层 约束校验（业务规则）—— 本对象
    第 2 层不通过时，把「你没排进 X」这个**具体事实**回喂给 compose_plan 重试；
    retries 封顶后不再回喂，但**必须**把结果如实写进 unsatisfied + reasons —— 这就是「降级不静默」。
    """

    required_attractions: List[str] = Field(default_factory=list)  # 用户要求必去的
    satisfied: List[str] = Field(default_factory=list)  # 确实排进行程的
    unsatisfied: List[str] = Field(default_factory=list)  # 没排进去的
    reasons: Dict[str, str] = Field(default_factory=dict)
    # ↑ key = 景点名，value = 原因。约定取值：
    #   "搜索不到该景点" / "容量超限（天数 × 每天上限）" / "距指定酒店过远" / "当日已满"
    hotel_anchor: Optional[str] = None  # 用户指定的酒店名；None 表示由系统选址
    hotel_source: Literal["user", "agent"] = "agent"
    feasible: bool = True
    # ↑ 可行性预检结论。False = 在花钱（调 LLM）之前就被判定不可行，
    #   此时直接 400 CONSTRAINT_INFEASIBLE，不消耗任何模型调用。


class DayPlan(BaseModel):
    """一天的行程。由 Agent 4 生成，是整份计划里唯一真正由 LLM 编排的部分。"""

    date: str  # YYYY-MM-DD。从 start_date 起连续，不允许有缺口
    day_index: int  # 第几天，从 0 开始计数（它是索引，方便 days[day_index]）；前端显示时 +1
    description: str = ""  # 当日概述，模型写
    transportation: str = ""  # 当日主要交通方式，从请求带下来
    accommodation: str = ""  # 住宿类型，从请求带下来
    # v0.2 删除了 hotel 字段：住宿是「行程级」的（TripPlan.hotel），不是天级的。
    # 3 天 2 晚住同一家是常态，没有游客天天换酒店；只有跨区域行程才分段（V1）。
    attractions: List[Attraction] = Field(default_factory=list)
    # ↑ 注意这是「排期结果」（2~3 个），不是 Agent 1 搜到的候选池（候选池在 TripState.attractions）。
    #   同一个字段名在两个地方含义不同，这是最容易被绕晕的一处。
    meals: List[Meal] = Field(default_factory=list)  # 每天必须含早/中/晚三餐
    day_total_cost: int = 0  # 当日小计（元）。由 budget.py 加总，不是模型给的


class TripPlan(BaseModel):
    """最终交付给前端的旅行计划。

    关键认知：它不是某一个 Agent 的输出，而是整张图的投影——
        city/dates       来自用户请求
        weather_info     来自 Agent 2
        days             来自 Agent 4
        hotel            来自 resolve_hotel（用户指定，或系统加权选址）
        budget           来自纯代码计算
        warnings/sources 来自所有节点
    正因为来源这么分散，它必须有大量默认值：任一路降级时，这个对象仍要能构造出来。
    """

    city: str  # 从请求抄来，必填（绝不会缺）
    start_date: str  # 从请求抄来，必填
    end_date: str  # v0.2：由代码派生 = start_date + travel_days - 1，不是用户直接给的
    days: List[DayPlan] = Field(default_factory=list)  # Agent 4 产出。全失败时为空列表，仍然返回
    weather_info: List[WeatherInfo] = Field(default_factory=list)  # Agent 2 直接搬 state.weather；只覆盖预报窗口（最多 4 天）
    overall_suggestions: str = ""  # Agent 4 写的总体建议
    budget: Optional[Budget] = None  # budget.py 计算；days 为空时为 None

    # ---- 住宿（v0.2：行程级，一家住到底）----
    hotel: Optional[Hotel] = None  # 全程住的酒店。None = 没找到/未指定，此时 overall_suggestions 里要说明
    hotel_nights: int = 0  # 住几晚 = TripRequest.nights 或 travel_days - 1
    hotel_segments: List[Hotel] = Field(default_factory=list)
    # ↑ V1 预留：跨区域行程分段住宿（例：前 2 晚市区、第 3 晚千岛湖）。
    #   V0 恒为空列表 —— 留字段不留逻辑，等真有跨城需求再实现。

    # ---- 硬约束对账 ----
    constraint_report: Optional[ConstraintReport] = None  # v0.2 新增，见该类说明

    warnings: List[str] = Field(default_factory=list)
    # ↑ 降级与估算的说明。有内容前端就必须显示（TripSummary.vue 的黄色警示条）。
    #   这是相对参考项目最关键的改进：它降级时会返回「杭州景点1」这种假名字，
    #   而前端完全看不出来。有了 warnings，降级对用户是可见的。
    sources: Dict[str, DataSource] = Field(default_factory=dict)
    # ↑ 字段级来源标记。key 约定（两边必须一致）：
    #     attractions / weather / hotels / hotel / plan
    #     attraction.ticket_price / hotel.price_range / hotel.estimated_cost / meal.estimated_cost
    #   v0.2 起 hotel 这一档可能取 "user"（用户指定），前端显示「你指定的」。
    #   前端据它显示「预估」标签，区分「高德实查」「模型估算」「用户指定」。


# ================================================================ 响应


class TripPlanResponse(BaseModel):
    """统一响应包装。

    真实数据在 data 字段下，不在第一层——这点在参考项目上踩过。
    """

    success: bool  # 是否拿到「完整」行程。注意它与 warnings 是两个独立维度，不是二选一
    message: str = ""  # 给人看的一句话。success=False 时是失败原因
    data: Optional[TripPlan] = None
    # ↑ success=False 但 data 有内容 = 只有部分结果（例如没有预算、某天缺景点）。
    #   前端要把这种情况当「成功」渲染，同时把 message 塞进 warnings 一起显示，
    #   而不是直接报错把已拿到的内容丢掉。HomeView.vue 里就是这么处理的。
    error_code: Optional[str] = None  # v0.2：便于前端按码分支（CITY_NOT_FOUND / CONSTRAINT_INFEASIBLE / ...）
    warnings: List[str] = Field(default_factory=list)  # 与 data.warnings 同源，方便前端不取 data 也能读到
    thread_id: Optional[str] = None
    # ↑ 会话 ID。V0 阶段只返回不消费（前端显示前 8 位当装饰）；
    #   V2 接上 LangGraph Checkpointer 之后，它就是「这一段会话的钥匙」，
    #   用户想改行程里的一天时要带回来。现在就返回它，是为了将来前端不用改。
