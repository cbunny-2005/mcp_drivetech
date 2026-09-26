"""
Demo frontend — TEMPORARY, talks to the FastAPI backend in demo/backend.
Delete this whole demo/ folder after the demo.

Run (from demo/frontend/):
    streamlit run app.py
"""

import os

import pandas as pd
import requests
import streamlit as st

API_URL = os.getenv("DEMO_API_URL", "http://localhost:8010")

st.set_page_config(page_title="RFQ Pipeline Demo", layout="wide")
st.title("RFQ Pipeline Demo")
st.caption(
    "Temporary demo UI — runs the real classify → extract → match → "
    "quote pipeline against unread RFQ emails. No database; results reset "
    "when the backend restarts."
)

if st.button("Run Dry Run", type="primary"):
    with st.spinner("Scanning unread mail and running the pipeline..."):
        try:
            resp = requests.post(f"{API_URL}/api/runs", timeout=120)
        except requests.exceptions.ConnectionError:
            st.error(f"Can't reach the backend at {API_URL} — is it running?")
            st.stop()
    if resp.status_code != 200:
        st.error(f"Backend error: {resp.text}")
    else:
        st.success(f"Processed {len(resp.json())} email(s).")

try:
    runs = requests.get(f"{API_URL}/api/runs", timeout=30).json()
except requests.exceptions.ConnectionError:
    st.error(f"Can't reach the backend at {API_URL} — is it running?")
    st.stop()

if not runs:
    st.info('No runs yet — click "Run Dry Run" to scan unread mail.')
    st.stop()

options = {f"{r['subject']} ({r['id']})": r["id"] for r in runs}
selected_label = st.selectbox("Select a run", list(options.keys()))
run_id = options[selected_label]

run = requests.get(f"{API_URL}/api/runs/{run_id}", timeout=30).json()

st.subheader("Classification")
cls = run["classification"]
st.write(f"**is_rfq:** {cls['is_rfq']}  |  **confidence:** {cls['confidence']}")
st.caption(cls["reason"])

if not cls["is_rfq"]:
    st.warning("Not classified as an RFQ — nothing further to show.")
    st.stop()

ext = run["extraction"]
st.subheader("Extraction")
st.write(f"**Company:** {ext['company']}  |  **Customer:** {ext['customer_name']}")
st.dataframe(pd.DataFrame(ext["products"]), use_container_width=True)

st.subheader("Match results")
lines = run["match_lines"] or []
match_df = pd.DataFrame(lines)
if not match_df.empty:
    # "result" here, not "status" — the line dicts from rfq_matcher_service
    # already have their own "status" field (exact/agent/review/unmatched).
    match_df.insert(0, "result", match_df["matched"].map({True: "OK", False: "MISS"}))
    st.dataframe(
        match_df[["result", "requested", "description", "rate", "match_method"]],
        use_container_width=True,
    )

quotation = run.get("quotation")
if not quotation:
    st.warning("Nothing matched — no quotation was generated for this run.")
    st.stop()

st.subheader("Quotation")
items_df = pd.DataFrame(quotation["items"])
st.dataframe(items_df, use_container_width=True)

summary = quotation["summary"]
st.metric("Subtotal", f"{quotation['currency']} {summary['subTotal']:,.2f}")

st.subheader("Edit a line item")
line_no = st.selectbox("Line number", items_df["lineNumber"].tolist())
item = next(i for i in quotation["items"] if i["lineNumber"] == line_no)
with st.form("edit_item"):
    item_name = st.text_input("Item name", item["itemName"])
    part_number = st.text_input("Part number", item["partNumber"])
    quantity = st.number_input("Quantity", value=float(item["quantity"]))
    unit_price = st.number_input("Unit price", value=float(item["unitPrice"]))
    discount = st.number_input("Discount %", value=float(item["discountPercentage"]))
    delivery = st.text_input("Delivery", item["delivery"])
    submitted = st.form_submit_button("Save")
    if submitted:
        patch_body = {
            "itemName": item_name,
            "partNumber": part_number,
            "quantity": quantity,
            "unitPrice": unit_price,
            "discountPercentage": discount,
            "delivery": delivery,
        }
        r = requests.patch(
            f"{API_URL}/api/runs/{run_id}/items/{line_no}", json=patch_body, timeout=30)
        if r.status_code == 200:
            st.success("Saved.")
            st.rerun()
        else:
            st.error(f"Failed: {r.text}")
