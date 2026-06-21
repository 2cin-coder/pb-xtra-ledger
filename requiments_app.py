import streamlit as st
import pandas as pd
import sqlite3
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
import os

# =========================================================================
# 📱 MOBILE TROUBLESHOOTING GUIDE & ERROR QUICK FIXES
# =========================================================================
# 1. ERROR: "Missing Secrets configuration!"
#    -> FIX: Go to Streamlit Dashboard -> App Settings -> Secrets. Ensure you
#            pasted your [email_credentials] headers and 16-character code exactly.
# 2. ERROR: "Email delivery connection error: (535, ... Authentication failed)"
#    -> FIX: Your Google App password is typed wrong, or you used your master account
#            password instead of generating an exclusive 16-character code.
# 3. ERROR: "ModuleNotFoundError: No module named 'pandas' / 'openpyxl'"
#    -> FIX: Your requirements.txt file on GitHub is misspelled or missing lines.
# =========================================================================

# --- 1. LOCAL SQLITE DATABASE INITIALIZATION ---
DB_FILE = "powerball_data.db"

def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS tickets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            main_numbers TEXT,
            powerball INTEGER,
            cost REAL,
            winnings REAL,
            net_balance REAL
        )
    """)
    conn.commit()
    conn.close()

def load_tickets_from_db():
    conn = sqlite3.connect(DB_FILE)
    df = pd.read_sql_query("SELECT id, main_numbers, powerball, cost, winnings, net_balance FROM tickets ORDER BY id DESC", conn)
    conn.close()
    df.columns = ["ID", "Main Numbers", "PowerBall", "Ticket Cost (ZAR)", "Winnings (ZAR)", "Net Profit/Loss"]
    return df

def save_single_ticket_to_db(main_numbers_str, pb, cost, winnings):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    net = winnings - cost
    cursor.execute("""
        INSERT INTO tickets (main_numbers, powerball, cost, winnings, net_balance)
        VALUES (?, ?, ?, ?, ?)
    """, (main_numbers_str, pb, cost, winnings, net))
    conn.commit()
    conn.close()

def save_bulk_df_to_db(df_to_append):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    for _, row in df_to_append.iterrows():
        cursor.execute("""
            INSERT INTO tickets (main_numbers, powerball, cost, winnings, net_balance)
            VALUES (?, ?, ?, ?, ?)
        """, (row['Main Numbers'], int(row['PowerBall']), float(row['Ticket Cost (ZAR)']), float(row['Winnings (ZAR)']), float(row['Net Profit/Loss'])))
    conn.commit()
    conn.close()

def clear_entire_database():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("DROP TABLE IF EXISTS tickets")
    conn.commit()
    conn.close()
    init_db()

# --- 2. MATCHING & AUTOMATIC WINNER ENGINE ---
def calculate_matches(ticket_numbers_str, ticket_pb, official_main_list, official_pb):
    """Compares ticket rows against official draw inputs to look for matches."""
    try:
        ticket_nums = set([int(x.strip()) for x in ticket_numbers_str.split(",")])
    except:
        return "Invalid Number Format", 0, False
        
    official_nums = set(official_main_list)
    matched_main = len(ticket_nums.intersection(official_nums))
    matched_pb = (int(ticket_pb) == int(official_pb))
    
    # Tier breakdown matrix
    if matched_main == 5 and matched_pb: return "🔥 JACKPOT! (5 Main + PB)", matched_main, matched_pb
    elif matched_main == 5: return "Match 5 Main", matched_main, matched_pb
    elif matched_main == 4 and matched_pb: return "Match 4 Main + PB", matched_main, matched_pb
    elif matched_main == 4: return "Match 4 Main", matched_main, matched_pb
    elif matched_main == 3 and matched_pb: return "Match 3 Main + PB", matched_main, matched_pb
    elif matched_main == 3: return "Match 3 Main", matched_main, matched_pb
    elif matched_main == 2 and matched_pb: return "Match 2 Main + PB", matched_main, matched_pb
    elif matched_main == 1 and matched_pb: return "Match 1 Main + PB", matched_main, matched_pb
    elif matched_main == 0 and matched_pb: return "Match PB Only", matched_main, matched_pb
    else: return "No Prize ❌", matched_main, matched_pb

# --- 3. EMAIL SYSTEM VIA SECRETS VAULT ---
def send_budget_email(to_email, df_summary, filename="powerball_budget.xlsx"):
    try:
        FROM_EMAIL = st.secrets["email_credentials"]["sender_email"]
        EMAIL_PASSWORD = st.secrets["email_credentials"]["app_password"]
    except Exception:
        st.error("🔒 Missing Secrets configuration! Add sender_email and app_password parameters inside your Streamlit Settings Vault.")
        return False
        
    SMTP_SERVER = "://gmail.com"
    SMTP_PORT = 587

    total_spent = df_summary["Ticket Cost (ZAR)"].sum()
    total_won = df_summary["Winnings (ZAR)"].sum()
    net_balance = df_summary["Net Profit/Loss"].sum()

    msg = MIMEMultipart()
    msg['From'] = FROM_EMAIL
    msg['To'] = to_email
    msg['Subject'] = "Weekly PowerBall Xtra Local Database Report"

    body = f"Attached is your complete lottery dashboard spreadsheet tracking report.\n\nSummary Metrics:\nTotal Invested: R{total_spent:.2f}\nTotal Returns: R{total_won:.2f}\nNet Status: R{net_balance:.2f}"
    msg.attach(MIMEText(body, 'plain'))
    df_summary.to_excel(filename, index=False)

    with open(filename, "rb") as attachment:
        part = MIMEBase("application", "octet-stream")
        part.set_payload(attachment.read())
        encoders.encode_base64(part)
        part.add_header("Content-Disposition", f"attachment; filename={filename}")
        msg.attach(part)

    try:
        server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT)
        server.starttls()
        server.login(FROM_EMAIL, EMAIL_PASSWORD)
        server.send_message(msg)
        server.quit()
        return True
    except Exception as e:
        st.error(f"Email delivery connection error: {e}")
        return False

def estimate_jackpot_odds(number_of_boards):
    TOTAL_COMBINATIONS = 42_375_200
    return (number_of_boards / TOTAL_COMBINATIONS) * 100

init_db()

# --- 4. STREAMLIT INTERFACE ---
st.set_page_config(page_title="PowerBall Mobile Ledger", page_icon="🗄️", layout="wide")
st.title("🗄️ Permanent PowerBall Xtra Ledger")

df_current = load_tickets_from_db()
total_records = len(df_current)

col_m1, col_m2, col_m3 = st.columns(3)
with col_m1: st.metric(label="Total Boards Logged", value=f"{total_records}")
with col_m2:
    current_net = df_current["Net Profit/Loss"].sum() if total_records > 0 else 0.0
    st.metric(label="Net Balance (ZAR)", value=f"R{current_net:,.2f}", delta=f"{current_net:.2f}")
with col_m3: st.metric(label="Combined Jackpot Win %", value=f"{estimate_jackpot_odds(total_records):.7f}%")

st.markdown("---")

tab1, tab2, tab3, tab4 = st.tabs(["📝 Manual Entry", "📤 CSV Bulk Upload", "🔮 Ticket Checker Engine", "✉️ Email & Settings"])

with tab1:
    col1, col2 = st.columns(2)
    with col1:
        st.write("#### 🎟️ Log Individual Line")
        # Quick price picker buttons for South Africa PowerBall rules
        ticket_type = st.radio("Select Play Type:", ["PowerBall Only (R10.00)", "PowerBall + Xtra (R15.00)"])
        selected_cost = 15.00 if "Xtra" in ticket_type else 10.00
        
        m1 = st.number_input("Ball 1", 1, 50, 4, key="b1")
        m2 = st.number_input("Ball 2", 1, 50, 15, key="b2")
        m3 = st.number_input("Ball 3", 1, 50, 22, key="b3")
        m4 = st.number_input("Ball 4", 1, 50, 39, key="b4")
        m5 = st.number_input("Ball 5", 1, 50, 48, key="b5")
        pb = st.number_input("PowerBall", 1, 20, 12, key="pbb")
        
        winnings = st.number_input("Prize Money Won (Rands)", min_value=0.0, value=0.0, step=5.00)
        
        if st.button("Commit to Cloud Storage"):
            numbers_list = sorted(list(set([m1, m2, m3, m4, m5])))
            if len(numbers_list) < 5: st.error("Validation error: Ensure all 5 primary numbers are distinct.")
            else:
                num_str = ", ".join(map(str, numbers_list))
                save_single_ticket_to_db(num_str, pb, selected_cost, winnings)
                st.success("Record permanently saved!")
                st.rerun()
    with col2:
        st.write("#### 🔎 Current Active Table Records")
        if total_records > 0: st.dataframe(df_current, use_container_width=True, hide_index=True)
        else: st.info("The database file is currently completely blank.")

with tab2:
    st.write("#### 📄 Upload a CSV File to Append Bulk Data")
    uploaded_file = st.file_uploader("Drop your file here", type=["csv"])
    if uploaded_file is not None:
        try:
            csv_df = pd.read_csv(uploaded_file)
            required_cols = ["Main Numbers", "PowerBall", "Ticket Cost (ZAR)", "Winnings (ZAR)"]
            if not all(x in csv_df.columns for x in required_cols): st.error("Spreadsheet fails structural constraints. Verify layout headers match.")
            else:
                csv_df["Net Profit/Loss"] = csv_df["Winnings (ZAR)"] - csv_df["Ticket Cost (ZAR)"]
                if st.button("Merge Uploaded File"):
                    save_bulk_df_to_db(csv_df)
                    st.success(f"Added {len(csv_df)} rows to database storage.")
                    st.rerun()
        except Exception as err: st.error(f"Processing error: {err}")

with tab3:
    st.write("#### 🔮 Mass Draw Result Checker Engine")
    st.write("Paste the official winning numbers from the [Sizekhaya National Lottery](https://sizekhaya.co.za/) below to check your tickets instantly:")
    
    c_res1, c_res2, c_res3, c_res4, c_res5, c_pb = st.columns(6)
    with c_res1: o1 = st.number_input("Winning 1", 1, 50, 1)
    with c_res2: o2 = st.number_input("Winning 2", 1, 50, 2)
