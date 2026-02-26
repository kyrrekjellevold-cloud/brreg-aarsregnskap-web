"""
Brreg Årsregnskap — Streamlit web app
======================================
Søk etter virksomhet og last ned årsregnskap-PDF-er direkte fra Brreg.

Deploy gratis på https://share.streamlit.io
"""

import io
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
import streamlit as st

st.set_page_config(
    page_title="Brreg Årsregnskap",
    page_icon="📄",
    layout="centered",
)

ENHETER_URL  = "https://data.brreg.no/enhetsregisteret/api/enheter"
REGNSKAP_BASE = "https://data.brreg.no/regnskapsregisteret/regnskap/aarsregnskap/kopi"


# ── API helpers ───────────────────────────────────────────────────────────────

def search_companies(query: str) -> list[dict]:
    r = requests.get(
        ENHETER_URL,
        params={"navn": query, "size": 20},
        headers={"Accept": "application/json"},
        timeout=10,
    )
    r.raise_for_status()
    return r.json().get("_embedded", {}).get("enheter", [])


def get_available_years(orgnr: str) -> list[str]:
    r = requests.get(
        f"{REGNSKAP_BASE}/{orgnr}/aar",
        headers={"Accept": "application/json"},
        timeout=10,
    )
    if r.status_code == 404:
        return []
    r.raise_for_status()
    return [str(y) for y in r.json()]


_thread_local = threading.local()

def _get_session() -> requests.Session:
    if not hasattr(_thread_local, "session"):
        _thread_local.session = requests.Session()
    return _thread_local.session

def fetch_pdf(orgnr: str, year: str) -> bytes:
    r = _get_session().get(
        f"{REGNSKAP_BASE}/{orgnr}/{year}",
        headers={"Accept": "application/octet-stream"},
        timeout=120,
    )
    r.raise_for_status()
    return r.content


# ── Page ─────────────────────────────────────────────────────────────────────

st.title("📄 Brreg Årsregnskap")
st.caption("Søk etter virksomhet og last ned årsregnskap-PDF-er fra Brønnøysundregistrene")

# Initialise session state
for key in ("companies", "pdf_bytes", "pdf_name", "zip_bytes", "zip_name"):
    if key not in st.session_state:
        st.session_state[key] = None

# ── 1. Search ─────────────────────────────────────────────────────────────────

with st.form("search_form"):
    query = st.text_input("Virksomhetsnavn", placeholder="f.eks. Equinor, DNB, Aker…")
    submitted = st.form_submit_button("🔍  Søk", type="primary")

if submitted and query.strip():
    with st.spinner("Søker…"):
        try:
            st.session_state.companies = search_companies(query.strip())
            st.session_state.pdf_bytes = None
            st.session_state.zip_bytes = None
        except Exception as e:
            st.error(f"Søkefeil: {e}")

# ── 2. Company picker → auto-load years ───────────────────────────────────────

if st.session_state.companies is not None:
    companies = st.session_state.companies
    if not companies:
        st.info("Ingen treff — prøv et annet søkeord.")
    else:
        labels = [f"{c['navn']}  ({c['organisasjonsnummer']})" for c in companies]
        idx = st.selectbox(
            "Velg virksomhet",
            range(len(labels)),
            format_func=lambda i: labels[i],
        )
        company = companies[idx]
        orgnr   = company["organisasjonsnummer"]

        # Mini info card
        col1, col2, col3 = st.columns(3)
        col1.metric("Org.nr.", orgnr)
        col2.metric("Form", (company.get("organisasjonsform") or {}).get("beskrivelse", "—"))
        col3.metric("Kommune", (company.get("forretningsadresse") or {}).get("kommune", "—"))

        # ── 3. Auto-load years ────────────────────────────────────────────────

        with st.spinner("Henter tilgjengelige år…"):
            try:
                years = get_available_years(orgnr)
            except Exception as e:
                st.error(f"Feil ved henting av år: {e}")
                years = []

        if not years:
            st.warning("Ingen årsregnskap funnet for denne virksomheten.")
        else:
            st.success(f"Tilgjengelige år: {', '.join(sorted(years))}")
            st.divider()

            year = st.selectbox("Velg år", sorted(years, reverse=True), key="year_select")

            col_a, col_b = st.columns(2)

            # --- Single year ---
            if col_a.button(f"⬇  Hent PDF for {year}", use_container_width=True):
                with st.spinner(f"Laster ned {year}…"):
                    try:
                        data = fetch_pdf(orgnr, year)
                        st.session_state.pdf_bytes = data
                        st.session_state.pdf_name  = f"aarsregnskap-{year}_{orgnr}.pdf"
                        st.session_state.zip_bytes = None
                    except Exception as e:
                        st.error(f"Nedlasting feilet: {e}")

            # --- All years as ZIP (parallel download) ---
            if col_b.button("⬇  Hent alle år (ZIP)", use_container_width=True):
                buf          = io.BytesIO()
                errors       = []
                sorted_years = sorted(years)
                bar          = st.progress(0, text="Laster ned…")
                done         = 0
                results      = {}

                with ThreadPoolExecutor(max_workers=6) as pool:
                    futures = {pool.submit(fetch_pdf, orgnr, yr): yr for yr in sorted_years}
                    for future in as_completed(futures):
                        yr = futures[future]
                        try:
                            results[yr] = future.result()
                        except Exception as e:
                            errors.append(f"{yr}: {e}")
                        done += 1
                        bar.progress(done / len(sorted_years), text=f"{done}/{len(sorted_years)} ferdig…")

                bar.empty()
                with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
                    for yr in sorted_years:
                        if yr in results:
                            zf.writestr(f"aarsregnskap-{yr}_{orgnr}.pdf", results[yr])
                if errors:
                    st.warning("Noen år feilet: " + " | ".join(errors))
                st.session_state.zip_bytes = buf.getvalue()
                st.session_state.zip_name  = f"aarsregnskap_{orgnr}_alle.zip"
                st.session_state.pdf_bytes = None

            # --- Download buttons (shown after fetch) ---
            if st.session_state.pdf_bytes:
                sz = len(st.session_state.pdf_bytes) // 1024
                st.download_button(
                    label=f"💾  Last ned {st.session_state.pdf_name}  ({sz} KB)",
                    data=st.session_state.pdf_bytes,
                    file_name=st.session_state.pdf_name,
                    mime="application/pdf",
                    type="primary",
                    use_container_width=True,
                )

            if st.session_state.zip_bytes:
                sz = len(st.session_state.zip_bytes) // 1024
                st.download_button(
                    label=f"💾  Last ned {st.session_state.zip_name}  ({sz} KB)",
                    data=st.session_state.zip_bytes,
                    file_name=st.session_state.zip_name,
                    mime="application/zip",
                    type="primary",
                    use_container_width=True,
                )

# ── Footer ────────────────────────────────────────────────────────────────────
st.divider()
st.caption("Data fra [Brønnøysundregistrene](https://www.brreg.no) · Åpen API")
