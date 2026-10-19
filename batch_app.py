import streamlit as st

# ===================== 兼容性补丁  =====================
# 解决 streamlit-cookies-manager 内部调用已弃用/移除的 st.cache 问题
if not hasattr(st, "cache"):
    st.cache = st.cache_data

import pandas as pd
import numpy as np
import itertools
import io
import datetime
import math
import project_parameters as pp  # 导入全局参数模块
from financial_plan_cash_flow_model import get_financial_plan_cash_flow
from reverse_price_model import solve_comprehensive_price
from streamlit_cookies_manager import EncryptedCookieManager

# --- 用户数据库（已迁移到 Streamlit secrets，请勿在代码中硬编码密码）---
# 配置方法见 .streamlit/secrets.toml.example（本地）
# 或 Streamlit Cloud -> App -> Settings -> Secrets（线上）
try:
    USER_CREDENTIALS = dict(st.secrets["users"])
except Exception:
    # secrets 未配置 [users] 段时安全兜底：所有账号都无法登录（fail-closed）
    USER_CREDENTIALS = {}

# 设置页面配置
st.set_page_config(page_title="风光储系统经济性评价平台", layout="wide")

# --- Cookie 管理配置 ---
try:
    _cookie_password = st.secrets["cookie_password"]
except Exception as e:
    raise RuntimeError(
        "缺少登录配置：请在 .streamlit/secrets.toml（参考 .streamlit/secrets.toml.example）"
        "或 Streamlit Cloud 的 App -> Settings -> Secrets 中配置 cookie_password 和 [users]"
    ) from e

cookies = EncryptedCookieManager(
    prefix="energy-app/",
    password=_cookie_password
)
if not cookies.ready():
    st.stop()

def check_login():
    """多用户验证逻辑（基于浏览器 Cookie 保持登录态）"""
    # 1. 检查已有的 Cookie 状态
    if cookies.get("auth_status") == "logged_in":
        return True

    st.title("风光储系统经济性评价平台 - 身份验证")

    with st.form("login_form"):
        user_input = st.text_input("账号")
        pw_input = st.text_input("密码", type="password")
        submit = st.form_submit_button("登录")

        if submit:
            # 2. 检查账号是否存在且密码是否匹配
            if user_input in USER_CREDENTIALS and USER_CREDENTIALS[user_input] == pw_input:
                cookies["auth_status"] = "logged_in"
                cookies["current_user"] = user_input  # 额外记录是哪个用户登录的
                cookies.save()
                st.success(f"欢迎回来，{user_input}！正在进入系统...")
                st.rerun()
                return True
            else:
                st.error("账号或密码不正确，请重试")
    return False

def logout():
    """登出逻辑 - 显示在侧边栏顶部"""
    current_user = cookies.get("current_user", "未知用户")
    st.sidebar.markdown("---")
    st.sidebar.write(f"**当前用户**: {current_user}")
    if st.sidebar.button("退出登录", use_container_width=True):
        cookies["auth_status"] = "logged_out"
        cookies["current_user"] = ""
        cookies.save()
        st.rerun()
    st.sidebar.markdown("---")

# 检查登录状态
if not check_login():
    st.stop()

# 在主应用标题前先显示登出功能（侧边栏顶部）
logout()

st.title("风光储系统经济性评价平台")
st.markdown("---")

# --- 解析步長 ---
def parse_range(input_str):
    """
    解析格式如 "起始, 终止, 步长" 的字符串，返回数值列表
    """
    try:
        vals = [float(x.strip()) for x in input_str.split(",")]
        if len(vals) == 1:
            return [vals[0]]
        start, end, step = vals
        if step <= 0:
            return [start]
        # 使用 np.arange 並處理浮點精度
        return np.round(np.arange(start, end + step/2, step), 4).tolist()
    except ValueError:
        return []

def _to_float(value, default=None):
    """安全转 float：空值/非数字/NaN 时返回默认值。"""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return default
    if math.isnan(f):
        return default
    return f

def _fmt_hour_list(vals, max_show=8):
    """把小时数列表格式化为紧凑字符串（用于侧边栏实时预览）。"""
    if not vals:
        return "—"
    shown = ", ".join(f"{v:g}" for v in vals[:max_show])
    if len(vals) > max_show:
        shown += f" …（共 {len(vals)} 个）"
    return shown

def _actual_hours(equiv_list, curtailment):
    """实际小时数 = 等效小时数 × (1 − 限电率)。
    curtailment 可为单个限电率（小数）或限电率列表；多个时返回去重排序后的组合结果。
    """
    rates = curtailment if isinstance(curtailment, (list, tuple)) else [curtailment]
    out = set()
    for r in rates:
        rate = _to_float(r, 0.0) or 0.0
        for v in equiv_list:
            out.add(round(v * (1 - rate), 4))
    return sorted(out)

def _pct_to_fraction(vals):
    """把百分数列表（0~100）转换为小数列表（0~1），并做上下限保护。"""
    return [min(max(_to_float(v, 0.0) or 0.0, 0.0), 100.0) / 100.0 for v in vals]

@st.cache_data
def load_table(file_bytes, filename):
    """读取上传的参数表（缓存避免每次交互重复解析）"""
    if filename.endswith('.xlsx'):
        return pd.read_excel(io.BytesIO(file_bytes))
    return pd.read_csv(io.BytesIO(file_bytes))

# 表格上传模式需要的标准列名，以及可兼容的常见写法（自动做全角/半角括号与空格归一化）
REQUIRED_TABLE_COLUMNS = {
    '风电容量 (MW)': ['风电容量(MW)', '风电容量', '风电规模(MW)'],
    '光伏容量 (MW)': ['光伏容量(MW)', '光伏容量', '光伏规模(MW)'],
    '风电利用小时数 (h)': ['风电利用小时数(h)', '风电利用小时', '风电等效利用小时数(h)', '风电等效利用小时数'],
    '光伏利用小时数 (h)': ['光伏利用小时数(h)', '光伏利用小时', '光伏等效利用小时数(h)', '光伏等效利用小时数'],
    '储能容量 (MWh)': ['储能容量(MWh)', '储能容量', '储能规模(MWh)'],
    '综合电价': ['综合上网电价', '电价'],
}

# 可选列：表格中存在则读取，缺失时按默认值处理（限电率缺省为 0，即不限电）
# 注意：限电率以百分数录入（5 表示 5%），与侧边栏保持一致。
OPTIONAL_TABLE_COLUMNS = {
    '风电限电率': ['风电限电率', '风电弃电率', '风电限电比例', '风电限电率(%)', '风电限电率（%）'],
    '光伏限电率': ['光伏限电率', '光伏弃电率', '光伏限电比例', '光伏限电率(%)', '光伏限电率（%）'],
}

# 反推模式下无需综合电价列（该列由求解器反推得出）
REQUIRED_TABLE_COLUMNS_REVERSE = {
    k: v for k, v in REQUIRED_TABLE_COLUMNS.items() if k != '综合电价'
}

def match_table_columns(df, required=None, optional=None):
    """
    将表格列名映射到标准列名；无法识别的必需列会出现在 missing 清单中。
    兼容：多余空格、全角括号（）、部分常见别名。
    required: 自定义必需列集合（默认使用正算模式的全量必需列）。
    optional: 可选列集合（默认使用 OPTIONAL_TABLE_COLUMNS），缺失不报错。
    返回 (col_map, missing, opt_map)。
    """
    required_cols = REQUIRED_TABLE_COLUMNS if required is None else required
    optional_cols = OPTIONAL_TABLE_COLUMNS if optional is None else optional

    def norm(c):
        return str(c).replace('（', '(').replace('）', ')').replace(' ', '').strip()

    norm_to_actual = {norm(c): c for c in df.columns}

    def build_map(cols):
        m = {}
        for std_name, aliases in cols.items():
            for cand in [std_name] + aliases:
                if norm(cand) in norm_to_actual:
                    m[std_name] = norm_to_actual[norm(cand)]
                    break
        return m

    col_map = build_map(required_cols)
    opt_map = build_map(optional_cols)
    missing = [k for k in required_cols if k not in col_map]
    return col_map, missing, opt_map

# --- 参数表模板（供下载后手动调整再上传）---
TEMPLATE_COLUMNS = [
    '方案名称', '风电容量 (MW)', '光伏容量 (MW)', '储能容量 (MWh)',
    '风电利用小时数 (h)', '光伏利用小时数 (h)',
    '风电限电率 (%)', '光伏限电率 (%)', '综合电价',
]

@st.cache_data
def build_template_bytes():
    """生成参数表模板（含“参数模板”与“填写说明”两个工作表）的 xlsx 字节流。"""
    template_df = pd.DataFrame([
        {'方案名称': '示例方案1', '风电容量 (MW)': 100, '光伏容量 (MW)': 100, '储能容量 (MWh)': 50,
         '风电利用小时数 (h)': 1500, '光伏利用小时数 (h)': 1500, '风电限电率 (%)': 5, '光伏限电率 (%)': 3, '综合电价': 0.33},
        {'方案名称': '示例方案2', '风电容量 (MW)': 200, '光伏容量 (MW)': 150, '储能容量 (MWh)': 100,
         '风电利用小时数 (h)': 1800, '光伏利用小时数 (h)': 1600, '风电限电率 (%)': 8, '光伏限电率 (%)': 5, '综合电价': 0.35},
        {'方案名称': '示例方案3', '风电容量 (MW)': 300, '光伏容量 (MW)': 200, '储能容量 (MWh)': 150,
         '风电利用小时数 (h)': 2000, '光伏利用小时数 (h)': 1700, '风电限电率 (%)': 0, '光伏限电率 (%)': 0, '综合电价': 0.40},
    ], columns=TEMPLATE_COLUMNS)

    notes_df = pd.DataFrame([
        ['方案名称', '可选', '仅作标识，不参与计算；可自由增删。'],
        ['风电容量 (MW)', '必填', '风电场装机容量，单位 MW。'],
        ['光伏容量 (MW)', '必填', '光伏电站装机容量，单位 MW。'],
        ['储能容量 (MWh)', '必填', '储能配置容量，单位 MWh。'],
        ['风电利用小时数 (h)', '必填', '风电年等效利用小时数（未扣限电）。'],
        ['光伏利用小时数 (h)', '必填', '光伏首年利用小时数（未扣限电）。'],
        ['风电限电率 (%)', '可选', '百分数（5 表示 5%），取值范围 0~100；缺省按 0 处理。'],
        ['光伏限电率 (%)', '可选', '百分数（3 表示 3%），取值范围 0~100；缺省按 0 处理。'],
        ['综合电价', '正算必填', '含税综合上网电价，单位 元/kWh；反推模式下无需该列。'],
        ['实际小时数', '自动派生', '实际小时数 = 等效小时数 × (1 − 限电率)，由程序自动计算，无需填写。'],
        ['格式说明', '—', '支持 .xlsx / .csv；第 1 行须为表头，列名建议与模板保持一致（程序会自动忽略全/半角括号与空格差异）。'],
    ], columns=['列名', '是否必填', '说明'])

    bio = io.BytesIO()
    with pd.ExcelWriter(bio, engine='openpyxl') as writer:
        template_df.to_excel(writer, index=False, sheet_name='参数模板')
        notes_df.to_excel(writer, index=False, sheet_name='填写说明')
    return bio.getvalue()


# --- 侧边栏：参数输入模式切换 ---
st.sidebar.header("📋 参数配置")
input_mode = st.sidebar.radio("选择输入模式", ["表格上传", "手动步长输入"])

if input_mode == "表格上传":
    uploaded_file = st.sidebar.file_uploader("上传项目参数表 (Excel/CSV)", type=["xlsx", "csv"])
    st.sidebar.download_button(
        "⬇️ 下载参数表模板 (Excel)",
        data=build_template_bytes(),
        file_name="批量计算参数模板.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        use_container_width=True,
        help="下载标准模板，按“参数模板”工作表填写后上传；限电率等列请勿改名。",
    )
    if uploaded_file:
        try:
            input_df = load_table(uploaded_file.getvalue(), uploaded_file.name)
            st.sidebar.success(f"成功读取 {len(input_df)} 条方案数据")
        except Exception as e:
            st.sidebar.error(f"读取失败: {e}")
            input_df = None
    else:
        input_df = None
else:
    # 保留原来的 parse_range 输入逻辑（可选）
    with st.sidebar.expander("1. 批量计算参数 (起始, 终止, 步长)", expanded=True):
        w_scale_in = st.text_input("风电装机规模 (MW)", "100")
        p_scale_in = st.text_input("光伏装机规模 (MW)", "100")
        s_scale_in = st.text_input("储能配置容量 (MWh)", "50")
        w_hours_in = st.text_input("风电等效利用小时数 (h)", "1500")
        p_hours_in = st.text_input("光伏等效利用小时数 (h)", "1500")
        w_price_in = st.text_input("风电含税电价 (元/kWh)", "0.33")
        p_price_in = st.text_input("光伏含税电价 (元/kWh)", "0.33")
        # 限电率：支持“起始, 终止, 步长”批量遍历（百分数，5 表示 5%）
        w_curt_in = st.text_input("风电限电率 (%)", "0", help="支持批量：如 0, 20, 5 表示 0%、5%、…、20%。")
        p_curt_in = st.text_input("光伏限电率 (%)", "0", help="支持批量：如 0, 20, 5 表示 0%、5%、…、20%。")
        # 实际小时数 = 等效小时数 × (1 − 限电率)，实时预览
        _w_equiv_preview = parse_range(w_hours_in)
        _p_equiv_preview = parse_range(p_hours_in)
        _w_curt_preview = _pct_to_fraction(parse_range(w_curt_in))
        _p_curt_preview = _pct_to_fraction(parse_range(p_curt_in))
        st.caption("**实际小时数 = 等效小时数 × (1 − 限电率)**")
        st.caption(f"风电实际小时数：{_fmt_hour_list(_actual_hours(_w_equiv_preview, _w_curt_preview))}")
        st.caption(f"光伏实际小时数：{_fmt_hour_list(_actual_hours(_p_equiv_preview, _p_curt_preview))}")


# 2. 投资与资金参数 (固定值)
with st.sidebar.expander("2. 投资与资金参数 (固定值)"):
    w_unit_inv = st.number_input("风电单位投资 (元/W)", value=float(pp.INVESTMENT_PARAMS['wind_unit_investment_per_w']), step=0.1)
    p_unit_inv = st.number_input("光伏单位投资 (元/Wp)", value=float(pp.INVESTMENT_PARAMS['pv_unit_investment_per_wp']), step=0.1)
    s_unit_inv = st.number_input("储能单位投资 (元/Wh)", value=float(pp.INVESTMENT_PARAMS['energy_storage_unit_investment_per_wh']), step=0.1)
    other_inv = st.number_input("其他非固定资产投资 (万元)", value=float(pp.INVESTMENT_PARAMS['other_non_fixed_asset_investment_million_yuan']), step=10.0)
    
    cap_ratio_pct = st.number_input("资本金比例 (%)", value=float(pp.INVESTMENT_PARAMS['capital_ratio']) * 100, format="%.2f", step=1.0, min_value=0.0, max_value=100.0)
    l_rate_pct = st.number_input("长期贷款利率 (%)", value=float(pp.INVESTMENT_PARAMS['long_term_loan_rate']) * 100, format="%.4f", step=0.05)
    s_rate_pct = st.number_input("短期贷款利率 (%)", value=float(pp.INVESTMENT_PARAMS['short_term_loan_rate']) * 100, format="%.4f", step=0.05)
    
    work_cap_kw = st.number_input("单位流动资金 (元/kW)", value=float(pp.INVESTMENT_PARAMS['working_capital_per_kw']), step=1.0)
    self_work_ratio_pct = st.number_input("自有流动资金比例 (%)", value=float(pp.INVESTMENT_PARAMS['self_owned_working_capital_ratio']) * 100, format="%.2f", step=1.0, min_value=0.0, max_value=100.0)
    
    repay_start = st.number_input("开始还款年份", value=int(pp.INVESTMENT_PARAMS['repayment_start_year']), step=1)
    repay_period = st.number_input("贷款偿还期限 (年)", value=int(pp.INVESTMENT_PARAMS['repayment_period']), step=1)
    
    repay_type = st.selectbox("还款方式", ["等额本息", "等额本金"], 
                             index=0 if pp.REPAYMENT_METHOD.get('equal_principal_and_interest') == 1 else 1)

# 3. 经营成本与设备更换 (固定值)
with st.sidebar.expander("3. 经营成本与设备更换 (固定值)"):
    w_op_cost = st.number_input("风电运维单价 (元/W)", value=float(pp.OPERATING_COST_PARAMS['wind_unit_operating_cost_per_w']), format="%.3f", step=0.001)
    p_op_cost = st.number_input("光伏运维单价 (元/Wp)", value=float(pp.OPERATING_COST_PARAMS['pv_unit_operating_cost_per_wp']), format="%.3f", step=0.001)
    s_op_cost = st.number_input("储能运维单价 (元/Wh)", value=float(pp.OPERATING_COST_PARAMS['energy_storage_unit_operating_cost_per_wh']), format="%.3f", step=0.001)
    
    replace_y1 = st.number_input("设备第一次更换年份", value=int(pp.OPERATING_COST_PARAMS['equipment_replacement_year']), step=1)
    replace_y2 = st.number_input("设备第二次更换年份", value=int(pp.OPERATING_COST_PARAMS.get('equipment_replacement_S_year', 14)), step=1)
    replace_price = st.number_input("设备更换单价 (元/Wh)", value=float(pp.OPERATING_COST_PARAMS['equipment_replacement_unit_price_per_wh']), step=0.01)

# 4. 光伏衰减与运营年限 (固定值)：小时数已移至“1. 批量计算参数”
with st.sidebar.expander("4. 光伏衰减与运营年限 (固定值)"):
    f_decline_pct = st.number_input("光伏首年衰减率 (%)", value=float(pp.POWER_GENERATION_PARAMS['first_year_decline']) * 100, format="%.3f", step=0.1, min_value=0.0, max_value=100.0)
    a_decline_pct = st.number_input("光伏后续逐年衰减率 (%)", value=float(pp.POWER_GENERATION_PARAMS['annual_decline']) * 100, format="%.3f", step=0.05, min_value=0.0, max_value=100.0)
    
    op_y_w = st.number_input("风电运营年限", value=int(pp.OPERATION_YEARS['wind']), step=1)
    op_y_p = st.number_input("光伏运营年限", value=int(pp.OPERATION_YEARS['pv']), step=1)

# 5. 税率/折旧/残值 (固定值)
with st.sidebar.expander("5. 税率、折旧与残值 (固定值)"):
    vat_pct = st.number_input("增值税率 (%)", value=float(pp.TAX_RATES['vat_rate']) * 100, format="%.2f", step=0.5, min_value=0.0, max_value=100.0)
    inc_std_pct = st.number_input("标准所得税率 (%)", value=float(pp.TAX_RATES['standard_income_tax_rate']) * 100, format="%.2f", step=0.5, min_value=0.0, max_value=100.0)
    
    w_dep_pct = st.number_input("风电折旧率 (%)", value=float(pp.DEPRECIATION_RATES['wind_depreciation_rate']) * 100, format="%.3f", step=0.05, min_value=0.0, max_value=100.0)
    p_dep_pct = st.number_input("光伏折旧率 (%)", value=float(pp.DEPRECIATION_RATES['pv_depreciation_rate']) * 100, format="%.3f", step=0.05, min_value=0.0, max_value=100.0)
    s_dep_pct = st.number_input("储能折旧率 (%)", value=float(pp.DEPRECIATION_RATES['energy_storage_depreciation_rate']) * 100, format="%.3f", step=0.05, min_value=0.0, max_value=100.0)
    salvage_pct = st.number_input("统一残值率 (%)", value=float(pp.SALVAGE_RATES['wind_salvage_rate']) * 100, format="%.2f", step=0.5, min_value=0.0, max_value=100.0)

# --- 将侧边栏固定的静态参数同步到全局参数模块（与具体方案无关，只需设置一次） ---
def sync_static_params():
    pp.INVESTMENT_PARAMS.update({
        'wind_unit_investment_per_w': w_unit_inv,
        'pv_unit_investment_per_wp': p_unit_inv,
        'energy_storage_unit_investment_per_wh': s_unit_inv,
        'other_non_fixed_asset_investment_million_yuan': other_inv,
        'capital_ratio': cap_ratio_pct / 100,
        'long_term_loan_rate': l_rate_pct / 100,
        'short_term_loan_rate': s_rate_pct / 100,
        'working_capital_per_kw': work_cap_kw,
        'self_owned_working_capital_ratio': self_work_ratio_pct / 100,
        'repayment_start_year': repay_start,
        'repayment_period': repay_period
    })

    pp.REPAYMENT_METHOD.clear()
    if repay_type == "等额本息":
        pp.REPAYMENT_METHOD['equal_principal_and_interest'] = 1
    else:
        pp.REPAYMENT_METHOD['equal_principal'] = 1

    pp.OPERATING_COST_PARAMS.update({
        'wind_unit_operating_cost_per_w': w_op_cost,
        'pv_unit_operating_cost_per_wp': p_op_cost,
        'energy_storage_unit_operating_cost_per_wh': s_op_cost,
        'equipment_replacement_year': replace_y1,
        'equipment_replacement_S_year': replace_y2,
        'equipment_replacement_unit_price_per_wh': replace_price
    })

    pp.POWER_GENERATION_PARAMS.update({
        'first_year_decline': f_decline_pct / 100,
        'annual_decline': a_decline_pct / 100
    })
    # 保持派生的“光致衰减 LID”与上述衰减率同步
    pp.POWER_GENERATION_PARAMS['photonic_decline'] = f_decline_pct / 100 - a_decline_pct / 100

    pp.TAX_RATES['vat_rate'] = vat_pct / 100
    pp.TAX_RATES['standard_income_tax_rate'] = inc_std_pct / 100
    pp.DEPRECIATION_RATES.update({
        'wind_depreciation_rate': w_dep_pct / 100,
        'pv_depreciation_rate': p_dep_pct / 100,
        'energy_storage_depreciation_rate': s_dep_pct / 100
    })
    pp.SALVAGE_RATES.update({
        'wind_salvage_rate': salvage_pct / 100,
        'pv_salvage_rate': salvage_pct / 100,
        'energy_storage_salvage_rate': salvage_pct / 100
    })
    pp.OPERATION_YEARS.update({'wind': op_y_w, 'pv': op_y_p})

# --- 按方案更新规模/小时数等可变参数，并重算派生参数（供正算与反推共用） ---
def apply_scenario_params(sc):
    pp.PROJECT_SCALE.update({
        'wind_mw': sc['wind_mw'],
        'pv_mw': sc['pv_mw'],
        'energy_storage_mwh': sc['storage_mwh']
    })
    pp.POWER_GENERATION_PARAMS.update({
        'wind_hours': sc['wind_hours'],
        'pv_first_year_hours': sc['pv_hours']
    })
    # --- 派生参数依赖各方案的规模/成本，必须逐方案重算 ---
    pp.INVESTMENT_RESULTS.clear()
    pp.INVESTMENT_RESULTS.update(pp._calculate_investments())
    pp.OPERATING_COST_RESULTS.clear()
    pp.OPERATING_COST_RESULTS.update(pp._calculate_operating_costs())

# --- 计算逻辑 ---
calc_mode = st.radio(
    "计算模式",
    ["⚡ 正向计算（电价 → IRR）", "🎯 反推电价（目标IRR → 综合电价）"],
    horizontal=True,
)
st.markdown("---")

if calc_mode == "⚡ 正向计算（电价 → IRR）":
    if st.button("🚀 开始批量方案计算"):
        scenarios = []

        if input_mode == "表格上传":
            if input_df is not None:
                col_map, missing, opt_map = match_table_columns(input_df)
                if missing:
                    st.error(
                        f"表格缺少必需列: {missing}。"
                        f"当前表格的列为: {list(input_df.columns)}，请修改表头后重新上传。"
                    )
                else:
                    rename_map = {v: k for k, v in col_map.items()}
                    rename_map.update({v: k for k, v in opt_map.items()})
                    input_df = input_df.rename(columns=rename_map)
                    has_w_cur = '风电限电率' in input_df.columns
                    has_p_cur = '光伏限电率' in input_df.columns
                    for _, row in input_df.iterrows():
                        w_equiv = _to_float(row['风电利用小时数 (h)'], 0.0) or 0.0
                        p_equiv = _to_float(row['光伏利用小时数 (h)'], 0.0) or 0.0
                        # 表格中的限电率为百分数（5 表示 5%），换算成小数参与计算
                        w_cur = ((_to_float(row['风电限电率'], 0.0) or 0.0) / 100.0) if has_w_cur else 0.0
                        p_cur = ((_to_float(row['光伏限电率'], 0.0) or 0.0) / 100.0) if has_p_cur else 0.0
                        scenarios.append({
                            'wind_mw': row['风电容量 (MW)'],
                            'pv_mw': row['光伏容量 (MW)'],
                            'storage_mwh': row['储能容量 (MWh)'],  # 或 row['储能容量 (kWh)']/1000
                            'wind_equiv_hours': w_equiv,
                            'pv_equiv_hours': p_equiv,
                            'w_curtail': w_cur,
                            'p_curtail': p_cur,
                            # 模型按“实际小时数 = 等效小时数 × (1 − 限电率)”计算发电量
                            'wind_hours': w_equiv * (1 - w_cur),
                            'pv_hours': p_equiv * (1 - p_cur),
                            'w_price': row['综合电价'],
                            'p_price': row['综合电价'],
                        })
            else:
                st.error("请先上传表格文件！")
        else:
            # 解析批量范围输入（规模、等效小时数、电价均支持“起始, 终止, 步长”）
            w_list = parse_range(w_scale_in)
            p_list = parse_range(p_scale_in)
            s_list = parse_range(s_scale_in)
            wh_list = parse_range(w_hours_in)
            ph_list = parse_range(p_hours_in)
            wc_list = _pct_to_fraction(parse_range(w_curt_in))
            pc_list = _pct_to_fraction(parse_range(p_curt_in))
            wp_list = parse_range(w_price_in)
            pp_list = parse_range(p_price_in)
            # 组合所有方案
            combinations = list(itertools.product(w_list, p_list, s_list, wh_list, ph_list, wc_list, pc_list, wp_list, pp_list))

            if not combinations:
                st.error("无法解析参数组合，请检查输入格式（应为：起始, 终止, 步长）。")
            else:
                for wm, pm, sm, wh, ph, wc, pc, wp, pp_val in combinations:
                    scenarios.append({
                        'wind_mw': wm, 'pv_mw': pm, 'storage_mwh': sm,
                        'wind_equiv_hours': wh, 'pv_equiv_hours': ph,
                        'w_curtail': wc, 'p_curtail': pc,
                        # 模型按“实际小时数 = 等效小时数 × (1 − 限电率)”计算发电量
                        'wind_hours': wh * (1 - wc),
                        'pv_hours': ph * (1 - pc),
                        'w_price': wp, 'p_price': pp_val,
                    })

        if scenarios:
            # --- 同步侧边栏固定的静态参数（与具体方案无关，只需设置一次，移出循环以提升性能） ---
            sync_static_params()

            results = []
            progress_bar = st.progress(0)
            num_scenarios = len(scenarios)

            for i, sc in enumerate(scenarios):
                # --- 更新每个方案的可变参数（规模、电价、利用小时数） ---
                pp.SELLING_PRICE_PARAMS.update({
                    'wind_price_per_kwh': sc['w_price'],
                    'pv_price_per_kwh': sc['p_price']
                })
                apply_scenario_params(sc)

                # --- 执行财务模型并提取结果 ---
                try:
                    data = get_financial_plan_cash_flow()
                    m = data['financing_cash_flow']

                    results.append({
                        "序号": i + 1,
                        "风电规模(MW)": sc['wind_mw'],
                        "光伏规模(MW)": sc['pv_mw'],
                        "储能规模(MWh)": sc['storage_mwh'],
                        "风电实际小时数(h)": round(sc.get('wind_hours', 0.0), 2),
                        "光伏实际小时数(h)": round(sc.get('pv_hours', 0.0), 2),
                        "风电电价": sc['w_price'],
                        "光伏电价": sc['p_price'],
                        "项目税后IRR": f"{m['P_post_irr_result']*100:.2f}%",
                        "资本金税后IRR": f"{m['C_post_irr_result']*100:.2f}%",
                        "项目税前IRR": f"{m.get('P_pre_irr_result', 0)*100:.2f}%",
                        "资本金税前IRR": f"{m.get('C_pre_irr_result', 0)*100:.2f}%",
                        "度电成本LCOE(元/kWh)": f"{m['LCOE']:.4f}"
                    })
                except Exception as e:
                    st.warning(f"方案 {i+1} 计算跳过: {e}")

                progress_bar.progress((i + 1) / num_scenarios)


            # --- 结果展示 ---
            st.subheader(f"方案计算完成 (共 {len(results)} 组)")
            df = pd.DataFrame(results)

            st.markdown("#### 完整批量方案明细表")
            st.dataframe(df, use_container_width=True, hide_index=True)

            # 导出 CSV
            csv = df.to_csv(index=False).encode('utf-8-sig')
            st.download_button("导出全量结果明细表", csv, "batch_valuation_results.csv", "text/csv")
        else:
            st.info("请在左侧配置批量参数（支持起始, 终止, 步长）和固定参数，点击按钮开始仿真。")

# ===================== 反推模式：目标 IRR → 综合电价 =====================
if calc_mode == "🎯 反推电价（目标IRR → 综合电价）":
    st.markdown("### 🎯 反推综合电价")
    st.caption(
        "除综合电价外，其余参数均保持当前侧边栏设置不变。"
        "输入目标 IRR，通过数值求解反推使该指标恰好达标所需的综合电价（风、光采用同一电价）。"
    )

    def _fmt_pct(x, digits=2):
        """IRR 百分比格式化，无法计算（nan/None）时显示占位符"""
        try:
            if x is None or (isinstance(x, float) and math.isnan(x)):
                return "—"
            return f"{x * 100:.{digits}f}%"
        except (TypeError, ValueError):
            return "—"

    IRR_TYPE_KEY_MAP = {
        "项目税后IRR": 'project_post',
        "项目税前IRR": 'project_pre',
        "资本金税后IRR": 'capital_post',
        "资本金税前IRR": 'capital_pre',
    }

    c1, c2 = st.columns(2)
    with c1:
        irr_type_label = st.selectbox("目标 IRR 类型", list(IRR_TYPE_KEY_MAP.keys()))
    with c2:
        target_irr_pct = st.number_input("目标 IRR (%)", value=8.0, step=0.1, format="%.4f")

    irr_type_key = IRR_TYPE_KEY_MAP[irr_type_label]
    target_irr = target_irr_pct / 100.0

    if st.button("🎯 开始反推综合电价"):
        scenarios = []

        if input_mode == "表格上传":
            if input_df is not None:
                col_map, missing, opt_map = match_table_columns(input_df, required=REQUIRED_TABLE_COLUMNS_REVERSE)
                if missing:
                    st.error(
                        f"表格缺少必需列: {missing}。"
                        f"当前表格的列为: {list(input_df.columns)}，请修改表头后重新上传。"
                    )
                else:
                    rename_map = {v: k for k, v in col_map.items()}
                    rename_map.update({v: k for k, v in opt_map.items()})
                    input_df = input_df.rename(columns=rename_map)
                    has_w_cur = '风电限电率' in input_df.columns
                    has_p_cur = '光伏限电率' in input_df.columns
                    for _, row in input_df.iterrows():
                        # 反推模式下表格中的综合电价列不参与计算（电价由求解得出）
                        w_equiv = _to_float(row['风电利用小时数 (h)'], 0.0) or 0.0
                        p_equiv = _to_float(row['光伏利用小时数 (h)'], 0.0) or 0.0
                        # 表格中的限电率为百分数（5 表示 5%），换算成小数参与计算
                        w_cur = ((_to_float(row['风电限电率'], 0.0) or 0.0) / 100.0) if has_w_cur else 0.0
                        p_cur = ((_to_float(row['光伏限电率'], 0.0) or 0.0) / 100.0) if has_p_cur else 0.0
                        scenarios.append({
                            'wind_mw': row['风电容量 (MW)'],
                            'pv_mw': row['光伏容量 (MW)'],
                            'storage_mwh': row['储能容量 (MWh)'],
                            'wind_equiv_hours': w_equiv,
                            'pv_equiv_hours': p_equiv,
                            'w_curtail': w_cur,
                            'p_curtail': p_cur,
                            # 模型按“实际小时数 = 等效小时数 × (1 − 限电率)”计算发电量
                            'wind_hours': w_equiv * (1 - w_cur),
                            'pv_hours': p_equiv * (1 - p_cur),
                        })
            else:
                st.error("请先上传表格文件！")
        else:
            # 手动模式：电价输入不参与反推，按规模与等效小时数组合遍历
            w_list = parse_range(w_scale_in)
            p_list = parse_range(p_scale_in)
            s_list = parse_range(s_scale_in)
            wh_list = parse_range(w_hours_in)
            ph_list = parse_range(p_hours_in)
            wc_list = _pct_to_fraction(parse_range(w_curt_in))
            pc_list = _pct_to_fraction(parse_range(p_curt_in))
            combinations = list(itertools.product(w_list, p_list, s_list, wh_list, ph_list, wc_list, pc_list))
            if not combinations:
                st.error("无法解析参数组合，请检查输入格式（应为：起始, 终止, 步长）。")
            else:
                for wm, pm, sm, wh, ph, wc, pc in combinations:
                    scenarios.append({
                        'wind_mw': wm, 'pv_mw': pm, 'storage_mwh': sm,
                        'wind_equiv_hours': wh, 'pv_equiv_hours': ph,
                        'w_curtail': wc, 'p_curtail': pc,
                        # 模型按“实际小时数 = 等效小时数 × (1 − 限电率)”计算发电量
                        'wind_hours': wh * (1 - wc),
                        'pv_hours': ph * (1 - pc),
                    })

        if scenarios:
            sync_static_params()
            results = []
            progress_bar = st.progress(0)
            num_scenarios = len(scenarios)

            for i, sc in enumerate(scenarios):
                apply_scenario_params(sc)
                try:
                    sol = solve_comprehensive_price(target_irr, irr_type=irr_type_key)
                    if sol['converged']:
                        results.append({
                            "序号": i + 1,
                            "风电规模(MW)": sc['wind_mw'],
                            "光伏规模(MW)": sc['pv_mw'],
                            "储能规模(MWh)": sc['storage_mwh'],
                            "风电实际小时数(h)": round(sc.get('wind_hours', 0.0), 2),
                            "光伏实际小时数(h)": round(sc.get('pv_hours', 0.0), 2),
                            "目标IRR类型": irr_type_label,
                            "目标IRR": f"{target_irr * 100:.2f}%",
                            "反推综合电价(元/kWh)": f"{sol['price']:.6f}",
                            "验证-目标IRR": _fmt_pct(sol['achieved_irr'], 4),
                            "验证-项目税后IRR": _fmt_pct(sol['P_post_irr_result']),
                            "验证-项目税前IRR": _fmt_pct(sol['P_pre_irr_result']),
                            "验证-资本金税后IRR": _fmt_pct(sol['C_post_irr_result']),
                            "验证-资本金税前IRR": _fmt_pct(sol['C_pre_irr_result']),
                            "度电成本LCOE(元/kWh)": f"{sol['LCOE']:.4f}" if sol['LCOE'] is not None else "—",
                            "备注": ""
                        })
                    else:
                        results.append({
                            "序号": i + 1,
                            "风电规模(MW)": sc['wind_mw'],
                            "光伏规模(MW)": sc['pv_mw'],
                            "储能规模(MWh)": sc['storage_mwh'],
                            "风电实际小时数(h)": round(sc.get('wind_hours', 0.0), 2),
                            "光伏实际小时数(h)": round(sc.get('pv_hours', 0.0), 2),
                            "目标IRR类型": irr_type_label,
                            "目标IRR": f"{target_irr * 100:.2f}%",
                            "反推综合电价(元/kWh)": "—",
                            "验证-目标IRR": "—",
                            "验证-项目税后IRR": "—",
                            "验证-项目税前IRR": "—",
                            "验证-资本金税后IRR": "—",
                            "验证-资本金税前IRR": "—",
                            "度电成本LCOE(元/kWh)": "—",
                            "备注": sol['message']
                        })
                except Exception as e:
                    st.warning(f"方案 {i + 1} 反推失败: {e}")

                progress_bar.progress((i + 1) / num_scenarios)

            st.subheader(f"反推完成 (共 {len(results)} 组)")
            df = pd.DataFrame(results)
            st.markdown("#### 反推结果明细表")
            st.dataframe(df, use_container_width=True, hide_index=True)

            # 导出 CSV
            csv = df.to_csv(index=False).encode('utf-8-sig')
            st.download_button("导出反推结果明细表", csv, "reverse_price_results.csv", "text/csv")
