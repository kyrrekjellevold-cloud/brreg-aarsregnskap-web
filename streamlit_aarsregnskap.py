"""
Brreg Årsregnskap — Streamlit web app
======================================
Søk etter virksomhet og last ned årsregnskap-PDF-er direkte fra Brreg.

Deploy gratis på https://share.streamlit.io
"""

import base64
import io
import json
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed

import anthropic
import pandas as pd
import requests
import streamlit as st

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


# ── Claude PDF extraction ─────────────────────────────────────────────────────

def extract_financials_from_pdf(pdf_bytes: bytes) -> dict:
    client = anthropic.Anthropic(api_key=st.secrets["ANTHROPIC_API_KEY"])
    prompt = (
        "Du er en norsk regnskapsekspert. Les dette årsregnskapet og returner et JSON-objekt "
        "med disse feltene (tall i hele kroner som heltall uten punktum/mellomrom, "
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
        "Returner KUN gyldig JSON, ingen forklaring, ingen kodeblokk."
    )
    message = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "document",
                    "source": {
                        "type": "base64",
                        "media_type": "application/pdf",
                        "data": base64.standard_b64encode(pdf_bytes).decode("utf-8"),
                    },
                },
                {"type": "text", "text": prompt},
            ],
        }],
    )
    text = message.content[0].text.strip()
    # Strip markdown code fences if present
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    try:
        return json.loads(text)
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
                "Bruker Claude til å lese PDF-ene og trekke ut nøkkeltall. "
                "Alle år behandles parallelt — vanligvis ferdig på 10–20 sek totalt."
            )

            if "ANTHROPIC_API_KEY" not in st.secrets:
                st.warning("Anthropic API-nøkkel mangler. Legg til `ANTHROPIC_API_KEY` i Streamlit Secrets.")
            elif st.button("📊  Ekstraher og last ned Excel", use_container_width=True, type="primary"):
                sorted_years = sorted(years)
                bar     = st.progress(0, text="Starter…")
                done    = 0
                results = {}
                errs    = []

                def _fetch_and_extract(yr: str) -> tuple[str, dict]:
                    pdf_bytes = fetch_pdf(orgnr, yr)
                    return yr, extract_financials_from_pdf(pdf_bytes)

                with ThreadPoolExecutor(max_workers=len(sorted_years)) as pool:
                    futures = {pool.submit(_fetch_and_extract, yr): yr for yr in sorted_years}
                    for future in as_completed(futures):
                        yr = futures[future]
                        try:
                            _, data = future.result()
                            results[yr] = data
                        except Exception as e:
                            errs.append(f"{yr}: {e}")
                        done += 1
                        bar.progress(done / len(sorted_years), text=f"{done}/{len(sorted_years)} ferdig…")

                rows = []
                for yr in sorted_years:
                    if yr not in results:
                        continue
                    data = results[yr]
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
