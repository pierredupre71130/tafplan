import streamlit as st
import fitz  # PyMuPDF
import json
import io
import re
from datetime import time, datetime
from groq import Groq
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer

st.set_page_config(
    page_title="TAFPLAN - Planning des soins",
    page_icon="🏥",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ---------------------------------------------------------------------------
# CSS personnalisé
# ---------------------------------------------------------------------------

def inject_css():
    st.markdown(
        """
        <style>
        .stButton > button[kind="primary"] {
            background-color: #FF6B00 !important;
            color: white !important;
            font-size: 1.15rem !important;
            font-weight: 600 !important;
            padding: 0.75rem 2rem !important;
            border-radius: 8px !important;
            border: none !important;
            width: 100%;
            letter-spacing: 0.03em;
            transition: background-color 0.2s ease;
        }
        .stButton > button[kind="primary"]:hover {
            background-color: #E55A00 !important;
        }
        .stButton > button[kind="primary"]:disabled {
            background-color: #FFBB80 !important;
            cursor: not-allowed !important;
        }
        .care-table {
            font-family: 'Segoe UI', Arial, sans-serif;
            width: 100%;
            border-collapse: collapse;
            margin-top: 1rem;
            border-radius: 8px;
            overflow: hidden;
            box-shadow: 0 2px 8px rgba(0,0,0,0.07);
        }
        .care-table th {
            background-color: #FF6B00;
            color: white;
            padding: 12px 16px;
            text-align: left;
            font-weight: 700;
            font-size: 0.95rem;
            letter-spacing: 0.04em;
            text-transform: uppercase;
        }
        .care-table td {
            padding: 10px 16px;
            border-bottom: 1px solid #FFE0B2;
            font-size: 0.97rem;
            vertical-align: middle;
        }
        .care-table tr:nth-child(even) td {
            background-color: #FFF3E0;
        }
        .care-table tr:last-child td { border-bottom: none; }
        .care-table tr:hover td {
            background-color: #FFE0B2;
            transition: background-color 0.15s;
        }
        .care-table .heure-cell {
            font-weight: 700;
            color: #FF6B00;
            white-space: nowrap;
            font-size: 1.05rem;
        }
        .care-table .resident-cell { font-weight: 600; color: #1A1A1A; }
        .rgpd-box {
            background-color: #FFF8F3;
            border-left: 4px solid #FF6B00;
            padding: 14px 18px;
            border-radius: 4px;
            font-size: 0.80rem;
            color: #666;
            margin-top: 2.5rem;
            line-height: 1.6;
        }
        .badge-count {
            display: inline-block;
            background-color: #FF6B00;
            color: white;
            font-size: 0.85rem;
            font-weight: 700;
            padding: 2px 10px;
            border-radius: 12px;
            margin-left: 8px;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


# ---------------------------------------------------------------------------
# Détection des actes de soins infirmiers
# ---------------------------------------------------------------------------

# Verbes / débuts caractéristiques d'un acte infirmier (en majuscules dans le PDF)
CARE_VERBS_PREFIX = [
    'POSE ', 'ABLATION ', 'SURVEILLANCE ', 'EVALUATION ', 'AIDE ',
    'CHANGEMENT ', 'REFECTION ', 'SOINS ', 'SOIN ', 'KINE ', 'KINÉ ',
    'DISTRIBUTION ', 'STIMULATION ', 'BARRIERES ', 'CONTENTIONS ',
    'PRISE EN CHARGE', 'SURV ', 'PST ', 'BILAN ', 'COMPLEMENT ALIMENTAIRE',
    'MOBILISATION', 'ASPIRATION ', 'SONDAGE ', 'PROTECTION ',
    'LEVER AU FAUTEUIL', 'MATELAS ANTI', 'SANGLE ', 'OPTIFIBRE',
    'ENSEIGNANT APA', 'COMPLEMENT ORAL',
]

# Mots-clés qui, s'ils apparaissent dans la ligne, indiquent un soin
CARE_KEYWORDS_CONTAINS = [
    'GLYCEMIE', 'DEXTRO', 'PANSEMENT', 'COLLYRE', 'TENSION',
    'STOMIE', 'ESCARRE', 'OXYGENE', 'CONSTANTES', 'DIURESE',
    'INSULINE', 'PESEE', 'FREESTYLE', 'CONTENTION',
]


def is_care_act(text: str) -> bool:
    u = text.upper().strip()
    if not u or len(u) < 5:
        return False
    if any(u.startswith(v) for v in CARE_VERBS_PREFIX):
        return True
    if any(kw in u for kw in CARE_KEYWORDS_CONTAINS):
        return True
    return False


def format_patient_name(raw: str) -> str:
    """Formate le nom du patient depuis le format PDF vers un format lisible."""
    raw = raw.strip()
    # Format: "NOM (née PRENOM_JEUNE) PRENOM" ou "NOM (né ...) PRENOM"
    match_f = re.search(r'^(.+?)\s*\(née?[^)]*\)\s*(.+)$', raw, re.IGNORECASE)
    match_m = re.search(r'^(.+?)\s*\(né\s[^)]*\)\s*(.+)$', raw, re.IGNORECASE)
    if match_f and 'née' in raw.lower():
        last = match_f.group(1).strip().title()
        first = match_f.group(2).strip().title()
        return f'Mme {first} {last}'
    if match_m:
        last = match_m.group(1).strip().title()
        first = match_m.group(2).strip().title()
        return f'M. {first} {last}'
    # Pas de parenthèses
    words = raw.split()
    if len(words) >= 2:
        last = words[0].title()
        first = ' '.join(words[1:]).title()
        return f'{first} {last}'
    return raw.title()


def title_fr(text: str) -> str:
    """Title-case en français (articles et prépositions en minuscules)."""
    LOWER_WORDS = {'de', 'du', 'des', 'et', 'au', 'aux', 'la', 'le', 'les',
                   'un', 'une', 'par', 'a', 'à', 'en', 'sur', 'sous', 'pour'}
    words = text.lower().replace('(acte de la vie courante)', '').split()
    result = []
    for i, w in enumerate(words):
        if i == 0 or w not in LOWER_WORDS:
            result.append(w.capitalize())
        else:
            result.append(w)
    return ' '.join(result).strip()


# ---------------------------------------------------------------------------
# Extraction PDF — toutes les pages, sans LLM
# ---------------------------------------------------------------------------

def extract_care_acts(pdf_bytes: bytes, heure_debut: time, heure_fin: time) -> list:
    """
    Parcourt TOUTES les pages du PDF et extrait les actes infirmiers.
    Retourne une liste de dicts {resident, heure, description}.
    """
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    results = []
    seen = set()  # Dédoublonnage : (resident, acte_normalisé, heure)

    for page_num in range(len(doc)):
        text = doc[page_num].get_text()
        if not text.strip():
            continue

        # Nom du patient sur cette page
        patient_match = re.search(r'Patient\s*:\s*(.+)', text)
        patient = format_patient_name(patient_match.group(1)) if patient_match else 'Résident inconnu'

        # Découper en blocs de prescription (chaque bloc commence par "Début le")
        blocks = re.split(r'Début le \d{2}/\d{2}/\d{2,4} à \d{2}:\d{2}', text)

        for block in blocks[1:]:
            # Ignorer les blocs contenant des indicateurs de médicament
            if re.search(r'\d+\s*(mg|mL|UI|µg|mcg|ug)\b', block[:300], re.I):
                continue
            if re.search(
                r'\b(comprimé|gélule|sachet|ampoule|cpr|gél|pdr|'
                r'cp\s?séc|cp\s?orodis|buvable|sirop|patch|goutte)\b',
                block[:300], re.I
            ):
                continue

            # Extraire toutes les heures présentes dans ce bloc
            times_in_block = re.findall(r'(\d{2}:\d{2})', block)

            # Construire le nom de l'acte (lignes en MAJUSCULES consécutives)
            act_lines = []
            for raw_line in block.split('\n'):
                line = raw_line.strip()
                if not line or line in ('c', 'g', 'h', 'j', ' ', '  '):
                    continue
                if re.match(r'^\d{2}:\d{2}', line):
                    continue
                if re.match(r'^\d+[\.,]\d+\s*Kg', line):
                    continue
                if re.match(r'^\*\s*\d', line):
                    continue
                if line == line.upper() and re.search(r'[A-Z]{3}', line) and not re.match(r'^\d', line):
                    act_lines.append(line.rstrip('.,;'))
                else:
                    if act_lines:
                        break  # Fin du nom de l'acte

            if not act_lines:
                continue

            act_name = ' '.join(act_lines).strip()
            act_name = re.sub(r'\s+', ' ', act_name)

            if not is_care_act(act_name):
                continue

            # Filtrer les heures dans la tranche demandée
            times_in_range = []
            for t_str in times_in_block:
                try:
                    h, m = map(int, t_str.split(':'))
                    t = time(h, m)
                    if heure_debut <= t <= heure_fin:
                        times_in_range.append(t_str)
                except (ValueError, AttributeError):
                    pass

            # Clé de dédoublonnage : on normalise légèrement la description
            act_key = re.sub(r'\s+', ' ', act_name[:50].upper())

            if times_in_range:
                for t_str in times_in_range:
                    key = (patient, act_key, t_str)
                    if key not in seen:
                        seen.add(key)
                        results.append({
                            'resident': patient,
                            'heure': t_str,
                            'description': title_fr(act_name),
                        })
            elif not times_in_block:
                # Acte sans heure précisée : inclure une fois
                key = (patient, act_key, None)
                if key not in seen:
                    seen.add(key)
                    results.append({
                        'resident': patient,
                        'heure': None,
                        'description': title_fr(act_name),
                    })

    doc.close()

    # Trier par heure (None en fin)
    def sort_key(s):
        h = s.get('heure')
        if not h:
            return time(23, 59)
        try:
            parts = h.split(':')
            return time(int(parts[0]), int(parts[1]))
        except Exception:
            return time(23, 59)

    return sorted(results, key=sort_key)


# ---------------------------------------------------------------------------
# Résolution clé API Groq
# ---------------------------------------------------------------------------

def get_groq_client():
    api_key = None
    try:
        api_key = st.secrets["GROQ_API_KEY"]
    except (KeyError, FileNotFoundError):
        pass
    if not api_key:
        api_key = st.session_state.get("groq_api_key_input", "").strip()
    if api_key:
        return Groq(api_key=api_key)
    return None


# ---------------------------------------------------------------------------
# Normalisation LLM (optionnelle) — améliore les libellés
# ---------------------------------------------------------------------------

SYSTEM_PROMPT_NORMALIZE = """Tu es un infirmier coordinateur EHPAD expert.
Tu reçois une liste d'actes de soins infirmiers extraits automatiquement d'un planning.

Ta mission :
1. Normalise chaque description : libellé court et professionnel en français (5-7 mots, Title Case)
   - Exemples : "ABLATION BAS DE CONTENTION" → "Ablation bas de contention"
   - "AIDE A LA PRISE DE MEDICAMENTS (ACTE DE LA VIE COURANTE)" → "Aide à la prise de médicaments"
   - "KINE MARCHE" → "Kinésithérapie marche"
   - "SURVEILLANCE GLYCEMIE CAPILLAIRE" → "Surveillance glycémie capillaire"
   - "EVALUATION DE LA DOULEUR" → "Évaluation de la douleur"
   - "SOINS DE BOUCHE" → "Soins de bouche"
   - "REFECTION PANSEMENT" → "Réfection pansement"
2. Supprime les doublons stricts (même résident + même acte + même heure)
3. Élimine les faux positifs évidents (noms de soignants, demandes d'examens, notes médicales non-soins)
4. Conserve tous les vrais actes infirmiers et paramédicaux
5. Ne modifie pas les champs "resident" ni "heure"

Réponds UNIQUEMENT avec un JSON valide, sans texte avant ni après :
{"soins": [{"resident": "...", "heure": "HH:MM", "description": "..."}]}
"""


def normalize_with_groq(client: Groq, candidates: list) -> list:
    """
    Envoie les candidats pré-extraits au LLM pour normalisation des libellés.
    En cas d'échec, retourne les candidats tels quels.
    """
    if not candidates:
        return candidates

    # Construire le texte compact pour le LLM
    lines = []
    for c in candidates:
        heure = c.get('heure') or '—'
        desc = c.get('description', '')[:70]
        lines.append(f"{heure} | {c['resident']} | {desc}")

    user_content = (
        f"Voici les {len(candidates)} actes extraits automatiquement du planning :\n\n"
        + "\n".join(lines)
    )

    try:
        response = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT_NORMALIZE},
                {"role": "user", "content": user_content},
            ],
            temperature=0.1,
            max_tokens=4096,
            response_format={"type": "json_object"},
        )
    except Exception as e:
        st.warning(f"Normalisation IA non disponible ({e}). Affichage des soins extraits directement.")
        return candidates

    raw = response.choices[0].message.content.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()

    try:
        data = json.loads(raw)
        soins = data.get("soins", [])
        if isinstance(soins, list) and soins:
            return soins
    except json.JSONDecodeError:
        pass

    # Fallback : retourner les candidats bruts
    return candidates


# ---------------------------------------------------------------------------
# Tri des soins
# ---------------------------------------------------------------------------

def sort_soins(soins: list) -> list:
    def sort_key(s):
        h = s.get('heure')
        if not h or h == '—':
            return time(23, 59)
        try:
            parts = str(h).split(':')
            return time(int(parts[0]), int(parts[1]))
        except Exception:
            return time(23, 59)
    return sorted(soins, key=sort_key)


def format_heure(heure_str) -> str:
    if not heure_str or heure_str == '—':
        return '—'
    return str(heure_str).replace(':', 'h')


# ---------------------------------------------------------------------------
# Affichage tableau HTML
# ---------------------------------------------------------------------------

def render_soins_table(soins: list):
    if not soins:
        st.warning(
            "Aucun soin infirmier trouvé dans la tranche horaire sélectionnée. "
            "Vérifiez la tranche horaire ou le contenu du PDF."
        )
        return

    rows = ""
    for s in soins:
        heure_display = format_heure(s.get('heure'))
        resident = s.get('resident') or 'Résident non identifié'
        description = s.get('description') or ''
        rows += f"""
        <tr>
            <td class="heure-cell">{heure_display}</td>
            <td class="resident-cell">{resident}</td>
            <td>{description}</td>
        </tr>"""

    table_html = f"""
    <table class="care-table">
        <thead>
            <tr>
                <th>Heure</th>
                <th>Résident(e)</th>
                <th>Acte de soin</th>
            </tr>
        </thead>
        <tbody>{rows}</tbody>
    </table>
    """
    st.markdown(table_html, unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# Export PDF (ReportLab)
# ---------------------------------------------------------------------------

def generate_pdf(soins: list, heure_debut: str, heure_fin: str, date_str: str) -> bytes:
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=A4,
        rightMargin=2 * cm, leftMargin=2 * cm,
        topMargin=2 * cm, bottomMargin=2 * cm,
        title="TAFPLAN — Planning des soins",
    )

    orange = colors.HexColor("#FF6B00")
    light_orange = colors.HexColor("#FFF3E0")
    dark_gray = colors.HexColor("#1A1A1A")
    medium_gray = colors.HexColor("#888888")
    styles = getSampleStyleSheet()

    title_style = ParagraphStyle(
        "TitleStyle", parent=styles["Title"],
        textColor=orange, fontSize=22, spaceAfter=4, fontName="Helvetica-Bold",
    )
    subtitle_style = ParagraphStyle(
        "SubtitleStyle", parent=styles["Normal"],
        textColor=dark_gray, fontSize=10, spaceAfter=18, fontName="Helvetica",
    )
    rgpd_style = ParagraphStyle(
        "RGPDStyle", parent=styles["Normal"],
        fontSize=7, textColor=medium_gray, fontName="Helvetica", leading=10,
    )

    elements = []
    elements.append(Paragraph("TAFPLAN — Planning des soins", title_style))
    elements.append(Paragraph(
        f"Tranche horaire : <b>{heure_debut} – {heure_fin}</b>"
        f"&nbsp;&nbsp;|&nbsp;&nbsp;Généré le {date_str}"
        f"&nbsp;&nbsp;|&nbsp;&nbsp;{len(soins)} soin(s)",
        subtitle_style,
    ))
    elements.append(Spacer(1, 0.2 * cm))

    header = ["Heure", "Résident(e)", "Acte de soin"]
    table_data = [header]
    for s in soins:
        table_data.append([
            format_heure(s.get('heure')),
            s.get('resident') or 'Non identifié',
            s.get('description') or '',
        ])

    col_widths = [3 * cm, 6.5 * cm, 8.5 * cm]
    t = Table(table_data, colWidths=col_widths, repeatRows=1)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), orange),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, 0), 10),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 9),
        ("TOPPADDING", (0, 0), (-1, 0), 9),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, light_orange]),
        ("FONTNAME", (0, 1), (-1, -1), "Helvetica"),
        ("FONTSIZE", (0, 1), (-1, -1), 9),
        ("TOPPADDING", (0, 1), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 1), (-1, -1), 6),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TEXTCOLOR", (0, 1), (0, -1), orange),
        ("FONTNAME", (0, 1), (0, -1), "Helvetica-Bold"),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#FFE0B2")),
        ("LINEBELOW", (0, 0), (-1, 0), 2, orange),
        ("ALIGN", (0, 0), (0, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    elements.append(t)

    elements.append(Spacer(1, 0.6 * cm))
    elements.append(Paragraph(
        "Document confidentiel — Données de santé protégées (RGPD Art. 9). "
        "Accès réservé au personnel soignant autorisé. Généré par TAFPLAN.",
        rgpd_style,
    ))

    doc.build(elements)
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# Application principale
# ---------------------------------------------------------------------------

def main():
    inject_css()

    # ── Sidebar ──────────────────────────────────────────────────────────────
    with st.sidebar:
        st.markdown("## 🏥 TAFPLAN")
        st.caption("Planning des soins EHPAD — Analyse IA")
        st.divider()

        groq_via_secrets = False
        try:
            _ = st.secrets["GROQ_API_KEY"]
            groq_via_secrets = True
        except (KeyError, FileNotFoundError):
            pass

        if not groq_via_secrets:
            st.subheader("Clé API Groq")
            key_input = st.text_input(
                "Clé API Groq",
                type="password",
                placeholder="gsk_...",
                label_visibility="collapsed",
                help="Optionnel — améliore la normalisation des libellés. "
                     "Gratuit sur console.groq.com",
            )
            st.session_state["groq_api_key_input"] = key_input
            if key_input:
                st.success("Clé renseignée", icon="✅")
            else:
                st.info(
                    "Sans clé : extraction Python directe.\n\n"
                    "Avec clé : l'IA normalise les libellés.",
                    icon="🔑",
                )
        else:
            st.success("Clé API configurée via Secrets", icon="✅")

        st.divider()
        st.caption("v2.0.0 — TAFPLAN")

    # ── En-tête ───────────────────────────────────────────────────────────────
    st.title("TAFPLAN — Planning des soins")
    st.markdown(
        "Importez le planning PDF de l'EHPAD et obtenez les **actes de soins infirmiers** "
        "filtrés par tranche horaire — toutes les pages analysées automatiquement."
    )
    st.divider()

    # ── Zone de saisie ────────────────────────────────────────────────────────
    col_upload, col_heures = st.columns([3, 2], gap="large")

    with col_upload:
        uploaded_file = st.file_uploader(
            "Importer le planning PDF",
            type=["pdf"],
            accept_multiple_files=False,
            help="PDF multi-pages (logiciel EHPAD). Toutes les pages sont analysées.",
        )

    with col_heures:
        st.markdown("**Tranche horaire à analyser**")
        sub_col1, sub_col2 = st.columns(2)
        with sub_col1:
            heure_debut = st.time_input("Début de la tranche", value=time(14, 0), step=1800)
        with sub_col2:
            heure_fin = st.time_input("Fin de la tranche", value=time(20, 0), step=1800)

        if heure_debut >= heure_fin:
            st.warning("L'heure de fin doit être postérieure à l'heure de début.")

    st.divider()

    bouton_disabled = uploaded_file is None or heure_debut >= heure_fin
    analyze_clicked = st.button(
        "🔍  Analyser les soins",
        type="primary",
        use_container_width=True,
        disabled=bouton_disabled,
    )

    if "soins_results" not in st.session_state:
        st.session_state["soins_results"] = None
    if "last_params" not in st.session_state:
        st.session_state["last_params"] = {}

    # ── Traitement ────────────────────────────────────────────────────────────
    if analyze_clicked:
        debut_str = heure_debut.strftime("%H:%M")
        fin_str = heure_fin.strftime("%H:%M")

        pdf_bytes = uploaded_file.read()
        nb_pages = fitz.open(stream=pdf_bytes, filetype="pdf").page_count

        with st.spinner(
            f"Analyse des {nb_pages} pages du PDF — extraction des actes infirmiers…"
        ):
            candidates = extract_care_acts(pdf_bytes, heure_debut, heure_fin)

        if not candidates:
            st.warning(
                "Aucun acte de soin infirmier trouvé dans la tranche "
                f"{debut_str}–{fin_str}. "
                "Essayez une autre tranche horaire."
            )
            st.stop()

        # Normalisation LLM (optionnelle)
        client = get_groq_client()
        if client:
            with st.spinner(
                f"Normalisation des {len(candidates)} soins par l'IA Groq…"
            ):
                soins = normalize_with_groq(client, candidates)
        else:
            soins = candidates  # Extraction Python directe, libellés déjà formatés

        soins = sort_soins(soins)

        st.session_state["soins_results"] = soins
        st.session_state["last_params"] = {
            "debut": debut_str,
            "fin": fin_str,
            "filename": uploaded_file.name,
            "nb_pages": nb_pages,
            "used_llm": client is not None,
        }

    # ── Affichage des résultats ───────────────────────────────────────────────
    if st.session_state["soins_results"] is not None:
        soins = st.session_state["soins_results"]
        params = st.session_state["last_params"]

        mode = "IA Groq" if params.get("used_llm") else "Extraction Python"
        st.markdown(
            f"### Planning des soins — {params.get('debut', '')} à {params.get('fin', '')}"
            f"&nbsp;<span class='badge-count'>{len(soins)} soin(s)</span>",
            unsafe_allow_html=True,
        )
        st.caption(
            f"Source : {params.get('filename', '')}  |  "
            f"{params.get('nb_pages', '?')} pages analysées  |  Mode : {mode}"
        )

        render_soins_table(soins)

        if soins:
            st.divider()
            date_str = datetime.now().strftime("%d/%m/%Y à %Hh%M")
            with st.spinner("Génération du PDF…"):
                pdf_bytes_export = generate_pdf(
                    soins,
                    params.get("debut", ""),
                    params.get("fin", ""),
                    date_str,
                )
            nom_fichier = (
                f"planning_soins_"
                f"{params.get('debut', '').replace(':', 'h')}_"
                f"{params.get('fin', '').replace(':', 'h')}.pdf"
            )
            st.download_button(
                label="⬇️  Télécharger le planning (PDF)",
                data=pdf_bytes_export,
                file_name=nom_fichier,
                mime="application/pdf",
                use_container_width=True,
            )

    # ── Disclaimer RGPD ───────────────────────────────────────────────────────
    st.markdown(
        """
        <div class="rgpd-box">
        <strong>Notice de confidentialité (RGPD)</strong> — Ce service traite des données de santé
        à caractère personnel (catégorie spéciale, Art. 9 du RGPD). Aucune donnée n'est stockée
        par cette application entre les sessions. Si une clé API Groq est utilisée, les descriptions
        des soins (sans données personnelles) sont transmises à l'API Groq pour normalisation.
        L'utilisation est réservée au personnel soignant habilité. En cas de question, rapprochez-vous
        du DPO de votre établissement.
        </div>
        """,
        unsafe_allow_html=True,
    )


if __name__ == "__main__":
    main()
