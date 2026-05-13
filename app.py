import streamlit as st
import pandas as pd
import psycopg2
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

def normalise_format_b(df: pd.DataFrame) -> pd.DataFrame:
    """
    Rename Format B columns to match the existing prodai schema (Format A).
    Columns that only exist in Format A will simply be absent → NULL in DB.
    Columns that only exist in Format B are renamed to their new nullable
    counterparts in prodai (add those columns to the table if not present).
    """
    df = df.copy()
    rename = {k: v for k, v in FORMAT_B_COLUMN_MAP.items() if k in df.columns}
    df = df.rename(columns=rename)
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

    df = df.copy()
    df.columns = [c.lower().replace(' ', '_') for c in df.columns]

    cur.execute(f"TRUNCATE {DB_SCHEMA}.prodai")

    cols = ','.join([f'"{c}"' for c in df.columns])
    placeholders = ','.join(['%s'] * len(df.columns))

    for _, row in df.iterrows():
        values = [
            safe_null(strip_dot_zero(v, 'year')) if 'planting_year' in col and col.startswith('plots')
            else safe_null(strip_dot_zero(v))
            for col, v in zip(df.columns, row)
        ]
        cur.execute(
            f"INSERT INTO {DB_SCHEMA}.prodai ({cols}) VALUES ({placeholders})",
            values
        )

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

    # DEBUG - remove after diagnosis
    cov_col = [i for i, c in enumerate(cols) if 'CAFSSel02Cov' in c]
    if cov_col and rows:
        idx = cov_col[0]
        sample_val = rows[0][idx]
        st.write(f"DEBUG → CAFSSel02Cov raw value: `{sample_val}` | type: `{type(sample_val)}`")

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
                df_input = normalise_format_b(df_input)
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
