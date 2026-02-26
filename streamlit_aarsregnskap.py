"""
Brreg Årsregnskap — Streamlit web app
======================================
Søk etter virksomhet og last ned årsregnskap-PDF-er direkte fra Brreg.

Deploy gratis på https://share.streamlit.io
"""

import base64
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
import streamlit as st
import streamlit.components.v1 as components

st.set_page_config(
    page_title="Brreg Årsregnskap",
    page_icon="📄",
    layout="centered",
)

ENHETER_URL   = "https://data.brreg.no/enhetsregisteret/api/enheter"
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

if "companies" not in st.session_state:
    st.session_state.companies = None

# ── 1. Search ─────────────────────────────────────────────────────────────────

with st.form("search_form"):
    query = st.text_input("Virksomhetsnavn", placeholder="f.eks. Equinor, DNB, Aker…")
    submitted = st.form_submit_button("🔍  Søk", type="primary")

if submitted and query.strip():
    with st.spinner("Søker…"):
        try:
            st.session_state.companies = search_companies(query.strip())
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

            # ── 4. Fetch all in parallel, push to browser Downloads ───────────

            if st.button("⬇  Last ned alle år", use_container_width=True, type="primary"):
                sorted_years = sorted(years)
                bar    = st.progress(0, text="Laster ned…")
                done   = 0
                results = {}
                errors  = []

                with ThreadPoolExecutor(max_workers=len(sorted_years)) as pool:
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

                if results:
                    # Build JS that triggers one browser download per PDF,
                    # staggered so the browser doesn't block them.
                    js_parts = []
                    for i, yr in enumerate(sorted(results.keys())):
                        b64  = base64.b64encode(results[yr]).decode("ascii")
                        fname = f"aarsregnskap-{yr}_{orgnr}.pdf"
                        js_parts.append(f"""
                            setTimeout(function() {{
                                var b = atob("{b64}");
                                var u = new Uint8Array(b.length);
                                for (var k = 0; k < b.length; k++) u[k] = b.charCodeAt(k);
                                var blob = new Blob([u], {{type: "application/pdf"}});
                                var url  = URL.createObjectURL(blob);
                                var a    = document.createElement("a");
                                a.href     = url;
                                a.download = "{fname}";
                                document.body.appendChild(a);
                                a.click();
                                document.body.removeChild(a);
                                URL.revokeObjectURL(url);
                            }}, {i * 600});
                        """)

                    components.html(
                        f"<script>{''.join(js_parts)}</script>",
                        height=1,
                    )
                    st.success(f"✅  {len(results)} PDF-er lastes ned til din nedlastingsmappe")

                if errors:
                    st.warning("Noen år feilet: " + " | ".join(errors))

# ── Footer ────────────────────────────────────────────────────────────────────
st.divider()
st.caption("Data fra [Brønnøysundregistrene](https://www.brreg.no) · Åpen API")
