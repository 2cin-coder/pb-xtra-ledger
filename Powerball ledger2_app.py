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
# CONFIG: GAME DEFINITIONS
# =========================================================
# Rules current as of June 2026 under Sizekhaya (the new National Lottery
# operator as of 1 June 2026). If you're reading this much later, sanity
# check against your physical ticket or the official app - lottery rules
# can change again.
DB_FILE = "powerball_data.db"

# Each game is fully described here: how many main numbers, the pool size,
# whether it has a separate bonus/PowerBall number, its cost, and the
# minimum match count that wins any prize at all. Adding a new game later
# just means adding one more entry to this dict.
GAMES = {
    "PowerBall": {
        "main_pool": 50,
        "main_drawn": 5,
        "bonus_pool": 16,       # PowerBall number range, 1-16
        "bonus_label": "PowerBall",
        "cost": 10.00,
        "min_match_for_prize": 0,  # "Match PB only" tier exists
    },
    "PowerBall XTRA": {
        "main_pool": 50,
        "main_drawn": 5,
        "bonus_pool": 16,
        "bonus_label": "PowerBall",
        "cost": 5.00,           # add-on cost on top of a PowerBall board
        "min_match_for_prize": 0,
        "is_addon_of": "PowerBall",
    },
    "Lotto": {
        "main_pool": 52,
        "main_drawn": 6,
        "bonus_pool": None,     # no separate bonus number
        "bonus_label": None,
        "cost": 5.00,
        "min_match_for_prize": 3,
    },
    "Lotto Plus 1": {
        "main_pool": 52,
        "main_drawn": 6,
        "bonus_pool": None,
        "bonus_label": None,
        "cost": 2.50,
        "min_match_for_prize": 3,
        "is_addon_of": "Lotto",
    },
    "Lotto 5 Max": {
        "main_pool": 52,
        "main_drawn": 6,
        "bonus_pool": None,
        "bonus_label": None,
        "cost": 2.50,
        "min_match_for_prize": 3,
        "is_addon_of": "Lotto",
    },
}

GAME_NAMES = list(GAMES.keys())

REQUIRED_BULK_COLUMNS = ["Game", "Main Numbers", "Bonus Number", "Ticket Cost (ZAR)", "Winnings (ZAR)"]


# =========================================================
# ODDS ENGINE (generalized across all games)
# =========================================================
def _combinations(n, k):
    if k < 0 or k > n:
        return 0
    result = 1
    for i in range(k):
        result = result * (n - i) // (i + 1)
    return result


def total_combinations(game_name):
    g = GAMES[game_name]
    base = _combinations(g["main_pool"], g["main_drawn"])
    if g["bonus_pool"]:
        base *= g["bonus_pool"]
    return base


def build_odds_table(game_name):
    """Returns (rows, overall_odds) for any configured game. Tier list is
    generated from the game's shape rather than hardcoded, so PowerBall
    (main + bonus) and Lotto (main only) both work from the same function."""
    g = GAMES[game_name]
    drawn = g["main_drawn"]
    pool = g["main_pool"]
    has_bonus = g["bonus_pool"] is not None
    total = total_combinations(game_name)

    rows = []
    total_winning = 0

    def main_ways(matched):
        return _combinations(drawn, matched) * _combinations(pool - drawn, drawn - matched)

    if has_bonus:
        bonus_pool = g["bonus_pool"]
        # Real-world PowerBall-style prize structure: matching 2 or fewer
        # main numbers only wins a prize if the bonus number also matches.
        # Matching 3+ main numbers wins with or without the bonus.
        tiers = []
        for m in range(drawn, 2, -1):  # drawn, drawn-1, ..., 3
            tiers.append((m, True))
            tiers.append((m, False))
        tiers.append((2, True))
        tiers.append((1, True))
        tiers.append((0, True))  # bonus-only tier
        for matched, has_b in tiers:
            combos = main_ways(matched) * (1 if has_b else (bonus_pool - 1))
            if combos == 0:
                continue
            label = f"Match {matched} + {g['bonus_label']}" if has_b else f"Match {matched}"
            if matched == drawn and has_b:
                label += " (Jackpot)"
            if matched == 0 and has_b:
                label = f"Match {g['bonus_label']} only"
            total_winning += combos
            rows.append({"Prize Tier": label, "Winning Combinations": combos, "Odds (1 in X)": round(total / combos, 1)})
    else:
        min_match = g["min_match_for_prize"]
        for m in range(drawn, min_match - 1, -1):
            combos = main_ways(m)
            if combos == 0:
                continue
            label = f"Match {m}" + (" (Jackpot)" if m == drawn else "")
            total_winning += combos
            rows.append({"Prize Tier": label, "Winning Combinations": combos, "Odds (1 in X)": round(total / combos, 1)})

    overall_odds = total / total_winning if total_winning else None
    return rows, overall_odds


# =========================================================
# 1. DATABASE LAYER
# =========================================================
def get_connection():
    return sqlite3.connect(DB_FILE)


def init_db():
    conn = get_connection()
    cursor = conn.cursor()

    # Detect a pre-multi-game (legacy single-PowerBall) database: it has a
    # 'powerball' column with a NOT NULL constraint that SQLite can't drop
    # via ALTER TABLE. Rebuild the table cleanly via copy-and-swap so new
    # inserts (which don't populate 'powerball') don't fail.
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='tickets'")
    table_exists = cursor.fetchone() is not None

    if table_exists:
        cursor.execute("PRAGMA table_info(tickets)")
        cols_info = cursor.fetchall()
        col_names = [c[1] for c in cols_info]
        legacy_powerball_not_null = any(
            c[1] == "powerball" and c[3] == 1 for c in cols_info  # c[3] is notnull flag
        )
        if legacy_powerball_not_null:
            cursor.execute("""
                CREATE TABLE tickets_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    game TEXT NOT NULL DEFAULT 'PowerBall',
                    main_numbers TEXT NOT NULL,
                    bonus_number INTEGER,
                    cost REAL NOT NULL,
                    winnings REAL NOT NULL DEFAULT 0,
                    net_balance REAL NOT NULL
                )
            """)
            cursor.execute("""
                INSERT INTO tickets_new (id, game, main_numbers, bonus_number, cost, winnings, net_balance)
                SELECT id, 'PowerBall', main_numbers, powerball, cost, winnings, net_balance FROM tickets
            """)
            cursor.execute("DROP TABLE tickets")
            cursor.execute("ALTER TABLE tickets_new RENAME TO tickets")
            conn.commit()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS tickets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            game TEXT NOT NULL DEFAULT 'PowerBall',
            main_numbers TEXT NOT NULL,
            bonus_number INTEGER,
            cost REAL NOT NULL,
            winnings REAL NOT NULL DEFAULT 0,
            net_balance REAL NOT NULL
        )
    """)
    # Catch any other older variant missing the 'game' or 'bonus_number'
    # columns but without the NOT NULL issue above.
    cursor.execute("PRAGMA table_info(tickets)")
    cols = [row[1] for row in cursor.fetchall()]
    if "game" not in cols:
        cursor.execute("ALTER TABLE tickets ADD COLUMN game TEXT NOT NULL DEFAULT 'PowerBall'")
    if "bonus_number" not in cols and "powerball" in cols:
        cursor.execute("ALTER TABLE tickets ADD COLUMN bonus_number INTEGER")
        cursor.execute("UPDATE tickets SET bonus_number = powerball WHERE bonus_number IS NULL")

    # Draw results: one row per game per draw date. dividends_json stores
    # a {"Prize Tier label": amount_in_rand} mapping you fill in once the
    # official results/payouts are published, so you don't have to
    # hardcode prize money that actually changes every single draw.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS draws (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            game TEXT NOT NULL,
            draw_date TEXT NOT NULL,
            main_numbers TEXT NOT NULL,
            bonus_number INTEGER,
            dividends_json TEXT NOT NULL DEFAULT '{}',
            entered_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(game, draw_date)
        )
    """)

    conn.commit()
    conn.close()


def load_tickets_from_db():
    conn = get_connection()
    df = pd.read_sql_query(
        "SELECT id, game, main_numbers, bonus_number, cost, winnings, net_balance "
        "FROM tickets ORDER BY id DESC",
        conn,
    )
    conn.close()
    df.columns = ["ID", "Game", "Main Numbers", "Bonus Number", "Ticket Cost (ZAR)", "Winnings (ZAR)", "Net Profit/Loss"]
    return df


def save_single_ticket_to_db(game, main_numbers_str, bonus_number, cost, winnings):
    conn = get_connection()
    cursor = conn.cursor()
    net = float(winnings) - float(cost)
    cursor.execute(
        """
        INSERT INTO tickets (game, main_numbers, bonus_number, cost, winnings, net_balance)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (str(game), str(main_numbers_str), bonus_number, float(cost), float(winnings), float(net)),
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
        bonus = row["Bonus Number"]
        bonus = None if pd.isna(bonus) else int(bonus)
        cursor.execute(
            """
            INSERT INTO tickets (game, main_numbers, bonus_number, cost, winnings, net_balance)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (str(row["Game"]), str(row["Main Numbers"]), bonus, cost_val, win_val, net_val),
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


# --- Draws (results + dividends) ---
import json
from datetime import date as _date


def save_draw_result(game, draw_date, main_numbers_str, bonus_number, dividends_dict):
    """Saves or overwrites a draw result for (game, draw_date). Overwriting
    is intentional - if you mistyped the dividends, re-saving the same date
    corrects it rather than creating a duplicate row."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO draws (game, draw_date, main_numbers, bonus_number, dividends_json)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(game, draw_date) DO UPDATE SET
            main_numbers = excluded.main_numbers,
            bonus_number = excluded.bonus_number,
            dividends_json = excluded.dividends_json,
            entered_at = datetime('now')
        """,
        (str(game), str(draw_date), str(main_numbers_str), bonus_number, json.dumps(dividends_dict)),
    )
    conn.commit()
    conn.close()


def load_draws_from_db(game=None):
    conn = get_connection()
    query = "SELECT id, game, draw_date, main_numbers, bonus_number, dividends_json, entered_at FROM draws"
    params = ()
    if game:
        query += " WHERE game = ?"
        params = (game,)
    query += " ORDER BY draw_date DESC"
    df = pd.read_sql_query(query, conn, params=params)
    conn.close()
    return df


def get_latest_draw(game):
    df = load_draws_from_db(game)
    if df.empty:
        return None
    return df.iloc[0]


def delete_draw(draw_id):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM draws WHERE id = ?", (int(draw_id),))
    conn.commit()
    conn.close()


def update_ticket_winnings(ticket_id, winnings):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT cost FROM tickets WHERE id = ?", (int(ticket_id),))
    row = cursor.fetchone()
    if row is None:
        conn.close()
        return False
    cost = row[0]
    net = float(winnings) - float(cost)
    cursor.execute(
        "UPDATE tickets SET winnings = ?, net_balance = ? WHERE id = ?",
        (float(winnings), net, int(ticket_id)),
    )
    conn.commit()
    conn.close()
    return True


def reconcile_tickets_against_draw(game, draw_date):
    """Checks every ticket for `game` against the saved draw on `draw_date`,
    looks up the dividend for whatever tier each ticket matched, and writes
    that amount into the ticket's winnings field. Returns a results
    DataFrame for display. This is the 'auto-update after each draw' step -
    it runs the moment you save a draw result, not on a timer, since there's
    no background process here to run it for you."""
    draws_df = load_draws_from_db(game)
    draw_row = draws_df[draws_df["draw_date"] == str(draw_date)]
    if draw_row.empty:
        return None
    draw_row = draw_row.iloc[0]

    official_main = [int(x.strip()) for x in draw_row["main_numbers"].split(",")]
    official_bonus = draw_row["bonus_number"]
    official_bonus = None if pd.isna(official_bonus) else int(official_bonus)
    dividends = json.loads(draw_row["dividends_json"])

    conn = get_connection()
    tickets_df = pd.read_sql_query(
        "SELECT id, main_numbers, bonus_number, cost FROM tickets WHERE game = ?",
        conn, params=(game,),
    )
    conn.close()

    results = []
    for _, t in tickets_df.iterrows():
        outcome, matched_main, matched_bonus = calculate_matches(
            t["main_numbers"], t["bonus_number"], game, official_main, official_bonus
        )
        payout = dividends.get(outcome, 0.0)
        update_ticket_winnings(t["id"], payout)
        results.append({
            "Ticket ID": t["id"],
            "Main Numbers": t["main_numbers"],
            "Result": outcome,
            "Payout (ZAR)": payout,
        })
    return pd.DataFrame(results)


# =========================================================
# 2. VALIDATION
# =========================================================
def parse_main_numbers(raw, game_name):
    g = GAMES[game_name]
    n_required = g["main_drawn"]
    pool = g["main_pool"]
    try:
        parts = [p.strip() for p in str(raw).split(",") if p.strip() != ""]
        nums = [int(p) for p in parts]
    except ValueError:
        raise ValueError(f"Could not parse numbers from '{raw}'. Use comma-separated integers.")

    if len(nums) != n_required:
        raise ValueError(f"{game_name} needs {n_required} main numbers, got {len(nums)} in '{raw}'.")
    if len(set(nums)) != n_required:
        raise ValueError(f"Main numbers must be unique: '{raw}'.")
    if any(n < 1 or n > pool for n in nums):
        raise ValueError(f"Main numbers must be between 1 and {pool} for {game_name}: '{raw}'.")
    return sorted(nums)


def validate_bonus_number(bonus, game_name):
    g = GAMES[game_name]
    if g["bonus_pool"] is None:
        return None  # this game has no bonus number; nothing to validate
    try:
        b_int = int(bonus)
    except (ValueError, TypeError):
        raise ValueError(f"{g['bonus_label']} '{bonus}' is not a valid integer.")
    if b_int < 1 or b_int > g["bonus_pool"]:
        raise ValueError(f"{g['bonus_label']} must be between 1 and {g['bonus_pool']}, got {b_int}.")
    return b_int


# =========================================================
# 3. MATCHING ENGINE
# =========================================================
def calculate_matches(ticket_numbers_str, ticket_bonus, game_name, official_main_list, official_bonus):
    g = GAMES.get(game_name)
    if g is None:
        return "Unknown Game", 0, False
    try:
        ticket_nums = set(parse_main_numbers(ticket_numbers_str, game_name))
        ticket_bonus_val = validate_bonus_number(ticket_bonus, game_name) if g["bonus_pool"] else None
    except ValueError:
        return "Invalid Number Format", 0, False

    official_nums = set(official_main_list)
    matched_main = len(ticket_nums.intersection(official_nums))
    has_bonus = g["bonus_pool"] is not None
    matched_bonus = (has_bonus and official_bonus is not None and int(ticket_bonus_val) == int(official_bonus))

    if has_bonus:
        bonus_label = g["bonus_label"]
        if matched_main == g["main_drawn"] and matched_bonus:
            return f"JACKPOT! (Match {matched_main} + {bonus_label})", matched_main, matched_bonus
        elif matched_main >= 2 and matched_bonus:
            return f"Match {matched_main} + {bonus_label}", matched_main, matched_bonus
        elif matched_main >= 3:
            return f"Match {matched_main}", matched_main, matched_bonus
        elif matched_main == 1 and matched_bonus:
            return f"Match 1 + {bonus_label}", matched_main, matched_bonus
        elif matched_main == 0 and matched_bonus:
            return f"Match {bonus_label} only", matched_main, matched_bonus
        else:
            return "No Prize", matched_main, matched_bonus
    else:
        min_match = g["min_match_for_prize"]
        if matched_main >= min_match:
            label = f"Match {matched_main}" + (" (Jackpot)" if matched_main == g["main_drawn"] else "")
            return label, matched_main, False
        return "No Prize", matched_main, False


def estimate_jackpot_odds(game_name, number_of_boards):
    if number_of_boards <= 0:
        return 0.0
    return (number_of_boards / total_combinations(game_name)) * 100


# =========================================================
# 4. EMAIL
# =========================================================
def send_budget_email(to_email, df_summary, filename="lottery_budget.xlsx"):
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
    msg["Subject"] = "Weekly Lottery Ledger Report"

    by_game = df_summary.groupby("Game")["Net Profit/Loss"].sum().to_dict()
    by_game_lines = "\n".join(f"  {g}: R{v:.2f}" for g, v in by_game.items())

    body = (
        "Attached is your complete lottery dashboard spreadsheet tracking report.\n\n"
        f"Summary Metrics:\nTotal Invested: R{total_spent:.2f}\n"
        f"Total Returns: R{total_won:.2f}\nNet Status: R{net_balance:.2f}\n\n"
        f"By game:\n{by_game_lines}"
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

st.set_page_config(page_title="National Lottery Ledger", page_icon="ðï¸", layout="wide")
st.title("ðï¸ Permanent National Lottery Ledger")
st.caption("Tracks PowerBall, PowerBall XTRA, Lotto, Lotto Plus 1, and Lotto 5 Max in one place.")

df_current = load_tickets_from_db()
total_records = len(df_current)

col_m1, col_m2, col_m3 = st.columns(3)
with col_m1:
    st.metric(label="Total Boards Logged", value=f"{total_records}")
with col_m2:
    current_net = df_current["Net Profit/Loss"].sum() if total_records > 0 else 0.0
    st.metric(label="Net Balance (ZAR)", value=f"R{current_net:,.2f}", delta=f"{current_net:.2f}")
with col_m3:
    if total_records > 0:
        by_game_counts = df_current["Game"].value_counts().to_dict()
        top_game = max(by_game_counts, key=by_game_counts.get)
        st.metric(label="Most-Played Game", value=top_game, delta=f"{by_game_counts[top_game]} boards")
    else:
        st.metric(label="Most-Played Game", value="â")

st.markdown("---")

tab1, tab2, tab3, tab5, tab4 = st.tabs(
    ["ð Entry & Quick Pick", "ð¤ Smart Bulk Upload", "ð® Ticket Checker Engine",
     "ð Draw Results & Dividends", "âï¸ Email & Settings"]
)

# ---------------------------------------------------------
# TAB 1: ENTRY & QUICK PICK
# ---------------------------------------------------------
with tab1:
    col1, col2 = st.columns(2)
    with col1:
        st.write("#### ðï¸ Log or Generate Individual Line")
        game_choice = st.selectbox("Select Game:", GAME_NAMES, key="game_choice")
        g = GAMES[game_choice]
        selected_cost = g["cost"]
        st.caption(
            f"{game_choice}: pick {g['main_drawn']} numbers from 1â{g['main_pool']}"
            + (f", plus 1 {g['bonus_label']} from 1â{g['bonus_pool']}" if g["bonus_pool"] else "")
            + f"  â¢  Cost: R{selected_cost:.2f}"
            + (f" (add-on to {g['is_addon_of']})" if "is_addon_of" in g else "")
        )

        st.write("##### Need numbers? Generate a random board:")
        st.caption("This generates a board AND saves it to the ledger immediately, at this game's cost.")
        qp_col1, qp_col2 = st.columns(2)
        with qp_col1:
            single_clicked = st.button("ð² Generate & Save Quick Pick")
        with qp_col2:
            all_clicked = st.button("ð²ð² Generate One of Every Game")

        if single_clicked:
            random_main = sorted(random.sample(range(1, g["main_pool"] + 1), g["main_drawn"]))
            random_bonus = random.randint(1, g["bonus_pool"]) if g["bonus_pool"] else None

            num_str = ", ".join(map(str, random_main))
            save_single_ticket_to_db(game_choice, num_str, random_bonus, selected_cost, 0.0)

            bonus_part = f" | {g['bonus_label']}: {random_bonus}" if random_bonus is not None else ""
            st.session_state["qp_last_saved"] = f"{game_choice}: {num_str}{bonus_part} | R{selected_cost:.2f}"
            st.session_state["qp_last_game"] = game_choice
            st.session_state.pop("qp_all_results", None)
            st.rerun()

        if all_clicked:
            all_results = []
            total_cost = 0.0
            for gname, gg in GAMES.items():
                rm = sorted(random.sample(range(1, gg["main_pool"] + 1), gg["main_drawn"]))
                rb = random.randint(1, gg["bonus_pool"]) if gg["bonus_pool"] else None
                num_str = ", ".join(map(str, rm))
                save_single_ticket_to_db(gname, num_str, rb, gg["cost"], 0.0)
                total_cost += gg["cost"]
                bonus_part = f" | {gg['bonus_label']}: {rb}" if rb is not None else ""
                all_results.append(f"{gname}: {num_str}{bonus_part} | R{gg['cost']:.2f}")
            st.session_state["qp_all_results"] = all_results
            st.session_state["qp_all_total_cost"] = total_cost
            st.session_state.pop("qp_last_saved", None)
            st.rerun()

        if st.session_state.get("qp_all_results"):
            st.success(f"Saved one ticket for all {len(GAME_NAMES)} games (total: R{st.session_state['qp_all_total_cost']:.2f}):")
            for line in st.session_state["qp_all_results"]:
                st.text(f"  â¢ {line}")

        if st.session_state.get("qp_last_saved"):
            st.success(f"Saved: {st.session_state['qp_last_saved']}")

            odds_game = st.session_state.get("qp_last_game", game_choice)
            odds_rows, overall_odds = build_odds_table(odds_game)
            jackpot_odds = odds_rows[0]["Odds (1 in X)"]
            st.caption(
                f"ð¯ This board's jackpot odds ({odds_game}): **1 in {jackpot_odds:,.0f}**  |  "
                f"Odds of winning *something*: **1 in {overall_odds:.1f}**"
            )
            with st.expander(f"ð Full odds breakdown â {odds_game}"):
                st.dataframe(
                    pd.DataFrame(odds_rows),
                    use_container_width=True,
                    hide_index=True,
                    column_config={"Odds (1 in X)": st.column_config.NumberColumn(format="%.1f")},
                )
                st.caption(
                    "These odds are identical for every possible board â the numbers you "
                    "picked don't change them. Shown here purely for reference."
                )

        st.write("##### Or set numbers manually and save below:")
        manual_main_default = ", ".join(str(n) for n in range(4, 4 + g["main_drawn"] * 7, 7))[: 5 * g["main_drawn"]]
        manual_numbers_raw = st.text_input(
            f"Main numbers (comma-separated, {g['main_drawn']} numbers from 1â{g['main_pool']})",
            value="",
            placeholder="e.g. " + ", ".join(str(n) for n in range(1, g["main_drawn"] + 1)),
            key="manual_numbers",
        )
        manual_bonus_raw = None
        if g["bonus_pool"]:
            manual_bonus_raw = st.number_input(
                g["bonus_label"], 1, g["bonus_pool"], 1, key="manual_bonus"
            )

        winnings = st.number_input("Prize Money Won (Rands)", min_value=0.0, value=0.0, step=5.00, key="num_win")

        if st.button("ð¾ Save Ticket to Ledger"):
            try:
                parsed = parse_main_numbers(manual_numbers_raw, game_choice)
                bonus_val = validate_bonus_number(manual_bonus_raw, game_choice) if g["bonus_pool"] else None
                num_str = ", ".join(map(str, parsed))
                save_single_ticket_to_db(game_choice, num_str, bonus_val, selected_cost, winnings)
                st.success("Record saved!")
                st.rerun()
            except ValueError as e:
                st.error(str(e))

    with col2:
        st.write("#### ð Current Active Table Records")
        if total_records > 0:
            game_filter = st.multiselect("Filter by game:", GAME_NAMES, default=GAME_NAMES, key="game_filter")
            filtered_df = df_current[df_current["Game"].isin(game_filter)] if game_filter else df_current
            st.dataframe(filtered_df, use_container_width=True, hide_index=True)

            with st.expander("ðï¸ Delete a ticket"):
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
    st.write("#### ð Upload an Excel or CSV File to Bulk Save Tickets")
    st.write("Your spreadsheet headers must match these names exactly:")
    st.code(" | ".join(REQUIRED_BULK_COLUMNS))
    st.caption(
        "`Game` must be one of: " + ", ".join(GAME_NAMES) + ". "
        "`Main Numbers` should be comma-separated integers matching that game's "
        "format (5 for PowerBall games, 6 for Lotto games). Leave `Bonus Number` "
        "blank for Lotto/Lotto Plus 1/Lotto 5 Max."
    )

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
                        game_val = str(row["Game"]).strip()
                        if game_val not in GAMES:
                            raise ValueError(f"Unknown game '{game_val}'. Must be one of: {', '.join(GAME_NAMES)}")
                        nums = parse_main_numbers(row["Main Numbers"], game_val)
                        bonus_raw = row.get("Bonus Number")
                        bonus_val = None
                        if GAMES[game_val]["bonus_pool"]:
                            if pd.isna(bonus_raw):
                                raise ValueError(f"{game_val} requires a Bonus Number.")
                            bonus_val = validate_bonus_number(bonus_raw, game_val)
                        cost_val = float(row["Ticket Cost (ZAR)"])
                        win_val = float(row["Winnings (ZAR)"])
                        if cost_val < 0 or win_val < 0:
                            raise ValueError("Cost and winnings cannot be negative.")
                        valid_rows.append(
                            {
                                "Game": game_val,
                                "Main Numbers": ", ".join(map(str, nums)),
                                "Bonus Number": bonus_val,
                                "Ticket Cost (ZAR)": cost_val,
                                "Winnings (ZAR)": win_val,
                            }
                        )
                    except (ValueError, TypeError, KeyError) as e:
                        errors.append(f"Row {idx + 1}: {e}")

                if errors:
                    st.warning(f"{len(errors)} row(s) failed validation and will be skipped:")
                    for err in errors[:20]:
                        st.text(f"  â¢ {err}")
                    if len(errors) > 20:
                        st.text(f"  ...and {len(errors) - 20} more.")

                st.info(f"{len(valid_rows)} of {len(uploaded_df)} row(s) are ready to import.")

                if valid_rows and st.button("ð¥ Import Valid Rows to Ledger"):
                    inserted = save_bulk_df_to_db(pd.DataFrame(valid_rows))
                    st.success(f"Imported {inserted} ticket(s) successfully!")
                    st.rerun()

        except Exception as e:
            st.error(f"Could not read file: {e}")

# ---------------------------------------------------------
# TAB 3: TICKET CHECKER ENGINE
# ---------------------------------------------------------
with tab3:
    st.write("#### ð® Check Your Logged Tickets Against the Official Draw")

    if total_records == 0:
        st.info("No tickets logged yet. Add some in the Entry tab first.")
    else:
        check_game = st.selectbox("Which game's draw are you checking?", GAME_NAMES, key="check_game")
        gc = GAMES[check_game]

        st.write(f"##### Enter the official {check_game} winning numbers:")
        official_cols = st.columns(gc["main_drawn"] + (1 if gc["bonus_pool"] else 0))
        official_main = []
        for i in range(gc["main_drawn"]):
            with official_cols[i]:
                val = st.number_input(f"#{i+1}", 1, gc["main_pool"], i + 1, key=f"off_main_{i}")
                official_main.append(val)
        official_bonus = None
        if gc["bonus_pool"]:
            with official_cols[-1]:
                official_bonus = st.number_input(gc["bonus_label"], 1, gc["bonus_pool"], 1, key="off_bonus")

        if len(set(official_main)) != gc["main_drawn"]:
            st.error(f"Official main numbers must be {gc['main_drawn']} distinct values.")
        else:
            if st.button("ð Check All Tickets"):
                relevant = df_current[df_current["Game"] == check_game]
                if relevant.empty:
                    st.info(f"No logged tickets found for {check_game}.")
                else:
                    results = []
                    for _, row in relevant.iterrows():
                        outcome, matched_main, matched_bonus = calculate_matches(
                            row["Main Numbers"], row["Bonus Number"], check_game, official_main, official_bonus
                        )
                        results.append(
                            {
                                "ID": row["ID"],
                                "Main Numbers": row["Main Numbers"],
                                "Bonus": row["Bonus Number"],
                                "Main Matches": matched_main,
                                "Bonus Match": "â" if matched_bonus else "â",
                                "Result": outcome,
                            }
                        )
                    results_df = pd.DataFrame(results).sort_values("Main Matches", ascending=False)
                    st.dataframe(results_df, use_container_width=True, hide_index=True)

                    winners = results_df[results_df["Result"] != "No Prize"]
                    if not winners.empty:
                        st.success(f"ð {len(winners)} ticket(s) matched at least one number!")
                    else:
                        st.info("No matches this time. Better luck next draw!")

# ---------------------------------------------------------
# TAB 5: DRAW RESULTS & DIVIDENDS
# ---------------------------------------------------------
with tab5:
    st.write("#### ð Log an Official Draw Result")
    st.caption(
        "There's no way for this app to fetch live results automatically â "
        "enter the date, winning numbers, and the dividend (prize money) for "
        "each tier once they're published. The moment you save, every ticket "
        "you logged for that game gets automatically checked and its winnings "
        "updated. That's the closest thing to 'auto-update' available without "
        "a live data feed."
    )

    dr_game = st.selectbox("Game:", GAME_NAMES, key="draw_game")
    dg = GAMES[dr_game]

    dr_col1, dr_col2 = st.columns([1, 2])
    with dr_col1:
        dr_date = st.date_input("Draw date:", value=_date.today(), key="draw_date_input")
    with dr_col2:
        dr_numbers_raw = st.text_input(
            f"Winning numbers ({dg['main_drawn']} numbers, comma-separated, 1â{dg['main_pool']})",
            placeholder=", ".join(str(n) for n in range(1, dg["main_drawn"] + 1)),
            key="draw_numbers_input",
        )

    dr_bonus_raw = None
    if dg["bonus_pool"]:
        dr_bonus_raw = st.number_input(
            f"Winning {dg['bonus_label']} (1â{dg['bonus_pool']})", 1, dg["bonus_pool"], 1, key="draw_bonus_input"
        )

    st.write("##### Dividends per prize tier (enter what was published, leave 0 if a tier had no winners)")
    odds_rows, _ = build_odds_table(dr_game)
    dividend_inputs = {}
    div_cols = st.columns(3)
    for i, row in enumerate(odds_rows):
        tier_label = row["Prize Tier"]
        with div_cols[i % 3]:
            dividend_inputs[tier_label] = st.number_input(
                tier_label, min_value=0.0, value=0.0, step=10.0, key=f"div_{dr_game}_{i}"
            )

    if st.button("ð¾ Save Draw Result & Auto-Check My Tickets"):
        try:
            parsed_numbers = parse_main_numbers(dr_numbers_raw, dr_game)
            parsed_bonus = validate_bonus_number(dr_bonus_raw, dr_game) if dg["bonus_pool"] else None
            num_str = ", ".join(map(str, parsed_numbers))
            save_draw_result(dr_game, dr_date.isoformat(), num_str, parsed_bonus, dividend_inputs)

            results = reconcile_tickets_against_draw(dr_game, dr_date.isoformat())
            st.success(f"Draw saved for {dr_game} on {dr_date.isoformat()}.")
            if results is not None and not results.empty:
                st.write(f"##### Auto-checked {len(results)} {dr_game} ticket(s):")
                st.dataframe(results, use_container_width=True, hide_index=True)
                won = results[results["Payout (ZAR)"] > 0]
                if not won.empty:
                    st.success(f"ð {len(won)} ticket(s) won a total of R{won['Payout (ZAR)'].sum():,.2f}!")
            else:
                st.info(f"No {dr_game} tickets logged yet to check against this draw.")
            st.rerun()
        except ValueError as e:
            st.error(str(e))

    st.markdown("---")
    st.write("#### ðï¸ Draw History")
    history_game_filter = st.selectbox("Show history for:", ["All Games"] + GAME_NAMES, key="history_filter")
    history_df = load_draws_from_db(None if history_game_filter == "All Games" else history_game_filter)

    if history_df.empty:
        st.info("No draw results logged yet.")
    else:
        display_df = history_df[["id", "game", "draw_date", "main_numbers", "bonus_number"]].copy()
        display_df.columns = ["ID", "Game", "Draw Date", "Winning Numbers", "Bonus"]
        st.dataframe(display_df, use_container_width=True, hide_index=True)

        with st.expander("ðï¸ Delete a draw result"):
            st.caption("Deleting a draw does not undo ticket winnings already applied â re-check manually if needed.")
            draw_to_delete = st.selectbox("Select draw ID to delete:", history_df["id"].tolist(), key="del_draw_id")
            if st.button("Delete selected draw"):
                delete_draw(draw_to_delete)
                st.success(f"Draw #{draw_to_delete} deleted.")
                st.rerun()

# ---------------------------------------------------------
# TAB 4: EMAIL & SETTINGS
# ---------------------------------------------------------
with tab4:
    st.write("#### âï¸ Email Yourself a Budget Report")
    st.caption(
        "Requires Streamlit secrets configured as:\n\n"
        "```\n[email_credentials]\nsender_email = \"you@gmail.com\"\n"
        "app_password = \"xxxx xxxx xxxx xxxx\"\n```"
    )

    to_email = st.text_input("Send report to:", placeholder="someone@example.com")
    if st.button("ð§ Send Report"):
        if total_records == 0:
            st.warning("Nothing to report yet â log at least one ticket first.")
        elif not to_email or "@" not in to_email:
            st.error("Please enter a valid email address.")
        else:
            with st.spinner("Sending..."):
                success = send_budget_email(to_email, df_current)
            if success:
                st.success(f"Report sent to {to_email}!")

    st.markdown("---")
    st.write("#### ð Game Rules Reference")
    rules_rows = []
    for name, g in GAMES.items():
        rules_rows.append(
            {
                "Game": name,
                "Pick": f"{g['main_drawn']} from 1-{g['main_pool']}" + (f" + 1 {g['bonus_label']} from 1-{g['bonus_pool']}" if g["bonus_pool"] else ""),
                "Cost (ZAR)": f"R{g['cost']:.2f}",
                "Jackpot Odds": f"1 in {total_combinations(name):,}",
            }
        )
    st.dataframe(pd.DataFrame(rules_rows), use_container_width=True, hide_index=True)

    st.markdown("---")
    st.write("#### â ï¸ Danger Zone")
    st.caption("This permanently deletes every ticket in the local database. This cannot be undone.")
    confirm_wipe = st.checkbox("I understand this will permanently delete all records.")
    if st.button("ðï¸ Clear Entire Database", disabled=not confirm_wipe):
        clear_entire_database()
        st.success("Database cleared.")
        st.rerun()
