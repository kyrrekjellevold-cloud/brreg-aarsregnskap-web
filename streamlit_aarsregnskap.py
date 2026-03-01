"""
Brreg Årsregnskap — Streamlit web app
======================================
Søk etter virksomhet og last ned årsregnskap-PDF-er direkte fra Brreg.

Deploy gratis på https://share.streamlit.io
"""

import io
import json
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
import requests
import streamlit as st
from mistralai import Mistral

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


def search_by_orgnr(orgnr: str) -> list[dict]:
    r = requests.get(
        f"{ENHETER_URL}/{orgnr}",
        headers={"Accept": "application/json"},
        timeout=10,
    )
    if r.status_code == 404:
        return []
    r.raise_for_status()
    return [r.json()]


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


# ── Mistral OCR helpers ───────────────────────────────────────────────────────

def _mistral_client() -> Mistral:
    return Mistral(api_key=st.secrets["MISTRAL_API_KEY"])


def ocr_pdf(pdf_bytes: bytes, filename: str = "document.pdf") -> str:
    client = _mistral_client()
    uploaded = client.files.upload(
        file={"file_name": filename, "content": pdf_bytes},
        purpose="ocr",
    )
    try:
        signed = client.files.get_signed_url(file_id=uploaded.id)
        result = client.ocr.process(
            model="mistral-ocr-latest",
            document={"type": "document_url", "document_url": signed.url},
        )
        return "\n\n".join(page.markdown for page in result.pages)
    finally:
        client.files.delete(file_id=uploaded.id)


def _extract_financial_sections(ocr_text: str, max_chars: int = 40000) -> str:
    """Return the portion of OCR text most likely to contain financial tables."""
    lower = ocr_text.lower()
    anchors = [
        "resultatregnskap", "resultat regnskap",
        "driftsinntekt", "salgsinntekt",
        "balanse", "eiendeler",
    ]
    start = len(ocr_text)
    for kw in anchors:
        idx = lower.find(kw)
        if idx != -1:
            start = min(start, idx)
    if start == len(ocr_text):
        # No anchors found — fall back to beginning
        start = 0
    # Include a little context before the first anchor
    start = max(0, start - 200)
    return ocr_text[start : start + max_chars]


def extract_financials(ocr_text: str) -> dict:
    client   = _mistral_client()
    section  = _extract_financial_sections(ocr_text)
    prompt = (
        "Du er en norsk regnskapsekspert. Analyser følgende OCR-tekst fra et norsk årsregnskap "
        "og returner et JSON-objekt med disse feltene (tall i hele kroner som heltall uten punktum/mellomrom, "
        "null hvis ikke funnet). Norske tall bruker punktum som tusenskilletegn — fjern disse.\n\n"
        "RESULTATREGNSKAP:\n"
        "- salgsinntekter\n"
        "- driftsinntekter\n"
        "- varekostnad\n"
        "- lønnskostnad\n"
        "- avskrivninger\n"
        "- andre_driftskostnader\n"
        "- sum_driftskostnader\n"
        "- driftsresultat\n"
        "- finansinntekter\n"
        "- finanskostnader\n"
        "- resultat_for_skatt\n"
        "- skattekostnad\n"
        "- aarsresultat\n\n"
        "BALANSE — EIENDELER:\n"
        "- anleggsmidler\n"
        "- omlopsmidler\n"
        "- sum_eiendeler\n\n"
        "BALANSE — EGENKAPITAL OG GJELD:\n"
        "- innskutt_egenkapital\n"
        "- opptjent_egenkapital\n"
        "- sum_egenkapital\n"
        "- langsiktig_gjeld\n"
        "- kortsiktig_gjeld\n"
        "- sum_gjeld\n\n"
        "Returner KUN gyldig JSON, ingen forklaring.\n\n"
        f"Regnskapstekst:\n{section}"
    )
    resp = client.chat.complete(
        model="mistral-large-latest",
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
    )
    try:
        return json.loads(resp.choices[0].message.content)
    except Exception:
        return {}


# ── Page ─────────────────────────────────────────────────────────────────────

st.title("📄 Brreg Årsregnskap")
st.caption("Søk etter virksomhet og last ned årsregnskap-PDF-er fra Brønnøysundregistrene")

if "companies" not in st.session_state:
    st.session_state.companies = None

# ── 1. Search ─────────────────────────────────────────────────────────────────

with st.form("search_form"):
    query = st.text_input("Virksomhetsnavn eller org.nr.", placeholder="f.eks. Equinor, DNB, Aker… eller 123456789")
    submitted = st.form_submit_button("🔍  Søk", type="primary")

if submitted and query.strip():
    with st.spinner("Søker…"):
        try:
            q = query.strip()
            digits_only = q.replace(" ", "").replace("-", "")
            if digits_only.isdigit() and len(digits_only) == 9:
                st.session_state.companies = search_by_orgnr(digits_only)
            else:
                st.session_state.companies = search_companies(q)
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
        navn    = company["navn"]

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

            col_a, col_b = st.columns(2)

            # ── Single year ───────────────────────────────────────────────────

            year = col_a.selectbox("Velg år", sorted(years, reverse=True))

            if col_a.button("⬇  Hent PDF", use_container_width=True):
                with st.spinner(f"Laster ned {year}…"):
                    try:
                        data = fetch_pdf(orgnr, year)
                        col_a.download_button(
                            label=f"💾  Last ned {year}",
                            data=data,
                            file_name=f"aarsregnskap-{year}_{orgnr}.pdf",
                            mime="application/pdf",
                            use_container_width=True,
                        )
                    except Exception as e:
                        st.error(f"Nedlasting feilet: {e}")

            # ── All years as ZIP (parallel) ───────────────────────────────────

            col_b.markdown("**Alle år**")
            if col_b.button("⬇  Last ned alle (ZIP)", use_container_width=True, type="primary"):
                sorted_years = sorted(years)
                bar     = st.progress(0, text="Laster ned…")
                done    = 0
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
                    buf = io.BytesIO()
                    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
                        for yr in sorted(results):
                            zf.writestr(f"aarsregnskap-{yr}_{orgnr}.pdf", results[yr])

                    safe_navn = "".join(c for c in navn if c.isalnum() or c in " _-").strip()
                    st.download_button(
                        label=f"💾  Last ned {safe_navn} – alle år ({len(results)} PDF-er)",
                        data=buf.getvalue(),
                        file_name=f"aarsregnskap_{orgnr}_alle.zip",
                        mime="application/zip",
                        type="primary",
                        use_container_width=True,
                    )

                if errors:
                    st.warning("Noen år feilet: " + " | ".join(errors))

            # ── OCR + Excel extraction ────────────────────────────────────────

            st.divider()
            st.subheader("Ekstraher regnskapsdata til Excel")
            st.caption(
                "Bruker Mistral OCR til å lese PDF-ene og trekke ut nøkkeltall. "
                "Kan ta litt tid — ca. 15–30 sek per år."
            )

            if "MISTRAL_API_KEY" not in st.secrets:
                st.warning("Mistral API-nøkkel mangler. Legg til `MISTRAL_API_KEY` i Streamlit Secrets.")
            elif st.button("📊  Ekstraher og last ned Excel", use_container_width=True, type="primary"):
                sorted_years = sorted(years)
                bar  = st.progress(0, text="Starter…")
                rows = []
                errs = []

                for i, yr in enumerate(sorted_years, 1):
                    bar.progress((i - 1) / len(sorted_years), text=f"Behandler {yr} ({i}/{len(sorted_years)})…")
                    try:
                        pdf_bytes = fetch_pdf(orgnr, yr)
                        ocr_text  = ocr_pdf(pdf_bytes, filename=f"aarsregnskap-{yr}-{orgnr}.pdf")
                        data      = extract_financials(ocr_text)
                        rows.append({
                            "År":                    int(yr),
                            # Resultatregnskap
                            "Salgsinntekter":         data.get("salgsinntekter"),
                            "Driftsinntekter":        data.get("driftsinntekter"),
                            "Varekostnad":            data.get("varekostnad"),
                            "Lønnskostnad":           data.get("lønnskostnad"),
                            "Avskrivninger":          data.get("avskrivninger"),
                            "Andre driftskostnader":  data.get("andre_driftskostnader"),
                            "Sum driftskostnader":    data.get("sum_driftskostnader"),
                            "Driftsresultat":         data.get("driftsresultat"),
                            "Finansinntekter":        data.get("finansinntekter"),
                            "Finanskostnader":        data.get("finanskostnader"),
                            "Resultat før skatt":     data.get("resultat_for_skatt"),
                            "Skattekostnad":          data.get("skattekostnad"),
                            "Årsresultat":            data.get("aarsresultat"),
                            # Balanse — eiendeler
                            "Anleggsmidler":          data.get("anleggsmidler"),
                            "Omløpsmidler":           data.get("omlopsmidler"),
                            "Sum eiendeler":          data.get("sum_eiendeler"),
                            # Balanse — egenkapital og gjeld
                            "Innskutt egenkapital":   data.get("innskutt_egenkapital"),
                            "Opptjent egenkapital":   data.get("opptjent_egenkapital"),
                            "Sum egenkapital":        data.get("sum_egenkapital"),
                            "Langsiktig gjeld":       data.get("langsiktig_gjeld"),
                            "Kortsiktig gjeld":       data.get("kortsiktig_gjeld"),
                            "Sum gjeld":              data.get("sum_gjeld"),
                        })
                    except Exception as e:
                        errs.append(f"{yr}: {e}")

                bar.progress(1.0, text="Ferdig!")

                if rows:
                    df = pd.DataFrame(rows).sort_values("År").reset_index(drop=True)
                    excel_buf = io.BytesIO()
                    with pd.ExcelWriter(excel_buf, engine="openpyxl") as writer:
                        df.to_excel(writer, index=False, sheet_name="Årsregnskap")
                    safe_navn = "".join(c for c in navn if c.isalnum() or c in " _-").strip()
                    st.download_button(
                        label=f"💾  Last ned {safe_navn} – regnskapsdata.xlsx",
                        data=excel_buf.getvalue(),
                        file_name=f"regnskap_{orgnr}_{safe_navn}.xlsx",
                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        use_container_width=True,
                    )
                    st.dataframe(df, use_container_width=True)

                if errs:
                    st.warning("Noen år feilet: " + " | ".join(errs))

# ── Footer ──────────────���─────────────────────────────────────────────────────
st.divider()
st.caption("Data fra [Brønnøysundregistrene](https://www.brreg.no) · Åpen API")
