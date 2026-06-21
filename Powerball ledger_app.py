import streamlit as st
import pandas as pd
import sqlite3
import random
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
import os

# =========================================================
# CONFIG
# =========================================================
DB_FILE = "powerball_data.db"
TOTAL_COMBINATIONS = 42_375_200  # C(50,5) x C(20,1) - verified jackpot odds for SA PowerBall

# Ticket prices - update these if Ithuba changes pricing
PRICE_STANDARD = 10.00
PRICE_XTRA = 15.00

REQUIRED_BULK_COLUMNS = ["Main Numbers", "PowerBall", "Ticket Cost (ZAR)", "Winnings (ZAR)"]

# =========================================================
# 1. DATABASE LAYER
# =========================================================
def get_connection():
    return sqlite3.connect(DB_FILE)


def init_db():
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS tickets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            main_numbers TEXT NOT NULL,
            powerball INTEGER NOT NULL,
            cost REAL NOT NULL,
            winnings REAL NOT NULL DEFAULT 0,
            net_balance REAL NOT NULL
        )
    """)
    conn.commit()
    conn.close()


def load_tickets_from_db():
    conn = get_connection()
    df = pd.read_sql_query(
        "SELECT id, main_numbers, powerball, cost, winnings, net_balance "
        "FROM tickets ORDER BY id DESC",
        conn,
    )
    conn.close()
    df.columns = ["ID", "Main Numbers", "PowerBall", "Ticket Cost (ZAR)", "Winnings (ZAR)", "Net Profit/Loss"]
    return df


def save_single_ticket_to_db(main_numbers_str, pb, cost, winnings):
    conn = get_connection()
    cursor = conn.cursor()
    net = float(winnings) - float(cost)
    cursor.execute(
        """
        INSERT INTO tickets (main_numbers, powerball, cost, winnings, net_balance)
        VALUES (?, ?, ?, ?, ?)
        """,
        (str(main_numbers_str), int(pb), float(cost), float(winnings), float(net)),
    )
    conn.commit()
    conn.close()


def save_bulk_df_to_db(df_to_append):
    conn = get_connection()
    cursor = conn.cursor()
    inserted = 0
    for _, row in df_to_append.iterrows():
        cost_val = float(row["Ticket Cost (ZAR)"])
        win_val = float(row["Winnings (ZAR)"])
        net_val = win_val - cost_val
        cursor.execute(
            """
            INSERT INTO tickets (main_numbers, powerball, cost, winnings, net_balance)
            VALUES (?, ?, ?, ?, ?)
            """,
            (str(row["Main Numbers"]), int(row["PowerBall"]), cost_val, win_val, net_val),
        )
        inserted += 1
    conn.commit()
    conn.close()
    return inserted


def clear_entire_database():
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM tickets")
    cursor.execute("DELETE FROM sqlite_sequence WHERE name='tickets'")
    conn.commit()
    conn.close()


def delete_ticket(ticket_id):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM tickets WHERE id = ?", (int(ticket_id),))
    conn.commit()
    conn.close()


# =========================================================
# 2. VALIDATION
# =========================================================
def parse_main_numbers(raw):
    try:
        parts = [p.strip() for p in str(raw).split(",") if p.strip() != ""]
        nums = [int(p) for p in parts]
    except ValueError:
        raise ValueError(f"Could not parse numbers from '{raw}'. Use comma-separated integers.")

    if len(nums) != 5:
        raise ValueError(f"Expected 5 main numbers, got {len(nums)} in '{raw}'.")
    if len(set(nums)) != 5:
        raise ValueError(f"Main numbers must be unique: '{raw}'.")
    if any(n < 1 or n > 50 for n in nums):
        raise ValueError(f"Main numbers must be between 1 and 50: '{raw}'.")
    return sorted(nums)


def validate_powerball(pb):
    try:
        pb_int = int(pb)
    except (ValueError, TypeError):
        raise ValueError(f"PowerBall '{pb}' is not a valid integer.")
    if pb_int < 1 or pb_int > 20:
        raise ValueError(f"PowerBall must be between 1 and 20, got {pb_int}.")
    return pb_int


# =========================================================
# 3. MATCHING ENGINE
# =========================================================
def calculate_matches(ticket_numbers_str, ticket_pb, official_main_list, official_pb):
    try:
        ticket_nums = set(parse_main_numbers(ticket_numbers_str))
        ticket_pb_int = validate_powerball(ticket_pb)
    except ValueError:
        return "Invalid Number Format", 0, False

    official_nums = set(official_main_list)
    matched_main = len(ticket_nums.intersection(official_nums))
    matched_pb = (ticket_pb_int == int(official_pb))

    if matched_main == 5 and matched_pb:
        return "JACKPOT! (5 Main + PB)", matched_main, matched_pb
    elif matched_main == 5:
        return "Match 5 Main", matched_main, matched_pb
    elif matched_main == 4 and matched_pb:
        return "Match 4 Main + PB", matched_main, matched_pb
    elif matched_main == 4:
        return "Match 4 Main", matched_main, matched_pb
    elif matched_main == 3 and matched_pb:
        return "Match 3 Main + PB", matched_main, matched_pb
    elif matched_main == 3:
        return "Match 3 Main", matched_main, matched_pb
    elif matched_main == 2 and matched_pb:
        return "Match 2 Main + PB", matched_main, matched_pb
    elif matched_main == 1 and matched_pb:
        return "Match 1 Main + PB", matched_main, matched_pb
    elif matched_main == 0 and matched_pb:
        return "Match PB Only", matched_main, matched_pb
    else:
        return "No Prize", matched_main, matched_pb


def estimate_jackpot_odds(number_of_boards):
    if number_of_boards <= 0:
        return 0.0
    return (number_of_boards / TOTAL_COMBINATIONS) * 100


# =========================================================
# 4. EMAIL
# =========================================================
def send_budget_email(to_email, df_summary, filename="powerball_budget.xlsx"):
    try:
        from_email = st.secrets["email_credentials"]["sender_email"]
        email_password = st.secrets["email_credentials"]["app_password"]
        smtp_server = st.secrets["email_credentials"].get("smtp_server", "smtp.gmail.com")
        smtp_port = int(st.secrets["email_credentials"].get("smtp_port", 587))
    except Exception:
        st.error(
            "Missing secrets configuration. Add `[email_credentials]` with "
            "`sender_email` and `app_password` (and optionally `smtp_server`, "
            "`smtp_port`) to your Streamlit secrets."
        )
        return False

    total_spent = df_summary["Ticket Cost (ZAR)"].sum()
    total_won = df_summary["Winnings (ZAR)"].sum()
    net_balance = df_summary["Net Profit/Loss"].sum()

    msg = MIMEMultipart()
    msg["From"] = from_email
    msg["To"] = to_email
    msg["Subject"] = "Weekly PowerBall Xtra Ledger Report"

    body = (
        "Attached is your complete lottery dashboard spreadsheet tracking report.\n\n"
        f"Summary Metrics:\nTotal Invested: R{total_spent:.2f}\n"
        f"Total Returns: R{total_won:.2f}\nNet Status: R{net_balance:.2f}"
    )
    msg.attach(MIMEText(body, "plain"))

    filepath = os.path.join(os.getcwd(), filename)
    df_summary.to_excel(filepath, index=False, engine="openpyxl")

    with open(filepath, "rb") as attachment:
        part = MIMEBase("application", "octet-stream")
        part.set_payload(attachment.read())
        encoders.encode_base64(part)
        part.add_header("Content-Disposition", f"attachment; filename={filename}")
        msg.attach(part)

    try:
        server = smtplib.SMTP(smtp_server, smtp_port, timeout=15)
        server.starttls()
        server.login(from_email, email_password)
        server.send_message(msg)
        server.quit()
        return True
    except smtplib.SMTPAuthenticationError:
        st.error("Authentication failed. For Gmail, use an App Password, not your normal password.")
        return False
    except Exception as e:
        st.error(f"Email delivery error: {e}")
        return False
    finally:
        if os.path.exists(filepath):
            os.remove(filepath)


# =========================================================
# 5. APP BOOTSTRAP
# =========================================================
init_db()

st.set_page_config(page_title="PowerBall Mobile Ledger", page_icon="🗄️", layout="wide")
st.title("🗄️ Permanent PowerBall Xtra Ledger")

df_current = load_tickets_from_db()
total_records = len(df_current)

col_m1, col_m2, col_m3 = st.columns(3)
with col_m1:
    st.metric(label="Total Boards Logged", value=f"{total_records}")
with col_m2:
    current_net = df_current["Net Profit/Loss"].sum() if total_records > 0 else 0.0
    st.metric(label="Net Balance (ZAR)", value=f"R{current_net:,.2f}", delta=f"{current_net:.2f}")
with col_m3:
    st.metric(label="Combined Jackpot Win %", value=f"{estimate_jackpot_odds(total_records):.7f}%")

st.markdown("---")

tab1, tab2, tab3, tab4 = st.tabs(
    ["📝 Entry & Quick Pick", "📤 Smart Bulk Upload", "🔮 Ticket Checker Engine", "✉️ Email & Settings"]
)

# ---------------------------------------------------------
# TAB 1: ENTRY & QUICK PICK
# ---------------------------------------------------------
with tab1:
    col1, col2 = st.columns(2)
    with col1:
        st.write("#### 🎟️ Log or Generate Individual Line")
        ticket_type = st.radio(
            "Select Play Type:",
            [f"PowerBall Only (R{PRICE_STANDARD:.2f})", f"PowerBall + Xtra (R{PRICE_XTRA:.2f})"],
        )
        selected_cost = PRICE_XTRA if "Xtra" in ticket_type else PRICE_STANDARD

        st.write("##### Need numbers? Generate a random board:")
        if st.button("🎲 Generate Random Quick Pick"):
            random_main = sorted(random.sample(range(1, 51), 5))
            random_pb = random.randint(1, 20)
            st.session_state["qp_m1"] = random_main[0]
            st.session_state["qp_m2"] = random_main[1]
            st.session_state["qp_m3"] = random_main[2]
            st.session_state["qp_m4"] = random_main[3]
            st.session_state["qp_m5"] = random_main[4]
            st.session_state["qp_pb"] = random_pb
            st.rerun()

        val_m1 = int(st.session_state.get("qp_m1", 4))
        val_m2 = int(st.session_state.get("qp_m2", 15))
        val_m3 = int(st.session_state.get("qp_m3", 22))
        val_m4 = int(st.session_state.get("qp_m4", 39))
        val_m5 = int(st.session_state.get("qp_m5", 48))
        val_pb = int(st.session_state.get("qp_pb", 12))

        m1 = st.number_input("Ball 1", 1, 50, val_m1, key="num_b1")
        m2 = st.number_input("Ball 2", 1, 50, val_m2, key="num_b2")
        m3 = st.number_input("Ball 3", 1, 50, val_m3, key="num_b3")
        m4 = st.number_input("Ball 4", 1, 50, val_m4, key="num_b4")
        m5 = st.number_input("Ball 5", 1, 50, val_m5, key="num_b5")
        pb = st.number_input("PowerBall", 1, 20, val_pb, key="num_pbb")

        winnings = st.number_input("Prize Money Won (Rands)", min_value=0.0, value=0.0, step=5.00, key="num_win")

        if st.button("💾 Save Ticket to Ledger"):
            numbers_list = sorted(set([m1, m2, m3, m4, m5]))
            if len(numbers_list) < 5:
                st.error("Validation error: Ensure all 5 primary numbers are distinct.")
            else:
                num_str = ", ".join(map(str, numbers_list))
                save_single_ticket_to_db(num_str, pb, selected_cost, winnings)
                st.success("Record saved!")
                st.rerun()

    with col2:
        st.write("#### 🔎 Current Active Table Records")
        if total_records > 0:
            st.dataframe(df_current, use_container_width=True, hide_index=True)

            with st.expander("🗑️ Delete a ticket"):
                ticket_to_delete = st.selectbox(
                    "Select ticket ID to delete", df_current["ID"].tolist(), key="del_id"
                )
                if st.button("Delete selected ticket"):
                    delete_ticket(ticket_to_delete)
                    st.success(f"Ticket #{ticket_to_delete} deleted.")
                    st.rerun()
        else:
            st.info("The database is currently empty. Log a ticket to get started.")

# ---------------------------------------------------------
# TAB 2: SMART BULK UPLOAD
# ---------------------------------------------------------
with tab2:
    st.write("#### 📄 Upload an Excel or CSV File to Bulk Save Tickets")
    st.write("Your spreadsheet headers must match these names exactly:")
    st.code(" | ".join(REQUIRED_BULK_COLUMNS))
    st.caption("`Main Numbers` should be 5 comma-separated integers (1-50), e.g. `4, 15, 22, 39, 48`.")

    uploaded_file = st.file_uploader("Drop your spreadsheet here:", type=["csv", "xlsx"])
    if uploaded_file is not None:
        try:
            if uploaded_file.name.endswith(".xlsx"):
                uploaded_df = pd.read_excel(uploaded_file, engine="openpyxl")
            else:
                uploaded_df = pd.read_csv(uploaded_file)

            uploaded_df.columns = uploaded_df.columns.str.strip()

            missing_cols = [c for c in REQUIRED_BULK_COLUMNS if c not in uploaded_df.columns]
            if missing_cols:
                st.error(f"Missing required column(s): {', '.join(missing_cols)}")
            else:
                st.write("##### Preview")
                st.dataframe(uploaded_df, use_container_width=True, hide_index=True)

                errors = []
                valid_rows = []
                for idx, row in uploaded_df.iterrows():
                    try:
                        nums = parse_main_numbers(row["Main Numbers"])
                        pb_val = validate_powerball(row["PowerBall"])
                        cost_val = float(row["Ticket Cost (ZAR)"])
                        win_val = float(row["Winnings (ZAR)"])
                        if cost_val < 0 or win_val < 0:
                            raise ValueError("Cost and winnings cannot be negative.")
                        valid_rows.append(
                            {
                                "Main Numbers": ", ".join(map(str, nums)),
                                "PowerBall": pb_val,
                                "Ticket Cost (ZAR)": cost_val,
                                "Winnings (ZAR)": win_val,
                            }
                        )
                    except (ValueError, TypeError, KeyError) as e:
                        errors.append(f"Row {idx + 1}: {e}")

                if errors:
                    st.warning(f"{len(errors)} row(s) failed validation and will be skipped:")
                    for err in errors[:20]:
                        st.text(f"  • {err}")
                    if len(errors) > 20:
                        st.text(f"  ...and {len(errors) - 20} more.")

                st.info(f"{len(valid_rows)} of {len(uploaded_df)} row(s) are ready to import.")

                if valid_rows and st.button("📥 Import Valid Rows to Ledger"):
                    inserted = save_bulk_df_to_db(pd.DataFrame(valid_rows))
                    st.success(f"Imported {inserted} ticket(s) successfully!")
                    st.rerun()

        except Exception as e:
            st.error(f"Could not read file: {e}")

# ---------------------------------------------------------
# TAB 3: TICKET CHECKER ENGINE
# ---------------------------------------------------------
with tab3:
    st.write("#### 🔮 Check Your Logged Tickets Against the Official Draw")

    if total_records == 0:
        st.info("No tickets logged yet. Add some in the Entry tab first.")
    else:
        st.write("##### Enter the official winning numbers:")
        oc1, oc2, oc3, oc4, oc5, ocpb = st.columns(6)
        with oc1:
            o1 = st.number_input("Off. 1", 1, 50, 1, key="off_1")
        with oc2:
            o2 = st.number_input("Off. 2", 1, 50, 2, key="off_2")
        with oc3:
            o3 = st.number_input("Off. 3", 1, 50, 3, key="off_3")
        with oc4:
            o4 = st.number_input("Off. 4", 1, 50, 4, key="off_4")
        with oc5:
            o5 = st.number_input("Off. 5", 1, 50, 5, key="off_5")
        with ocpb:
            opb = st.number_input("Off. PB", 1, 20, 1, key="off_pb")

        official_main = [o1, o2, o3, o4, o5]
        if len(set(official_main)) != 5:
            st.error("Official main numbers must be 5 distinct values.")
        else:
            if st.button("🔍 Check All Tickets"):
                results = []
                for _, row in df_current.iterrows():
                    outcome, matched_main, matched_pb = calculate_matches(
                        row["Main Numbers"], row["PowerBall"], official_main, opb
                    )
                    results.append(
                        {
                            "ID": row["ID"],
                            "Main Numbers": row["Main Numbers"],
                            "PowerBall": row["PowerBall"],
                            "Main Matches": matched_main,
                            "PB Match": "✅" if matched_pb else "—",
                            "Result": outcome,
                        }
                    )
                results_df = pd.DataFrame(results)
                results_df = results_df.sort_values("Main Matches", ascending=False)
                st.dataframe(results_df, use_container_width=True, hide_index=True)

                winners = results_df[results_df["Result"] != "No Prize"]
                if not winners.empty:
                    st.success(f"🎉 {len(winners)} ticket(s) matched at least one number!")
                else:
                    st.info("No matches this time. Better luck next draw!")

# ---------------------------------------------------------
# TAB 4: EMAIL & SETTINGS
# ---------------------------------------------------------
with tab4:
    st.write("#### ✉️ Email Yourself a Budget Report")
    st.caption(
        "Requires Streamlit secrets configured as:\n\n"
        "```\n[email_credentials]\nsender_email = \"you@gmail.com\"\n"
        "app_password = \"xxxx xxxx xxxx xxxx\"\n```"
    )

    to_email = st.text_input("Send report to:", placeholder="someone@example.com")
    if st.button("📧 Send Report"):
        if total_records == 0:
            st.warning("Nothing to report yet — log at least one ticket first.")
        elif not to_email or "@" not in to_email:
            st.error("Please enter a valid email address.")
        else:
            with st.spinner("Sending..."):
                success = send_budget_email(to_email, df_current)
            if success:
                st.success(f"Report sent to {to_email}!")

    st.markdown("---")
    st.write("#### ⚠️ Danger Zone")
    st.caption("This permanently deletes every ticket in the local database. This cannot be undone.")
    confirm_wipe = st.checkbox("I understand this will permanently delete all records.")
    if st.button("🗑️ Clear Entire Database", disabled=not confirm_wipe):
        clear_entire_database()
        st.success("Database cleared.")
        st.rerun()
