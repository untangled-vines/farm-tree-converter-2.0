import streamlit as st
import pandas as pd
import psycopg2
from psycopg2.extras import execute_values
import io
import math

# DB connection details from Streamlit secrets
DB_HOST = st.secrets["DB_HOST"]
DB_PORT = st.secrets["DB_PORT"]
DB_NAME = st.secrets["DB_NAME"]
DB_USER = st.secrets["DB_USER"]
DB_PASSWORD = st.secrets["DB_PASSWORD"]
DB_SCHEMA = st.secrets["DB_SCHEMA"]

# ---------------------------------------------------------------------------
# FORMAT DETECTION & NORMALISATION
# ---------------------------------------------------------------------------
# Signature column that only exists in the new (Format B) CSV
FORMAT_B_SIGNATURE = "Reference Number Local Partner"

# Map Format B column names → existing prodai column names (Format A names).
# Only columns whose names differ need to appear here.
# Columns that are identical in both formats are left as-is.
# Columns that exist in Format A but not Format B will be absent from the
# DataFrame after renaming; load_csv_to_db will insert NULL for them
# automatically because Postgres will not find a matching column to fill.
FORMAT_B_COLUMN_MAP = {
    "Reference Number Local Partner":       "address",           # closest equivalent – adjust if you have a dedicated column
    "Farmer Agreement Signed":              "farm_agreement_signed",
    "Land Use":                             "land_use",          # Format-B-only → NULL in Format A rows (add col to table if needed)
    "Land Ownership":                       "land_tenure",       # maps to existing land-tenure concept
    "Full Name":                            "full_name",         # Format-B-only extra
    "Phone Number new":                     "phone_number_alt",  # Format-B-only extra
    "Plots1 Agroforestry Type":             "plots1_agroforestry_design",
    # Portuguese admin fields – map to nullable columns in prodai
    "Provincia":                            "provincia",
    "Distrito":                             "distrito",
    "Posto Administrativo":                 "posto_administrativo",
    "Localidade":                           "localidade",
    "Povoado":                              "povoado",
    # Verification / matching fields
    "Rosto do Beneficiário - match name":   "match_name",
    "Rosto do Beneficiário - match ID Number": "match_id_number",
    "Data de Registo":                      "data_de_registo",
    "Elegivel?":                            "elegivel",
    "Plot Are > 1 ha":                      "plot_area_gt_1ha",
    "Match contact":                        "match_contact",
    "Match Name":                           "match_name_2",
    "Tem Cartao":                           "tem_cartao",
}

def detect_format(df: pd.DataFrame) -> str:
    """Return 'B' if this is the new format, 'A' for the original."""
    return "B" if FORMAT_B_SIGNATURE in df.columns else "A"

def get_prodai_columns() -> list:
    """Return the actual column names present in the prodai table."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = %s AND table_name = 'prodai'",
        (DB_SCHEMA,)
    )
    cols = [row[0] for row in cur.fetchall()]
    cur.close()
    conn.close()
    return cols

def normalise_format_b(df: pd.DataFrame, existing_columns: list) -> pd.DataFrame:
    """
    Rename Format B columns to match the existing prodai schema (Format A),
    then drop any column that doesn't exist in prodai — no DB changes needed.
    """
    rename = {k: v for k, v in FORMAT_B_COLUMN_MAP.items() if k in df.columns}
    df = df.rename(columns=rename)
    df.columns = [c.lower().replace(' ', '_') for c in df.columns]
    # Drop columns not in prodai immediately to free memory
    cols_to_keep = [c for c in df.columns if c in existing_columns]
    df = df[cols_to_keep]
    return df

# ---------------------------------------------------------------------------
# DATABASE HELPERS  (unchanged from original)
# ---------------------------------------------------------------------------

def get_connection():
    return psycopg2.connect(
        host=DB_HOST,
        port=DB_PORT,
        dbname=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD
    )

def strip_dot_zero(x, column_name=None):
    """Strip trailing .0 from integer-like floats, leave real decimals intact."""
    s = str(x) if x is not None else ''
    if '.' in s:
        parts = s.split('.')
        if parts[1] == '0' and parts[0].isdigit():
            return parts[0]
    return s

def safe_null(v):
    """Return None for any NaN/null variant, strip .0 from integer-like floats."""
    if v is None:
        return None
    if isinstance(v, float):
        if math.isnan(v):
            return None
        if v == int(v):
            return int(v)
        return v
    if isinstance(v, str):
        if v.strip().lower() in ('nan', 'none', 'nat', ''):
            return None
        if '.' in v:
            parts = v.split('.')
            if parts[1] == '0' and parts[0].lstrip('-').isdigit():
                return int(parts[0])
        return v
    return v

def load_csv_to_db(df):
    conn = get_connection()
    cur = conn.cursor()

    df.columns = [c.lower().replace(' ', '_') for c in df.columns]
    # De-duplicate columns (safety net — should not occur after normalisation)
    df = df.loc[:, ~df.columns.duplicated()]

    cur.execute(f"TRUNCATE {DB_SCHEMA}.prodai")

    cols = ','.join([f'"{c}"' for c in df.columns])
    batch_size = 500
    batch = []

    for _, row in df.iterrows():
        batch.append(tuple(
            safe_null(strip_dot_zero(v, 'year')) if 'planting_year' in col and col.startswith('plots')
            else safe_null(strip_dot_zero(v))
            for col, v in zip(df.columns, row)
        ))
        if len(batch) >= batch_size:
            execute_values(cur, f"INSERT INTO {DB_SCHEMA}.prodai ({cols}) VALUES %s", batch)
            conn.commit()
            batch.clear()

    # Insert any remaining rows
    if batch:
        execute_values(cur, f"INSERT INTO {DB_SCHEMA}.prodai ({cols}) VALUES %s", batch)
        conn.commit()

    conn.commit()
    cur.close()
    conn.close()

def get_transformed_data():
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(f"SELECT * FROM {DB_SCHEMA}.prodai_transformed")
    rows = cur.fetchall()
    cols = [desc[0] for desc in cur.description]
    cur.close()
    conn.close()

    df = pd.DataFrame(rows, columns=cols)
    df = df.where(pd.notnull(df), None)

    def safe_str(v):
        if v is None:
            return ''
        from decimal import Decimal
        if isinstance(v, Decimal):
            return format(v.normalize(), 'f')
        return str(v)

    return df.apply(lambda col: col.map(safe_str)).replace({'None': '', 'nan': '', '<NA>': ''})

def df_to_csv(df):
    output = io.StringIO()
    df.to_csv(output, index=False, quoting=0)
    return output.getvalue().encode('utf-8')

# ---------------------------------------------------------------------------
# STREAMLIT UI
# ---------------------------------------------------------------------------

def main():
    st.title("🌱 Acorn → FarmTree Converter")
    st.write("Upload your Acorn CSV export to convert it to FarmTree multiplot format.")

    uploaded_file = st.file_uploader(
        "Upload Acorn CSV or Excel",
        type=["csv", "xlsx"],
        key="acorn_csv_uploader"
    )

    if uploaded_file:
        st.info("File uploaded — click Convert to process it.")

        if st.button("Convert", key="convert_btn"):
            with st.spinner("Loading data..."):
                try:
                    if uploaded_file.name.endswith('.xlsx'):
                        df_input = pd.read_excel(uploaded_file, engine='openpyxl')
                    else:
                        sample = uploaded_file.read(2048).decode('utf-8')
                        uploaded_file.seek(0)
                        delimiter = ';' if sample.count(';') > sample.count(',') else ','
                        df_input = pd.read_csv(uploaded_file, delimiter=delimiter, encoding='utf-8')
                    st.success(f"Loaded {len(df_input)} farmer records")
                except Exception as e:
                    st.error(f"Failed to read CSV: {e}")
                    st.stop()

            # ── FORMAT DETECTION & NORMALISATION (new lines) ──────────────
            fmt = detect_format(df_input)
            if fmt == "B":
                st.info("Detected new format — normalising columns before import.")
                existing_cols = get_prodai_columns()
                df_input = normalise_format_b(df_input, existing_cols)
            # ──────────────────────────────────────────────────────────────

            with st.spinner("Transforming data..."):
                try:
                    load_csv_to_db(df_input)
                    df_output = get_transformed_data()
                    st.success(f"Transformed {len(df_output)} plots successfully!")
                except Exception as e:
                    st.error(f"Transformation failed: {e}")
                    st.stop()

            csv_bytes = df_to_csv(df_output)
            st.download_button(
                label="⬇️ Download FarmTree CSV",
                data=csv_bytes,
                file_name="farmtree_export.csv",
                mime="text/csv"
            )

if __name__ == "__main__":
    main()
