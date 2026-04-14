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
# Détection des actes de soins infirmiers (actes en MAJUSCULES dans le PDF)
# ---------------------------------------------------------------------------

# Préfixes d'actes infirmiers à détecter
CARE_VERBS_PREFIX = [
    'SURVEILLANCE ', 'EVALUATION ', 'SOINS ', 'SOIN ',
    'SURV ', 'PST ', 'CHANGEMENT POCHE', 'POSE ATTELLE',
    'ASPIRATION ', 'SONDAGE ', 'PROTECTION ',
    'ABLATION ', 'CHANGEMENT ', 'KINE ', 'BILAN ',
    'LEVER ', 'HYDRATATION ', 'STIMULATION ', 'ENSEIGNANT ',
    'PRISE EN CHARGE ', 'BARRIERES ', 'SANGLE ', 'CONTENTIONS ',
    'COMPLEMENT ',
]

# Mots-clés présents dans la ligne → acte infirmier
CARE_KEYWORDS_CONTAINS = [
    'GLYCEMIE', 'DEXTRO', 'PANSEMENT', 'TENSION',
    'STOMIE', 'ESCARRE', 'OXYGENE', 'CONSTANTES', 'DIURESE',
    'PESEE',
]

# Patterns à exclure (actes non souhaités)
CARE_ACT_BLACKLIST = [
    r'^AIDE A LA PRISE',       # Aide à la prise de médicaments
    r'^REFECTION PANSEMENT',   # Réfection pansement
    r'^DISTRIBUTION',          # Distribution médicaments
    r'^ENSEIGNANT',            # Enseignant APA
    r'^OPTIFIBRE',
    r'^REGIME',
    r'^PROTECTION ',
]


def is_care_act(text: str) -> bool:
    u = text.upper().strip()
    if not u or len(u) < 5:
        return False
    # Vérifier la blacklist en premier
    for pattern in CARE_ACT_BLACKLIST:
        if re.search(pattern, u):
            return False
    if any(u.startswith(v) for v in CARE_VERBS_PREFIX):
        return True
    if any(kw in u for kw in CARE_KEYWORDS_CONTAINS):
        return True
    return False


def format_patient_name(raw: str) -> str:
    """Formate le nom du patient depuis le format PDF vers un format lisible."""
    raw = raw.strip()
    # Format PDF : "NOM [MULTI] (née/né PRENOM_JEUNE) PRENOM"
    # Exemples : "NYZAK (née DIEU) HENRIETTE"
    #            "DE SOUSA MAGALHAES (né DE SOUSA MAGALHAE) CARLOS"
    #            "DUPONT JEAN" (pas de parenthèses)

    # Avec parenthèses et prénom après
    m_f = re.search(r'^(.+?)\s*\(née?[^)]*\)\s+(\S.+)$', raw, re.IGNORECASE)
    m_m = re.search(r'^(.+?)\s*\(né\s[^)]*\)\s+(\S.+)$', raw, re.IGNORECASE)

    if m_f and 'née' in raw.lower():
        last = m_f.group(1).strip().title()
        first = m_f.group(2).strip().title()
        return f'Mme {first} {last}'
    if m_m:
        last = m_m.group(1).strip().title()
        first = m_m.group(2).strip().title()
        return f'M. {first} {last}'

    # Parenthèses sans prénom après (nom tronqué dans le PDF)
    m_trunc = re.search(r'^(.+?)\s*\(n[ée]', raw, re.IGNORECASE)
    if m_trunc:
        last = m_trunc.group(1).strip().title()
        # Détecter le genre
        civil = 'Mme' if 'née' in raw.lower() else 'M.'
        return f'{civil} {last}'

    # Pas de parenthèses : dernier mot = prénom, reste = nom
    words = raw.split()
    if len(words) >= 2:
        first = words[-1].title()
        last = ' '.join(words[:-1]).title()
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
# Extraction spéciale : collyres et injections SC (écrits comme médicaments)
# ---------------------------------------------------------------------------

def _times_in_range(block: str, heure_debut: time, heure_fin: time) -> list:
    """Retourne les heures HH:MM du bloc qui tombent dans la tranche."""
    result = []
    for t_str in re.findall(r'(\d{2}:\d{2})', block):
        try:
            h, m = map(int, t_str.split(':'))
            if heure_debut <= time(h, m) <= heure_fin:
                result.append(t_str)
        except ValueError:
            pass
    return result


def extract_medication_care_acts(block: str, patient: str, room: str,
                                  heure_debut: time, heure_fin: time) -> list:
    """
    Détecte dans un bloc de médicament les actes infirmiers implicites :
    - Instillation collyre (voie ophtalmique)
    - Injection SC insuline
    - Perfusion IV
    """
    lower = block.lower()
    results = []

    # ── Collyre ──────────────────────────────────────────────────────────────
    if 'collyre' in lower and ('ophtalmique' in lower or 'goutte' in lower):
        times = _times_in_range(block, heure_debut, heure_fin)
        # Nom du médicament : (MARQUE) en parenthèses, ou nom sur la ligne avec "collyre"
        brand = re.search(r'\(([A-Z][A-Z0-9\s\-]+)\)', block)
        if brand:
            drug_name = brand.group(1).strip().title()
        else:
            drug_name = 'collyre'
            for line in block.split('\n'):
                if 'collyre' in line.lower() and len(line.strip()) > 7:
                    before = re.split(r'\bcollyre\b', line, flags=re.IGNORECASE)[0]
                    before = before.strip().rstrip(' ,')
                    before = re.sub(r'\s*\d[\d\s%/\.]*$', '', before).strip()
                    if before:
                        drug_name = before.title()
                        break
        # Note yeux (ex : "2 YEUX", "œil droit")
        note_match = re.search(r'Note médecin\s*:\s*(.{3,40})', block, re.IGNORECASE)
        note = f" — {note_match.group(1).strip().lower()}" if note_match else ''
        desc = f"Instillation collyre ({drug_name}){note}"

        if times:
            for t in times:
                results.append({'resident': patient, 'room': room, 'heure': t, 'description': desc})
        else:
            results.append({'resident': patient, 'room': room, 'heure': None, 'description': desc})

    # ── Injection SC insuline ─────────────────────────────────────────────────
    if ('voie sc' in lower or ', voie sc' in lower or 'sc,' in lower
            or 'sous-cut' in lower or 'sous cutan' in lower) and 'insuline' in lower:
        times = _times_in_range(block, heure_debut, heure_fin)
        is_lente = 'lente' in lower or 'glargine' in lower or 'toujeo' in lower \
                   or 'abasaglar' in lower or 'lantus' in lower or 'tresiba' in lower
        is_rapide = 'rapide' in lower or 'asparte' in lower or 'novorapid' in lower \
                    or 'humalog' in lower or 'apidra' in lower
        si_besoin = 'si besoin' in lower or 'selon prot' in lower

        if is_lente:
            ins_type = 'Injection insuline lente SC'
        elif is_rapide:
            ins_type = 'Injection insuline rapide SC'
        else:
            ins_type = 'Injection insuline SC'

        if si_besoin:
            ins_type += ' (si besoin)'

        dose_match = re.search(r'(\d+)\s*unité', lower)
        dose = f" {dose_match.group(1)} UI" if dose_match else ''
        desc = f"{ins_type}{dose}"

        if times:
            for t in times:
                results.append({'resident': patient, 'room': room, 'heure': t, 'description': desc})
        elif si_besoin or not times:
            # Insuline "si besoin" ou sans heure : inclure sans heure
            results.append({'resident': patient, 'room': room, 'heure': None, 'description': desc})

    # ── Perfusion IV ──────────────────────────────────────────────────────────
    if 'perfusion' in lower or 'voie iv' in lower or 'intraveineux' in lower \
            or 'voie veineuse' in lower:
        times = _times_in_range(block, heure_debut, heure_fin)
        # Extraire le nom du médicament perfusé
        lines = [l.strip() for l in block.split('\n') if l.strip()]
        drug_line = next(
            (l for l in lines if len(l) > 5 and not re.match(r'^\d', l)
             and l not in ('c', 'g', 'h', 'j')
             and not re.search(r'\b(gélule|comprimé|cp|capsule|sachet|ampoule|gel|sirop|pdr|cp séc|cp orodis)\b', l, re.I)), 'Perfusion IV'
        )
        desc = f"Perfusion IV — {drug_line[:40].rstrip('.,')}"
        if times:
            for t in times:
                results.append({'resident': patient, 'room': room, 'heure': t, 'description': desc})
        else:
            results.append({'resident': patient, 'room': room, 'heure': None, 'description': desc})

    # ── Traitements si besoin ─────────────────────────────────────────────────
    if 'si besoin' in lower and not any(keyword in lower for keyword in ['collyre', 'insuline', 'perfusion', 'voie iv', 'intraveineux', 'voie veineuse']):
        times = _times_in_range(block, heure_debut, heure_fin)
        # Extraire le nom du médicament : première ligne appropriée
        lines = [l.strip() for l in block.split('\n') if l.strip()]
        drug_line = next(
            (l for l in lines if len(l) > 5 and not re.match(r'^\d', l) and l not in ('c', 'g', 'h', 'j') and 'si besoin' not in l.lower()), None
        )
        if drug_line:
            # Essayer d'extraire le nom avant le dosage
            match = re.search(r'^(.+?)\s*\d+', drug_line.strip())
            if match:
                drug_name = match.group(1).strip()
            else:
                drug_name = drug_line.strip()
            drug_name = drug_name.title()
            # Si marque entre parenthèses, l'utiliser
            brand_match = re.search(r'\(([A-Z][A-Z0-9\s\-]+)\)', drug_name)
            if brand_match:
                drug_name = brand_match.group(1).strip().title()
        else:
            drug_name = 'Médicament'
        desc = f"Traitement si besoin — {drug_name}"
        if times:
            for t in times:
                results.append({'resident': patient, 'room': room, 'heure': t, 'description': desc})
        else:
            results.append({'resident': patient, 'room': room, 'heure': None, 'description': desc})

    return results


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

        # Chambre du patient
        room_match = re.search(r'Chambre\s*:\s*(.+)', text)
        room = room_match.group(1).strip() if room_match else 'Inconnue'

        # Découper en blocs de prescription (chaque bloc commence par "Début le")
        blocks = re.split(r'Début le \d{2}/\d{2}/\d{2,4} à \d{2}:\d{2}', text)

        for block in blocks[1:]:
            # ── Extraction spéciale AVANT le filtre médicament ───────────────
            # Collyres et injections SC : écrits comme médicaments mais = actes
            for act in extract_medication_care_acts(block, patient, room, heure_debut, heure_fin):
                key = (act['resident'], act['description'][:50].upper(), act.get('heure'))
                if key not in seen:
                    seen.add(key)
                    results.append(act)

            # ── Ignorer les blocs de médicaments ─────────────────────────────
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
                            'room': room,
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
                        'room': room,
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


def get_room_number(room_str: str) -> int:
    match = re.search(r'(\d+)', room_str)
    return int(match.group(1)) if match else 0

def sort_soins(soins: list) -> list:
    def sort_key(s):
        room_num = get_room_number(s.get('room', '0'))
        h = s.get('heure')
        if not h or h == '—':
            h_time = time(23, 59)
        else:
            try:
                parts = str(h).split(':')
                h_time = time(int(parts[0]), int(parts[1]))
            except Exception:
                h_time = time(23, 59)
        return (room_num, h_time)
    return sorted(soins, key=sort_key)


CATEGORY_RULES = [
    ("Collyre", ["COLLYRE", "OPHTALMIQUE", "YEUX", "OCULAIRE"]),
    ("Injection / SC", ["INJECTION", "VOIE SC", "SC ", "SANS SC", "SOUSTCUT", "SOUS CUTAN"]),
    ("Perfusion / IV", ["PERFUSION", "INTRAVEINEUX", "VOIE IV", "IV", "VEINEUSE"]),
    ("Surveillance", ["SURVEILLANCE", "SURV", "GLYCEMIE", "DEXTRO", "CONSTANTES", "TENSION", "OXYGENE", "DIURESE", "PESEE", "PESÉE"]),
    ("Évaluation", ["EVALUATION", "BILAN", "DOULEUR"]),
    ("Aide à la prise", ["AIDE A LA PRISE", "ACTE DE LA VIE COURANTE"]),
    ("Pose / Ablation", ["POSE ", "ABLATION", "CHANGEMENT", "ATTELLE", "CHAUSSETTES DE CONTENTION", "SANGLE", "MATELAS"]),
    ("Soins locaux", ["PANSEMENT", "STOMIE", "ASPIRATION", "SONDAGE", "PROTECTION"]),
    ("Kinésithérapie", ["KINE", "KINÉ", "MOBILISATION", "MARCHE"]),
    ("Contentions", ["BANDES", "BAS", "CHAUSSETTES"]),
    ("Contentions physiques", ["SANGLE", "CONTENTION", "CONTENTIONS", "BARRIERES", "BARRIERE"]),
    ("Ergothérapie", ["ERGO", "ERGOTHÉRAPIE", "PRISE EN CHARGE ERGO"]),
    ("Psychologue", ["PSYCHOLOGUE", "BILAN PSYCHO"]),
    ("Lever", ["LEVER", "FAUTEUIL"]),
    ("Hydratation", ["HYDRATATION", "BOISSON", "STIMULATION"]),
    ("Enseignement", ["ENSEIGNANT", "APA"]),
    ("Compléments alimentaires", ["COMPLEMENT", "FORTIMEL", "CALCIDOSE", "OPTIFIBRE", "PROTEINE", "NUTRITION", "DIETETIQUE"]),
    ("Traitements si besoin", ["TRAITEMENT SI BESOIN"]),
]


def categorize_care_act(description: str) -> str:
    text = (description or "").upper()
    for category, keywords in CATEGORY_RULES:
        if any(keyword in text for keyword in keywords):
            return category
    return "Autres actes"


def assign_care_categories(soins: list) -> list:
    for soin in soins:
        soin["category"] = categorize_care_act(soin.get("description", ""))
    return soins


def filter_soins(soins: list, categories: list, query: str, floor_filter: str) -> list:
    q = (query or "").strip().lower()
    filtered = []
    for soin in soins:
        if categories and soin.get("category") not in categories:
            continue
        if floor_filter == "RDC (1-99)":
            if get_room_number(soin.get('room', '0')) > 99:
                continue
        elif floor_filter == "1er étage (100+)":
            if get_room_number(soin.get('room', '0')) < 100:
                continue
        if q:
            searchable = (
                f"{soin.get('resident','')} {soin.get('room','')} {soin.get('description','')} {soin.get('category','')}"
            ).lower()
            if q not in searchable:
                continue
        filtered.append(soin)
    return filtered


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
        room = s.get('room') or 'Inconnue'
        category = s.get('category') or 'Non renseignée'
        description = s.get('description') or ''
        rows += f"""
        <tr>
            <td class="heure-cell">{heure_display}</td>
            <td>{room}</td>
            <td class="resident-cell">{resident}</td>
            <td>{category}</td>
            <td>{description}</td>
        </tr>"""

    table_html = f"""
    <table class="care-table">
        <thead>
            <tr>
                <th>Heure</th>
                <th>Chambre</th>
                <th>Résident(e)</th>
                <th>Catégorie</th>
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

    header = ["Heure", "Chambre", "Résident(e)", "Catégorie", "Acte de soin"]
    table_data = [header]
    for s in soins:
        table_data.append([
            format_heure(s.get('heure')),
            s.get('room') or 'Inconnue',
            s.get('resident') or 'Non identifié',
            s.get('category') or 'Non renseignée',
            s.get('description') or '',
        ])

    col_widths = [2 * cm, 2.5 * cm, 4 * cm, 3 * cm, 6 * cm]
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

        soins = assign_care_categories(soins)
        soins = sort_soins(soins)
        categories = sorted({s.get("category", "Autres actes") for s in soins})

        st.session_state["soins_results"] = soins
        st.session_state["category_filter"] = categories
        st.session_state["search_query"] = ""
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
        categories = sorted({s.get("category", "Autres actes") for s in soins})

        if "category_filter" not in st.session_state or not st.session_state["category_filter"]:
            st.session_state["category_filter"] = categories

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

        with st.expander("🔎 Recherche et filtres", expanded=True):
            search_query = st.text_input(
                "Recherche mots-clés",
                value=st.session_state.get("search_query", ""),
                key="search_query",
                placeholder="Ex. douleur, perfusion, Mme Dupont",
                help="Filtrer les soins par résident, acte ou catégorie.",
            )
            st.markdown("**Catégories à afficher**")
            if st.button("Restaurer le filtre de base"):
                st.session_state["category_filter"] = categories
            # Système de checkboxes pour les catégories
            selected_categories = []
            cols = st.columns(3)  # 3 colonnes pour organiser les checkboxes
            for i, cat in enumerate(categories):
                col = cols[i % 3]
                default_checked = cat in st.session_state.get("category_filter", categories)
                if col.checkbox(cat, value=default_checked, key=f"cat_{cat}"):
                    selected_categories.append(cat)
            st.session_state["category_filter"] = selected_categories  # Mettre à jour l'état de session
            st.caption(
                "Cochez/décochez les catégories à afficher. Le bouton 'Restaurer le filtre de base' "
                "sélectionne toutes les catégories."
            )
            floor_filter = st.selectbox(
                "Filtrer par étage",
                options=["Tous", "RDC (1-99)", "1er étage (100+)"],
                index=0,
                help="Filtrer les résultats par étage. Les chambres 1-99 sont au RDC, 100+ au 1er étage."
            )

        filtered_soins = filter_soins(soins, selected_categories, st.session_state.get("search_query", ""), floor_filter)
        st.markdown(f"**Résultats filtrés : {len(filtered_soins)} / {len(soins)} soin(s)**")
        render_soins_table(filtered_soins)

        if filtered_soins:
            st.divider()
            date_str = datetime.now().strftime("%d/%m/%Y à %Hh%M")
            with st.spinner("Génération du PDF…"):
                pdf_bytes_export = generate_pdf(
                    filtered_soins,
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
