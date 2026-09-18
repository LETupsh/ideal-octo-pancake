import streamlit as st

# ===================== 兼容性补丁  =====================
# 解决 streamlit-cookies-manager 内部调用已弃用/移除的 st.cache 问题
if not hasattr(st, "cache"):
    st.cache = st.cache_data

import pandas as pd
import numpy as np
import itertools
import io
import os
import datetime
import project_parameters as pp  # 导入全局参数模块
from financial_plan_cash_flow_model import get_financial_plan_cash_flow
from streamlit_cookies_manager import EncryptedCookieManager

# --- 用户数据库（与 A-energy_app.py 一致） ---
USER_CREDENTIALS = {
    "msj01": "888888",
    "cyt01": "888888",
    "user01": "000000"
}

# 设置页面配置
st.set_page_config(page_title="风光储系统经济性评价平台", layout="wide")

# --- Cookie 管理配置（与 A-energy_app.py 完全一致的 EncryptedCookieManager 方式） ---
cookies = EncryptedCookieManager(
    prefix="energy-app/",
    password=os.environ.get("COOKIES_PASSWORD", "a_very_secret_password_12345")
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

# 原有应用代码从这里开始...
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
    '风电利用小时数 (h)': ['风电利用小时数(h)', '风电利用小时'],
    '光伏利用小时数 (h)': ['光伏利用小时数(h)', '光伏利用小时'],
    '储能容量 (MWh)': ['储能容量(MWh)', '储能容量', '储能规模(MWh)'],
    '综合电价': ['综合上网电价', '电价'],
}

def match_table_columns(df):
    """
    将表格列名映射到标准列名；无法识别的返回缺失清单。
    兼容：多余空格、全角括号（）、部分常见别名。
    """
    def norm(c):
        return str(c).replace('（', '(').replace('）', ')').replace(' ', '').strip()

    norm_to_actual = {norm(c): c for c in df.columns}
    col_map = {}
    for std_name, aliases in REQUIRED_TABLE_COLUMNS.items():
        for cand in [std_name] + aliases:
            if cand in norm_to_actual:
                col_map[std_name] = norm_to_actual[cand]
                break
    missing = [k for k in REQUIRED_TABLE_COLUMNS if k not in col_map]
    return col_map, missing

# --- 侧边栏：参数输入模式切换 ---
st.sidebar.header("📋 参数配置")
input_mode = st.sidebar.radio("选择输入模式", ["表格上传", "手动步长输入"])

if input_mode == "表格上传":
    uploaded_file = st.sidebar.file_uploader("上传项目参数表 (Excel/CSV)", type=["xlsx", "csv"])
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
    with st.sidebar.expander("1. 批量遍历参数 (起始, 终止, 步长)", expanded=True):
        w_scale_in = st.text_input("风电装机规模 (MW)", "100")
        p_scale_in = st.text_input("光伏装机规模 (MW)", "100")
        s_scale_in = st.text_input("储能配置容量 (MWh)", "50")
        w_price_in = st.text_input("风电含税电价 (元/kWh)", "0.33")
        p_price_in = st.text_input("光伏含税电价 (元/kWh)", "0.33")

# 2. 投资与资金参数 (固定值)
with st.sidebar.expander("2. 投资与资金参数 (固定值)"):
    w_unit_inv = st.number_input("风电单位投资 (元/W)", value=float(pp.INVESTMENT_PARAMS['wind_unit_investment_per_w']), step=0.1)
    p_unit_inv = st.number_input("光伏单位投资 (元/Wp)", value=float(pp.INVESTMENT_PARAMS['pv_unit_investment_per_wp']), step=0.1)
    s_unit_inv = st.number_input("储能单位投资 (元/Wh)", value=float(pp.INVESTMENT_PARAMS['energy_storage_unit_investment_per_wh']), step=0.1)
    other_inv = st.number_input("其他非固定资产投资 (万元)", value=float(pp.INVESTMENT_PARAMS['other_non_fixed_asset_investment_million_yuan']), step=10.0)
    
    cap_ratio = st.number_input("资本金比例", value=float(pp.INVESTMENT_PARAMS['capital_ratio']), format="%.4f", step=0.01)
    l_rate = st.number_input("长期贷款利率", value=float(pp.INVESTMENT_PARAMS['long_term_loan_rate']), format="%.4f", step=0.0001)
    s_rate = st.number_input("短期贷款利率", value=float(pp.INVESTMENT_PARAMS['short_term_loan_rate']), format="%.4f", step=0.0001)
    
    work_cap_kw = st.number_input("单位流动资金 (元/kW)", value=float(pp.INVESTMENT_PARAMS['working_capital_per_kw']), step=1.0)
    self_work_ratio = st.number_input("自有流动资金比例", value=float(pp.INVESTMENT_PARAMS['self_owned_working_capital_ratio']), format="%.4f", step=0.01)
    
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

# 4. 发电小时数与衰减 (固定值)
with st.sidebar.expander("4. 发电与年限参数 (固定值)"):
    w_h = st.number_input("风电等效利用小时数", value=float(pp.POWER_GENERATION_PARAMS['wind_hours']), step=10.0)
    p_h = st.number_input("光伏首年利用小时数", value=float(pp.POWER_GENERATION_PARAMS['pv_first_year_hours']), step=10.0)
    f_decline = st.number_input("首年衰减率", value=float(pp.POWER_GENERATION_PARAMS['first_year_decline']), format="%.4f", step=0.001)
    a_decline = st.number_input("后续逐年衰减率", value=float(pp.POWER_GENERATION_PARAMS['annual_decline']), format="%.4f", step=0.0001)
    
    op_y_w = st.number_input("风电运营年限", value=int(pp.OPERATION_YEARS['wind']), step=1)
    op_y_p = st.number_input("光伏运营年限", value=int(pp.OPERATION_YEARS['pv']), step=1)

# 5. 税率/折旧/残值 (固定值)
with st.sidebar.expander("5. 税率、折旧与残值 (固定值)"):
    vat = st.number_input("增值税率", value=float(pp.TAX_RATES['vat_rate']), step=0.01)
    inc_std = st.number_input("标准所得税率", value=float(pp.TAX_RATES['standard_income_tax_rate']), step=0.01)
    
    w_dep = st.number_input("风电折旧率", value=float(pp.DEPRECIATION_RATES['wind_depreciation_rate']), format="%.4f", step=0.0001)
    p_dep = st.number_input("光伏折旧率", value=float(pp.DEPRECIATION_RATES['pv_depreciation_rate']), format="%.4f", step=0.0001)
    s_dep = st.number_input("储能折旧率", value=float(pp.DEPRECIATION_RATES['energy_storage_depreciation_rate']), format="%.4f", step=0.0001)
    salvage = st.number_input("统一残值率", value=float(pp.SALVAGE_RATES['wind_salvage_rate']), step=0.01)

# --- 计算逻辑 ---
if st.button("🚀 开始批量方案计算"):
    scenarios = []

    if input_mode == "表格上传":
        if input_df is not None:
            col_map, missing = match_table_columns(input_df)
            if missing:
                st.error(
                    f"表格缺少必需列: {missing}。"
                    f"当前表格的列为: {list(input_df.columns)}，请修改表头后重新上传。"
                )
            else:
                input_df = input_df.rename(columns={v: k for k, v in col_map.items()})
                for _, row in input_df.iterrows():
                    scenarios.append({
                        'wind_mw': row['风电容量 (MW)'],
                        'pv_mw': row['光伏容量 (MW)'],
                        'wind_hours': row['风电利用小时数 (h)'],
                        'pv_hours': row['光伏利用小时数 (h)'],
                        'storage_mwh': row['储能容量 (MWh)'], # 或 row['储能容量 (kWh)']/1000
                        'w_price': row['综合电价'],
                        'p_price': row['综合电价'],
                    })
        else:
            st.error("请先上传表格文件！")
    else:
        # 解析批量范围输入
        w_list = parse_range(w_scale_in)
        p_list = parse_range(p_scale_in)
        s_list = parse_range(s_scale_in)
        wp_list = parse_range(w_price_in)
        pp_list = parse_range(p_price_in)
        # 组合所有方案
        combinations = list(itertools.product(w_list, p_list, s_list, wp_list, pp_list))

        if not combinations:
            st.error("无法解析参数组合，请检查输入格式（应为：起始, 终止, 步长）。")
        else:
            for wm, pm, sm, wp, pp_val in combinations:
                scenarios.append({'wind_mw': wm, 'pv_mw': pm, 'storage_mwh': sm, 'wind_hours': w_h, 'pv_hours': p_h, 'w_price': wp, 'p_price': pp_val})

    if scenarios:
        # --- 同步侧边栏固定的静态参数（与具体方案无关，只需设置一次，移出循环以提升性能） ---
        pp.INVESTMENT_PARAMS.update({
            'wind_unit_investment_per_w': w_unit_inv,
            'pv_unit_investment_per_wp': p_unit_inv,
            'energy_storage_unit_investment_per_wh': s_unit_inv,
            'other_non_fixed_asset_investment_million_yuan': other_inv,
            'capital_ratio': cap_ratio,
            'long_term_loan_rate': l_rate,
            'short_term_loan_rate': s_rate,
            'working_capital_per_kw': work_cap_kw,
            'self_owned_working_capital_ratio': self_work_ratio,
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
            'first_year_decline': f_decline,
            'annual_decline': a_decline
        })

        pp.TAX_RATES['vat_rate'] = vat
        pp.TAX_RATES['standard_income_tax_rate'] = inc_std
        pp.DEPRECIATION_RATES.update({
            'wind_depreciation_rate': w_dep,
            'pv_depreciation_rate': p_dep,
            'energy_storage_depreciation_rate': s_dep
        })
        pp.SALVAGE_RATES.update({
            'wind_salvage_rate': salvage,
            'pv_salvage_rate': salvage,
            'energy_storage_salvage_rate': salvage
        })
        pp.OPERATION_YEARS.update({'wind': op_y_w, 'pv': op_y_p})

        results = []
        progress_bar = st.progress(0)
        num_scenarios = len(scenarios)

        for i, sc in enumerate(scenarios):
            # --- 更新每个方案的可变参数（规模、电价、利用小时数） ---
            pp.PROJECT_SCALE.update({
                'wind_mw': sc['wind_mw'],
                'pv_mw': sc['pv_mw'],
                'energy_storage_mwh': sc['storage_mwh']
            })
            pp.SELLING_PRICE_PARAMS.update({
                'wind_price_per_kwh': sc['w_price'],
                'pv_price_per_kwh': sc['p_price']
            })
            pp.POWER_GENERATION_PARAMS.update({
                'wind_hours': sc['wind_hours'],
                'pv_first_year_hours': sc['pv_hours']
            })

            # --- 派生参数依赖各方案的规模/成本，必须在循环内逐方案重算 ---
            pp.INVESTMENT_RESULTS.clear()
            pp.INVESTMENT_RESULTS.update(pp._calculate_investments())
            pp.OPERATING_COST_RESULTS.clear()
            pp.OPERATING_COST_RESULTS.update(pp._calculate_operating_costs())

            # --- 执行财务模型并提取结果 ---
            try:
                data = get_financial_plan_cash_flow(total_years=26)
                m = data['financing_cash_flow']

                results.append({
                    "序号": i + 1,
                    "风电规模(MW)": sc['wind_mw'],
                    "光伏规模(MW)": sc['pv_mw'],
                    "储能规模(MWh)": sc['storage_mwh'],
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
