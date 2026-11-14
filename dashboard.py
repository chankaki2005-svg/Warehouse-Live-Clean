"""
新輝雲端自動化後台 — 儀表板
Streamlit dashboard for warehouse inventory, anomaly alerts, workers, and project data.
"""

import base64
import mimetypes
import os
import json
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta
from pathlib import Path

import streamlit as st
import pandas as pd

# ── 檔案類型常量 ────────────────────────────────────────────────
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff", ".svg"}
PDF_EXTENSIONS = {".pdf"}
ASSETS_DIR = Path("attached_assets")


def scan_assets_dir(directory: Path) -> list[dict]:
    """Recursively scan a directory for image and PDF files, return metadata list."""
    results = []
    if not directory.exists() or not directory.is_dir():
        return results
    for f in sorted(directory.rglob("*")):
        if not f.is_file():
            continue
        ext = f.suffix.lower()
        if ext not in IMAGE_EXTENSIONS and ext not in PDF_EXTENSIONS:
            continue
        stat = f.stat()
        file_type = "image" if ext in IMAGE_EXTENSIONS else "pdf"
        results.append({
            "path": str(f),
            "relative_path": str(f.relative_to(directory)),
            "name": f.name,
            "ext": ext,
            "type": file_type,
            "size": stat.st_size,
            "size_kb": round(stat.st_size / 1024, 1),
            "modified": datetime.fromtimestamp(stat.st_mtime, tz=HKT).strftime("%Y-%m-%d %H:%M"),
            "parent": str(f.parent.relative_to(directory)),
        })
    return results

# ── Supabase 設定 ──────────────────────────────────────────────
SUPABASE_URL = os.environ.get("VITE_SUPABASE_URL", "").rstrip("/")
SUPABASE_ANON_KEY = os.environ.get("VITE_SUPABASE_ANON_KEY", "")
HKT = timezone(timedelta(hours=8))


def supabase_get(table: str, select: str = "*", limit: int = 1000, order: str | None = None):
    """GET rows from a Supabase table via the REST API."""
    if not SUPABASE_URL or not SUPABASE_ANON_KEY:
        st.error("Supabase 連線未設定。請檢查環境變數 VITE_SUPABASE_URL 和 VITE_SUPABASE_ANON_KEY。")
        return []
    params = {"select": select, "limit": str(limit)}
    if order:
        params["order"] = order
    query = urllib.parse.urlencode(params)
    url = f"{SUPABASE_URL}/rest/v1/{table}?{query}"
    req = urllib.request.Request(url, headers={
        "apikey": SUPABASE_ANON_KEY,
        "Authorization": f"Bearer {SUPABASE_ANON_KEY}",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except Exception as e:
        st.error(f"讀取 {table} 失敗：{e}")
        return []


def ts_to_str(ts, fmt="%Y-%m-%d %H:%M"):
    """Convert epoch-millis (bigint) to HKT string."""
    if ts is None:
        return ""
    try:
        return datetime.fromtimestamp(ts / 1000, tz=HKT).strftime(fmt)
    except (TypeError, ValueError, OSError):
        return str(ts)


def ts_to_date_str(ts):
    return ts_to_str(ts, "%Y-%m-%d")


# ── 頁面設定 ────────────────────────────────────────────────────
st.set_page_config(
    page_title="新輝雲端自動化後台",
    page_icon="🏢",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown("""
<style>
    /* 主色調：深藍 + 銀灰，工程公司風格 */
    :root {
        --sf-primary: #1a3a5c;
        --sf-accent: #2e7d32;
        --sf-warning: #f57c00;
        --sf-danger: #c62828;
    }
    .main .block-container { padding-top: 1.5rem; max-width: 1400px; }
    h1, h2, h3 { color: var(--sf-primary); }
    .metric-card {
        background: #f8f9fa; border-radius: 10px; padding: 1rem;
        border-left: 4px solid var(--sf-primary); margin-bottom: 0.5rem;
    }
    .stMetric { background: #f8f9fa; border-radius: 8px; padding: 0.8rem; }
    div[data-testid="stMetricValue"] { font-size: 1.6rem; }
    .severity-critical { color: #c62828; font-weight: bold; }
    .severity-warning { color: #f57c00; font-weight: bold; }
    .severity-info { color: #1565c0; }
    .ack-yes { color: #2e7d32; }
    .ack-no { color: #c62828; font-weight: bold; }
</style>
""", unsafe_allow_html=True)

st.title("🏢 新輝雲端自動化後台")
st.caption("Sin Fai Cloud Automation Backend — 倉庫管理儀表板")
st.divider()

# ── 側邊欄導航 ──────────────────────────────────────────────────
st.sidebar.title("導航")
page = st.sidebar.radio("選擇頁面", [
    "📊 總覽",
    "📦 倉庫庫存",
    "🚨 異常警報",
    "👷 工人管理",
    "📄 報價項目",
    "💰 財務報表",
    "🔴 石油氣價格",
    "🏢 公司資料",
    "📦 供應商",
    "📅 請假記錄",
    "🖼️ 圖紙庫",
    "📷 相片與文件",
])

# ── 頁面：總覽 ──────────────────────────────────────────────────
if page == "📊 總覽":
    st.header("📊 系統總覽")

    col1, col2, col3, col4 = st.columns(4)

    anomalies = supabase_get("anomaly_alerts", select="id,acknowledged", limit=1000)
    unack = [a for a in anomalies if not a.get("acknowledged")]
    col1.metric("異常警報總數", len(anomalies), f"{len(unack)} 待處理" if unack else "全部已確認")

    workers = supabase_get("workers", select="id", limit=1000)
    col2.metric("工人人數", len(workers))

    quotations = supabase_get("quotation_projects", select="id", limit=1000)
    col3.metric("報價項目", len(quotations))

    companies = supabase_get("companies", select="id", limit=100)
    col4.metric("公司數目", len(companies))

    st.divider()

    st.subheader("最近異常警報")
    recent_alerts = supabase_get(
        "anomaly_alerts",
        select="id,item_name,type,severity,description,quantity,current_stock,acknowledged,acknowledged_by,created_at",
        limit=10,
        order="created_at.desc",
    )
    if recent_alerts:
        alert_df = pd.DataFrame(recent_alerts)
        alert_df["時間"] = alert_df["created_at"].apply(ts_to_str)
        alert_df["已確認"] = alert_df["acknowledged"].apply(lambda x: "✅ 是" if x else "❌ 否")
        display_cols = ["時間", "item_name", "type", "severity", "description", "quantity", "current_stock", "已確認", "acknowledged_by"]
        st.dataframe(alert_df[display_cols].rename(columns={
            "item_name": "物料", "type": "類型", "severity": "嚴重程度",
            "description": "描述", "quantity": "數量", "current_stock": "當前庫存",
            "acknowledged_by": "確認人",
        }), use_container_width=True, hide_index=True)
    else:
        st.info("暫無異常警報記錄。")

    st.divider()

    st.subheader("報價項目概覽")
    quote_data = supabase_get("quotation_projects", select="id,name,gas_type,company_id,created_at,updated_at", limit=20, order="updated_at.desc")
    if quote_data:
        qdf = pd.DataFrame(quote_data)
        qdf["建立日期"] = qdf["created_at"].apply(ts_to_date_str)
        qdf["更新日期"] = qdf["updated_at"].apply(ts_to_date_str)
        st.dataframe(qdf[["name", "gas_type", "company_id", "建立日期", "更新日期"]].rename(columns={
            "name": "項目名稱", "gas_type": "氣體類型", "company_id": "公司",
        }), use_container_width=True, hide_index=True)
    else:
        st.info("暫無報價項目。")


# ── 頁面：倉庫庫存 ──────────────────────────────────────────────
elif page == "📦 倉庫庫存":
    st.header("📦 倉庫庫存")

    items = supabase_get("items", select="*", limit=1000)
    transactions = supabase_get("transactions", select="*", limit=500, order="timestamp.desc")

    if items:
        idf = pd.DataFrame(items)
        st.subheader(f"物料清單（共 {len(idf)} 項）")
        st.dataframe(idf, use_container_width=True, hide_index=True)
    else:
        st.info("物料表目前沒有資料。請透過 App 或 WhatsApp 上傳庫存記錄。")

    st.divider()

    if transactions:
        tdf = pd.DataFrame(transactions)
        if "timestamp" in tdf.columns:
            tdf["時間"] = tdf["timestamp"].apply(ts_to_str)
        st.subheader(f"最近交易記錄（共 {len(tdf)} 筆）")
        st.dataframe(tdf, use_container_width=True, hide_index=True)
    else:
        st.info("暫無交易記錄。")


# ── 頁面：異常警報 ──────────────────────────────────────────────
elif page == "🚨 異常警報":
    st.header("🚨 異常警報")

    alerts = supabase_get(
        "anomaly_alerts",
        select="id,item_id,item_name,type,severity,description,quantity,current_stock,acknowledged,acknowledged_by,created_at",
        limit=1000,
        order="created_at.desc",
    )

    if alerts:
        adf = pd.DataFrame(alerts)
        adf["時間"] = adf["created_at"].apply(ts_to_str)
        adf["已確認"] = adf["acknowledged"].apply(lambda x: "✅ 是" if x else "❌ 否")

        col1, col2 = st.columns(2)
        with col1:
            severity_filter = st.multiselect("按嚴重程度篩選", adf["severity"].unique().tolist())
        with col2:
            ack_filter = st.radio("確認狀態", ["全部", "待處理", "已確認"])

        filtered = adf
        if severity_filter:
            filtered = filtered[filtered["severity"].isin(severity_filter)]
        if ack_filter == "待處理":
            filtered = filtered[~filtered["acknowledged"]]
        elif ack_filter == "已確認":
            filtered = filtered[filtered["acknowledged"]]

        st.subheader(f"警報列表（篩選後 {len(filtered)} 筆 / 共 {len(adf)} 筆）")

        def severity_badge(val):
            if val == "critical":
                return f'<span class="severity-critical">🔴 嚴重</span>'
            elif val == "warning":
                return f'<span class="severity-warning">🟡 警告</span>'
            return f'<span class="severity-info">🔵 資訊</span>'

        display_df = filtered[["時間", "item_name", "type", "severity", "description", "quantity", "current_stock", "已確認", "acknowledged_by"]].copy()
        st.dataframe(display_df.rename(columns={
            "item_name": "物料", "type": "類型", "severity": "嚴重程度",
            "description": "描述", "quantity": "數量", "current_stock": "當前庫存",
            "acknowledged_by": "確認人",
        }), use_container_width=True, hide_index=True)
    else:
        st.info("暫無異常警報記錄。")


# ── 頁面：工人管理 ──────────────────────────────────────────────
elif page == "👷 工人管理":
    st.header("👷 工人管理")

    workers = supabase_get("workers", select="id,name,phone,trade,note,created_at,company_id", limit=1000, order="created_at.desc")
    licenses = supabase_get("worker_licenses", select="*", limit=1000)

    if workers:
        wdf = pd.DataFrame(workers)
        wdf["註冊日期"] = wdf["created_at"].apply(ts_to_date_str)

        search = st.text_input("搜尋工人姓名或工種", "")
        if search:
            mask = wdf["name"].str.contains(search, case=False, na=False) | wdf["trade"].str.contains(search, case=False, na=False)
            wdf = wdf[mask]

        st.subheader(f"工人列表（共 {len(wdf)} 人）")
        st.dataframe(wdf[["name", "phone", "trade", "note", "company_id", "註冊日期"]].rename(columns={
            "name": "姓名", "phone": "電話", "trade": "工種", "note": "備註", "company_id": "公司",
        }), use_container_width=True, hide_index=True)
    else:
        st.info("暫無工人記錄。")

    st.divider()

    if licenses:
        ldf = pd.DataFrame(licenses)
        st.subheader(f"工人牌照（共 {len(ldf)} 筆）")
        st.dataframe(ldf, use_container_width=True, hide_index=True)
    else:
        st.info("暫無工人牌照記錄。")


# ── 頁面：報價項目 ──────────────────────────────────────────────
elif page == "📄 報價項目":
    st.header("📄 報價項目")

    projects = supabase_get("quotation_projects", select="*", limit=1000, order="updated_at.desc")
    sections = supabase_get("quotation_project_sections", select="*", limit=1000)
    proj_items = supabase_get("quotation_project_items", select="*", limit=1000)

    if projects:
        pdf = pd.DataFrame(projects)
        pdf["建立日期"] = pdf["created_at"].apply(ts_to_date_str)
        pdf["更新日期"] = pdf["updated_at"].apply(ts_to_date_str)
        st.subheader(f"項目列表（共 {len(pdf)} 個）")
        st.dataframe(pdf[["name", "gas_type", "company_id", "建立日期", "更新日期"]].rename(columns={
            "name": "項目名稱", "gas_type": "氣體類型", "company_id": "公司",
        }), use_container_width=True, hide_index=True)

        st.divider()
        selected = st.selectbox("選擇項目查看詳情", pdf["name"].tolist() if "name" in pdf.columns else [])
        if selected and "id" in pdf.columns:
            proj_id = pdf.loc[pdf["name"] == selected, "id"].values[0]
            if sections:
                sdf = pd.DataFrame(sections)
                proj_sections = sdf[sdf.get("project_id", "") == proj_id]
                if not proj_sections.empty:
                    st.subheader("分項 sections")
                    st.dataframe(proj_sections, use_container_width=True, hide_index=True)
            if proj_items:
                idf2 = pd.DataFrame(proj_items)
                proj_i = idf2[idf2.get("project_id", "") == proj_id]
                if not proj_i.empty:
                    st.subheader("項目物料")
                    st.dataframe(proj_i, use_container_width=True, hide_index=True)
    else:
        st.info("暫無報價項目。")


# ── 頁面：財務報表 ──────────────────────────────────────────────
elif page == "💰 財務報表":
    st.header("💰 財務報表")

    cf = supabase_get("cashflow_statements", select="*", limit=100, order="generated_at.desc")
    invoices = supabase_get("invoices", select="*", limit=100, order="created_at.desc")
    expenses = supabase_get("expense_invoices", select="*", limit=100, order="created_at.desc")

    if cf:
        cdf = pd.DataFrame(cf)
        cdf["生成時間"] = cdf["generated_at"].apply(ts_to_str)

        col1, col2, col3 = st.columns(3)
        col1.metric("總收入", f"HK$ {cdf['income_total'].sum():,.2f}")
        col2.metric("總支出", f"HK$ {cdf['expense_total'].sum():,.2f}")
        col3.metric("淨結餘", f"HK$ {cdf['net_balance'].sum():,.2f}")

        st.subheader("現金流量表")
        st.dataframe(cdf[["company_name", "period", "income_total", "expense_total", "net_balance", "entry_count", "生成時間"]].rename(columns={
            "company_name": "公司", "period": "期間", "income_total": "收入",
            "expense_total": "支出", "net_balance": "淨結餘", "entry_count": "記錄數",
        }), use_container_width=True, hide_index=True)
    else:
        st.info("暫無現金流量記錄。")

    st.divider()

    if invoices:
        st.subheader(f"發票（共 {len(invoices)} 筆）")
        st.dataframe(pd.DataFrame(invoices), use_container_width=True, hide_index=True)

    if expenses:
        st.subheader(f"支出發票（共 {len(expenses)} 筆）")
        st.dataframe(pd.DataFrame(expenses), use_container_width=True, hide_index=True)


# ── 頁面：石油氣價格 ────────────────────────────────────────────
elif page == "🔴 石油氣價格":
    st.header("🔴 石油氣價格")

    periods = supabase_get("gas_price_periods", select="*", limit=50, order="effective_from.desc")
    items = supabase_get("gas_price_items", select="*", limit=1000)

    if periods:
        pdf3 = pd.DataFrame(periods)
        st.subheader("價格期段")
        st.dataframe(pdf3, use_container_width=True, hide_index=True)
    else:
        st.info("暫無價格期段記錄。")

    st.divider()

    if items:
        idf3 = pd.DataFrame(items)
        st.subheader(f"價格項目（共 {len(idf3)} 項）")
        st.dataframe(idf3, use_container_width=True, hide_index=True)
    else:
        st.info("暫無石油氣價格項目。")


# ── 頁面：公司資料 ──────────────────────────────────────────────
elif page == "🏢 公司資料":
    st.header("🏢 公司資料")

    companies = supabase_get("companies", select="*", limit=100, order="created_at.desc")
    if companies:
        cdf2 = pd.DataFrame(companies)
        for _, row in cdf2.iterrows():
            with st.expander(f"**{row['name']}** ({row.get('code', '')})", expanded=False):
                col1, col2 = st.columns(2)
                with col1:
                    st.write(f"**中文名稱：** {row['name']}")
                    st.write(f"**英文名稱：** {row.get('english_name', '')}")
                    st.write(f"**代號：** {row.get('code', '')}")
                    st.write(f"**電話：** {row.get('tel', '')}")
                    st.write(f"**傳真：** {row.get('fax', '')}")
                with col2:
                    st.write(f"**地址：** {row.get('address', '')}")
                    st.write(f"**英文地址：** {row.get('address_en', '')}")
                    st.write(f"**聯絡人：** {row.get('po_contact', '')}")
                    st.write(f"**建立日期：** {ts_to_date_str(row.get('created_at'))}")
    else:
        st.info("暫無公司記錄。")


# ── 頁面：供應商 ────────────────────────────────────────────────
elif page == "📦 供應商":
    st.header("📦 供應商")

    suppliers = supabase_get("suppliers", select="*", limit=100, order="created_at.desc")
    if suppliers:
        sdf2 = pd.DataFrame(suppliers)
        sdf2["建立日期"] = sdf2["created_at"].apply(ts_to_date_str) if "created_at" in sdf2.columns else ""
        st.dataframe(sdf2, use_container_width=True, hide_index=True)
    else:
        st.info("暫無供應商記錄。")


# ── 頁面：請假記錄 ──────────────────────────────────────────────
elif page == "📅 請假記錄":
    st.header("📅 請假記錄")

    leaves = supabase_get("leave_records", select="*", limit=500, order="created_at.desc")
    if leaves:
        ldf2 = pd.DataFrame(leaves)
        if "created_at" in ldf2.columns:
            ldf2["申請日期"] = ldf2["created_at"].apply(ts_to_date_str)
        st.dataframe(ldf2, use_container_width=True, hide_index=True)
    else:
        st.info("暫無請假記錄。")


# ── 頁面：圖紙庫 ────────────────────────────────────────────────
elif page == "🖼️ 圖紙庫":
    st.header("🖼️ 圖紙庫")

    drawings = supabase_get("drawing_library", select="*", limit=100, order="created_at.desc")
    if drawings:
        ddf = pd.DataFrame(drawings)
        ddf["上傳日期"] = ddf["created_at"].apply(ts_to_date_str)
        st.subheader(f"資料庫圖紙列表（共 {len(ddf)} 份）")
        st.dataframe(ddf[["name", "mime_type", "size", "上傳日期", "company_id"]].rename(columns={
            "name": "檔案名稱", "mime_type": "類型", "size": "大小 (bytes)", "company_id": "公司",
        }), use_container_width=True, hide_index=True)
    else:
        st.info("資料庫暫無圖紙記錄。")

    st.divider()

    st.subheader("attached_assets 本機圖紙與 PDF")
    asset_files = scan_assets_dir(ASSETS_DIR)
    if asset_files:
        pdf_files = [f for f in asset_files if f["type"] == "pdf"]
        image_files = [f for f in asset_files if f["type"] == "image"]
        col1, col2 = st.columns(2)
        col1.metric("圖片檔案", len(image_files))
        col2.metric("PDF 檔案", len(pdf_files))

        if pdf_files:
            st.subheader("PDF 文件")
            for f in pdf_files:
                with st.expander(f"📄 {f['relative_path']}（{f['size_kb']} KB）"):
                    with open(f["path"], "rb") as fh:
                        st.download_button(
                            f"下載 {f['name']}",
                            fh.read(),
                            file_name=f["name"],
                            mime="application/pdf",
                            key=f"dl_pdf_{f['path']}",
                        )
    else:
        st.info(f"{ASSETS_DIR}/ 資料夾中沒有找到圖片或 PDF 檔案。")

    st.divider()
    st.subheader("其他本機檔案夾")
    other_dirs = [Path("pending_drawings"), Path("daily_reports")]
    found_any = False
    for d in other_dirs:
        if d.exists() and d.is_dir():
            files = [f for f in d.rglob("*") if f.is_file()]
            if files:
                found_any = True
                st.write(f"**{d}/** （{len(files)} 個檔案）")
                for f in sorted(files):
                    size_kb = f.stat().st_size / 1024
                    st.write(f"  - {f.relative_to(d)}（{size_kb:.1f} KB）")
    if not found_any:
        st.info("pending_drawings / daily_reports 資料夾為空或不存在。")


# ── 頁面：相片與文件 ────────────────────────────────────────────
elif page == "📷 相片與文件":
    st.header("📷 相片與文件")
    st.caption(f"掃描資料夾：{ASSETS_DIR}/")

    asset_files = scan_assets_dir(ASSETS_DIR)

    if not asset_files:
        st.warning(f"{ASSETS_DIR}/ 資料夾不存在或沒有任何圖片 / PDF 檔案。")
        st.info("請確保 attached_assets 資料夾已放在專案根目錄，並包含相片（.png / .jpg / .jpeg / .gif / .webp）或 PDF（.pdf）檔案。")
    else:
        image_files = [f for f in asset_files if f["type"] == "image"]
        pdf_files = [f for f in asset_files if f["type"] == "pdf"]

        col1, col2, col3 = st.columns(3)
        col1.metric("圖片總數", len(image_files))
        col2.metric("PDF 總數", len(pdf_files))
        col3.metric("總大小", f"{sum(f['size_kb'] for f in asset_files):.1f} KB")

        st.divider()

        tab_images, tab_pdfs, tab_all = st.tabs([f"📷 圖片（{len(image_files)}）", f"📄 PDF（{len(pdf_files)}）", "📋 全部檔案"])

        with tab_images:
            if not image_files:
                st.info("沒有圖片檔案。")
            else:
                subdirs = sorted(set(f["parent"] for f in image_files))
                selected_dir = st.selectbox("按子資料夾篩選", ["全部"] + subdirs, key="img_dir_filter")
                filtered_imgs = image_files if selected_dir == "全部" else [f for f in image_files if f["parent"] == selected_dir]

                st.subheader(f"圖片畫廊（{len(filtered_imgs)} 張）")
                cols_per_row = 4
                for i in range(0, len(filtered_imgs), cols_per_row):
                    row_imgs = filtered_imgs[i:i + cols_per_row]
                    cols = st.columns(cols_per_row)
                    for col, img in zip(cols, row_imgs):
                        with col:
                            st.image(img["path"], caption=img["relative_path"], use_container_width=True)
                            st.caption(f"{img['size_kb']} KB · {img['modified']}")

        with tab_pdfs:
            if not pdf_files:
                st.info("沒有 PDF 檔案。")
            else:
                subdirs_p = sorted(set(f["parent"] for f in pdf_files))
                selected_pdir = st.selectbox("按子資料夾篩選", ["全部"] + subdirs_p, key="pdf_dir_filter")
                filtered_pdfs = pdf_files if selected_pdir == "全部" else [f for f in pdf_files if f["parent"] == selected_pdir]

                st.subheader(f"PDF 文件列表（{len(filtered_pdfs)} 份）")
                for f in filtered_pdfs:
                    with st.expander(f"📄 {f['relative_path']}（{f['size_kb']} KB · {f['modified']}）"):
                        with open(f["path"], "rb") as fh:
                            st.download_button(
                                f"下載 {f['name']}",
                                fh.read(),
                                file_name=f["name"],
                                mime="application/pdf",
                                key=f"dl_pdf2_{f['path']}",
                            )

        with tab_all:
            all_df = pd.DataFrame(asset_files)
            st.dataframe(all_df[["relative_path", "type", "ext", "size_kb", "modified", "parent"]].rename(columns={
                "relative_path": "檔案路徑", "type": "類型", "ext": "格式",
                "size_kb": "大小 (KB)", "modified": "修改時間", "parent": "所在資料夾",
            }), use_container_width=True, hide_index=True)


# ── 頁尾 ────────────────────────────────────────────────────────
st.sidebar.divider()
st.sidebar.caption(f"最後更新：{datetime.now(HKT).strftime('%Y-%m-%d %H:%M:%S')}（HKT）")
st.sidebar.caption("新輝雲端自動化後台 v3.0")
