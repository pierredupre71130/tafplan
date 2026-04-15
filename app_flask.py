"""
TAFPLAN — Planning des soins EHPAD
Version Flask (compatible réseau d'entreprise sans WebSocket)
"""

from flask import Flask, render_template, request, jsonify, send_file
from werkzeug.utils import secure_filename
import fitz
import json
import io
import re
from datetime import time
from groq import Groq
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
import os
import unicodedata
from collections import OrderedDict

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # 50MB max
app.config['UPLOAD_FOLDER'] = '/tmp'

# ═══════════════════════════════════════════════════════════════════════════
# CONSTANTES (copiées depuis app.py Streamlit)
# ═══════════════════════════════════════════════════════════════════════════

CARE_VERBS_PREFIX = [
    'SURVEILLANCE ', 'EVALUATION ', 'SOINS ', 'SOIN ',
    'SURV ', 'PST ', 'CHANGEMENT POCHE', 'POSE ATTELLE',
    'ASPIRATION ', 'SONDAGE ', 'PROTECTION ',
    'ABLATION ', 'CHANGEMENT ', 'KINE ', 'BILAN ',
    'LEVER ', 'HYDRATATION ', 'STIMULATION ', 'ENSEIGNANT ',
    'PRISE EN CHARGE ', 'BARRIERES ', 'SANGLE ', 'CONTENTIONS ',
    'COMPLEMENT ', 'RADIO', 'SCANNER', 'IRM', 'ECG', 'ECHO',
    'PRELEVEMENT ', 'PRISE DE SANG', 'ECBU', 'ANALYSE ',
]

CARE_KEYWORDS_CONTAINS = [
    'GLYCEMIE', 'DEXTRO', 'PANSEMENT', 'TENSION',
    'STOMIE', 'ESCARRE', 'OXYGENE', 'CONSTANTES', 'DIURESE',
    'PESEE', 'EXAMEN', 'BIOLOGIE', 'HEMOCULTURE', 'UROCULTURE',
    'COMPLEMENT', 'ALIMENTAIRE', 'FORTIMEL', 'CLINUTREN', 'FORTEOCARE', 'DESSERT',
]

CARE_ACT_BLACKLIST = [
    r'^AIDE A LA PRISE',
    r'^REFECTION PANSEMENT',
    r'^DISTRIBUTION',
    r'^ENSEIGNANT',
    r'^REGIME',
    r'^PROTECTION ',
]

CATEGORY_RULES = [
    ("Imagerie & ECG", ["RADIO", "SCANNER", "IRM", "ECG", "ECHO"]),
    ("Prélèvements & Biologie", ["PRELEVEMENT", "PRISE DE SANG", "ECBU", "BIOLOGIE",
                                  "HEMOCULTURE", "UROCULTURE", "BILAN COPRO", "BILAN SANGUIN",
                                  "BILAN BIOLOGIQUE"]),
    ("Collyre", ["COLLYRE", "OPHTALMIQUE", "YEUX", "OCULAIRE"]),
    ("Injection / SC", ["INJECTION", "VOIE SC", "SC ", "SANS SC", "SOUSTCUT", "SOUS CUTAN"]),
    ("Perfusion / IV", ["PERFUSION", "INTRAVEINEUX", "VOIE IV", "IV", "VEINEUSE"]),
    ("Surveillance", ["SURVEILLANCE", "SURV", "GLYCEMIE", "DEXTRO", "CONSTANTES", "TENSION", "OXYGENE", "DIURESE", "PESEE", "PESÉE"]),
    ("Psychologue", ["PSYCHOLOGUE", "BILAN PSYCHO"]),
    ("Évaluation", ["EVALUATION", "DOULEUR"]),
    ("Aide à la prise", ["AIDE A LA PRISE", "ACTE DE LA VIE COURANTE"]),
    ("Pose / Ablation", ["POSE ", "ABLATION", "CHANGEMENT", "ATTELLE", "CHAUSSETTES DE CONTENTION", "MATELAS"]),
    ("Soins locaux", ["PANSEMENT", "STOMIE", "ASPIRATION", "SONDAGE", "PROTECTION"]),
    ("Kinésithérapie", ["KINE", "KINÉ", "MOBILISATION", "MARCHE"]),
    ("Contentions", ["BANDES", "BAS", "CHAUSSETTES"]),
    ("Contentions physiques", ["SANGLE", "CONTENTION", "CONTENTIONS", "BARRIERES", "BARRIERE"]),
    ("Ergothérapie", ["ERGO", "ERGOTHÉRAPIE", "PRISE EN CHARGE ERGO"]),
    ("Lever", ["LEVER", "FAUTEUIL"]),
    ("Hydratation", ["HYDRATATION", "BOISSON", "STIMULATION"]),
    ("Enseignement", ["ENSEIGNANT APA", "ENSEIGNANT"]),
    ("Compléments alimentaires", ["COMPLEMENT", "FORTIMEL", "OPTIFIBRE", "PROTEINE", "NUTRITION", "DIETETIQUE"]),
    ("Traitements si besoin", ["TRAITEMENT SI BESOIN"]),
]

# ═══════════════════════════════════════════════════════════════════════════
# UTILITAIRES
# ═══════════════════════════════════════════════════════════════════════════

def _normalize(text: str) -> str:
    """Normaliser texte : majuscules + suppression accents"""
    text = (text or "").upper()
    return ''.join(
        c for c in unicodedata.normalize('NFD', text)
        if unicodedata.category(c) != 'Mn'
    )

def is_care_act(text: str) -> bool:
    u = text.upper().strip()
    if not u or len(u) < 5:
        return False
    u = _normalize(u)
    for pattern in CARE_ACT_BLACKLIST:
        if re.search(pattern, u):
            return False
    if any(_normalize(v) in u for v in CARE_VERBS_PREFIX):
        return True
    if any(_normalize(kw) in u for kw in CARE_KEYWORDS_CONTAINS):
        return True
    return False

def categorize_care_act(description: str) -> str:
    text = _normalize(description or "")
    for category, keywords in CATEGORY_RULES:
        if any(_normalize(kw) in text for kw in keywords):
            return category
    return "Autres actes"

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
    - Traitements si besoin
    """
    lower = block.lower()
    results = []

    # ── Collyre ──────────────────────────────────────────────────────────────
    if 'collyre' in lower and ('ophtalmique' in lower or 'goutte' in lower):
        times = _times_in_range(block, heure_debut, heure_fin)
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
        note_match = re.search(r'Note médecin\s*:\s*(.{3,40})', block, re.IGNORECASE)
        note = f" — {note_match.group(1).strip().lower()}" if note_match else ''
        desc = f"Instillation collyre ({drug_name}){note}"
        if times:
            for t in times:
                results.append({'resident': patient, 'room': room, 'heure': t, 'description': desc, 'category': 'Collyre'})
        else:
            results.append({'resident': patient, 'room': room, 'heure': None, 'description': desc, 'category': 'Collyre'})

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
                results.append({'resident': patient, 'room': room, 'heure': t, 'description': desc, 'category': 'Injection / SC'})
        elif si_besoin or not times:
            results.append({'resident': patient, 'room': room, 'heure': None, 'description': desc, 'category': 'Injection / SC'})

    # ── Perfusion IV ──────────────────────────────────────────────────────────
    if 'perfusion' in lower or 'voie iv' in lower or 'intraveineux' in lower \
            or 'voie veineuse' in lower:
        times = _times_in_range(block, heure_debut, heure_fin)
        lines = [l.strip() for l in block.split('\n') if l.strip()]
        drug_line = next(
            (l for l in lines if len(l) > 5 and not re.match(r'^\d', l)
             and l not in ('c', 'g', 'h', 'j')
             and not re.search(r'\b(gélule|comprimé|cp|capsule|sachet|ampoule|gel|sirop|pdr|cp séc|cp orodis)\b', l, re.I)), 'Perfusion IV'
        )
        desc = f"Perfusion IV — {drug_line[:40].rstrip('.,')}"
        if times:
            for t in times:
                results.append({'resident': patient, 'room': room, 'heure': t, 'description': desc, 'category': 'Perfusion / IV'})
        else:
            results.append({'resident': patient, 'room': room, 'heure': None, 'description': desc, 'category': 'Perfusion / IV'})

    # ── Traitements si besoin ─────────────────────────────────────────────────
    if 'si besoin' in lower and not any(kw in lower for kw in ['collyre', 'insuline', 'perfusion', 'voie iv', 'intraveineux', 'voie veineuse']):
        times = _times_in_range(block, heure_debut, heure_fin)
        lines = [l.strip() for l in block.split('\n') if l.strip()]
        drug_line = next(
            (l for l in lines if len(l) > 5 and not re.match(r'^\d', l)
             and l not in ('c', 'g', 'h', 'j') and 'si besoin' not in l.lower()), None
        )
        if drug_line:
            match = re.search(r'^(.+?)\s*\d+', drug_line.strip())
            drug_name = match.group(1).strip() if match else drug_line.strip()
            drug_name = drug_name.title()
            brand_match = re.search(r'\(([A-Z][A-Z0-9\s\-]+)\)', drug_name)
            if brand_match:
                drug_name = brand_match.group(1).strip().title()
        else:
            drug_name = 'Médicament'
        desc = f"Traitement si besoin — {drug_name}"
        # Vérifier si c'est une contention physique
        block_norm = _normalize(block)
        if any(kw in block_norm for kw in ['SANGLE', 'CONTENTION', 'BARRIERE', 'BARRIERES']):
            cat_si_besoin = 'Contentions physiques'
            desc = f"{drug_name} (si besoin)"
        else:
            cat_si_besoin = 'Traitements si besoin'
        if times:
            for t in times:
                results.append({'resident': patient, 'room': room, 'heure': t, 'description': desc, 'category': cat_si_besoin})
        else:
            results.append({'resident': patient, 'room': room, 'heure': None, 'description': desc, 'category': cat_si_besoin})

    return results


def extract_dietary_supplements(block: str, patient: str, room: str,
                                heure_debut: time, heure_fin: time) -> list:
    """Détecte les compléments alimentaires écrits directement comme noms de produits."""
    results = []
    lines = [l.strip() for l in block.split('\n') if l.strip()]

    DIETARY_PRODUCTS = [
        'FORTIMEL', 'CLINUTREN', 'OPTIFIBRE', 'RENUTRYL',
        'NUTRIDRINK', 'ENSURE', 'FRESUBIN', 'CUBITAN', 'DIASIP',
        'PROTEINE', 'FORTIFRESH', 'FORTEOCARE'
    ]

    for line in lines:
        # Ignorer la ligne "Note médecin" elle-même pour éviter de créer une prescription séparée
        if re.match(r'^\s*Note m[eé]decin\s*:', line, re.IGNORECASE):
            continue

        line_upper = line.upper()
        for product in DIETARY_PRODUCTS:
            if product in line_upper:
                times = _times_in_range(block, heure_debut, heure_fin)
                description = f"Complément alimentaire ({product.title()})"
                if 'DESSERT' in line_upper:
                    description = f"Complément alimentaire ({product.title()} Dessert)"
                elif 'PROTEIN' in line_upper or 'PROTEINE' in line_upper:
                    description = f"Complément alimentaire ({product.title()} Protéiné)"
                if times:
                    for t in times:
                        results.append({'resident': patient, 'room': room, 'heure': t, 'description': description, 'category': 'Compléments alimentaires'})
                else:
                    results.append({'resident': patient, 'room': room, 'heure': None, 'description': description, 'category': 'Compléments alimentaires'})
                break
    return results


def format_patient_name(raw: str) -> str:
    raw = raw.strip()
    match = re.search(r'([A-Z\s\-]+)\s*\(?(?:née|né)\s+([A-Z\s\-]+)\)?\s+([A-Z\s\-]+)', raw)
    if match:
        last_name, maiden_name, first_name = match.groups()
        last_name = last_name.strip().title()
        first_name = first_name.strip().title()
        return f"Mme/M. {first_name} {last_name}"
    return raw[:30].title()

def get_room_number(room: str) -> int:
    match = re.search(r'\d+', room or '0')
    return int(match.group()) if match else 0

def title_fr(text: str) -> str:
    return ' '.join(word.capitalize() for word in text.lower().split())

def extract_care_acts(pdf_bytes: bytes, heure_debut: time, heure_fin: time) -> list:
    """Extraction simplifée des actes de soins"""
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    results = []
    seen = set()

    for page_num in range(len(doc)):
        text = doc[page_num].get_text()
        if not text.strip():
            continue

        patient_match = re.search(r'Patient\s*:\s*(.+)', text)
        patient = format_patient_name(patient_match.group(1)) if patient_match else 'Résident inconnu'

        room_match = re.search(r'Chambre\s*:\s*(.+)', text)
        room = room_match.group(1).strip() if room_match else 'Inconnue'

        dates_debut = re.findall(r'Début le (\d{2}/\d{2}/\d{2,4}) à \d{2}:\d{2}', text)
        blocks = re.split(r'Début le \d{2}/\d{2}/\d{2,4} à \d{2}:\d{2}', text)

        for i, block in enumerate(blocks[1:]):
            date_debut = dates_debut[i] if i < len(dates_debut) else None
            # Extraction collyres, injections SC, perfusions IV, traitements si besoin
            for act in extract_medication_care_acts(block, patient, room, heure_debut, heure_fin):
                key = (act['resident'], act['description'][:50].upper(), act.get('heure'))
                if key not in seen:
                    seen.add(key)
                    act['date_debut'] = date_debut
                    results.append(act)

            # Extraction compléments alimentaires (hors Note médecin pour éviter doublon)
            for act in extract_dietary_supplements(block, patient, room, heure_debut, heure_fin):
                key = (act['resident'], act['description'][:50].upper(), act.get('heure'))
                if key not in seen:
                    seen.add(key)
                    act['date_debut'] = date_debut
                    results.append(act)

            # Extraction spéciale pour les barrières
            for line in block.split('\n'):
                line_norm = _normalize(line.strip())
                if 'BARRIERES MISES EN PLACE' in line_norm or 'BARRIERE MISES EN PLACE' in line_norm:
                    key = (patient, 'BARRIERES MISES EN PLACE', None)
                    if key not in seen:
                        seen.add(key)
                        results.append({
                            'resident': patient,
                            'room': room,
                            'heure': None,
                            'description': 'Barrières mises en place',
                            'category': 'Contentions physiques',
                            'date_debut': date_debut,
                        })
                    break

            # Filtre médicaments : exclure si c'est un bloc de médicament (dosages, formes galéniques)
            block_sans_note = re.split(r'Note m[eé]decin\s*:', block, flags=re.IGNORECASE)[0]
            check_zone = block_sans_note[:300]

            # Vérifier si c'est un complément alimentaire (ne pas filtrer comme médicament)
            is_dietary = (
                'COMPLEMENT' in check_zone.upper() or 'ALIMENTAIRE' in check_zone.upper() or
                any(prod in check_zone.upper() for prod in [
                    'FORTIMEL', 'OPTIFIBRE', 'CLINUTREN',
                    'RENUTRYL', 'NUTRIDRINK', 'ENSURE', 'FRESUBIN',
                    'CUBITAN', 'DIASIP', 'PROTEINE', 'FORTIFRESH', 'SUPPLEMENT',
                    'FORTEOCARE', 'DESSERT'
                ])
            )

            if not is_dietary:
                if re.search(r'\d+\s*(mg|mL|UI|µg|mcg|ug)\b', check_zone, re.I):
                    continue
                if re.search(
                    r'\b(comprimé|gélule|sachet|ampoule|cpr|gél|pdr|'
                    r'cp\s?séc|cp\s?orodis|buvable|sirop|patch|goutte)\b',
                    check_zone, re.I
                ):
                    continue

            # Extraction standard
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
                        break

            if not act_lines:
                # Fallback
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
                    if is_care_act(line):
                        act_lines = [line.rstrip('.,;')]
                        break

            if not act_lines:
                continue

            act_name = ' '.join(act_lines).strip()
            act_name = re.sub(r'\s+', ' ', act_name)

            if not is_care_act(act_name):
                continue

            description = title_fr(act_name)
            act_upper = act_name.upper()
            act_norm = _normalize(act_upper)

            # Normalisation de variantes de contentions physiques
            if 'BARRIERES MISES EN PLACE' in act_norm or 'BARRIERES AU LIT' in act_norm or 'BARRIERE AU LIT' in act_norm:
                description = 'Barrières au lit'
            elif 'CONTENTIONS FAUTEUIL' in act_norm:
                description = 'Contentions fauteuil'
            elif 'SANGLE VENTRALE' in act_norm:
                description = 'Sangle ventrale'

            # Détection de compléments alimentaires dans le nom de l'acte
            PRODUITS = ['FORTIMEL', 'CLINUTREN', 'OPTIFIBRE', 'RENUTRYL',
                        'NUTRIDRINK', 'ENSURE', 'FRESUBIN', 'CUBITAN', 'DIASIP',
                        'PROTEINE', 'FORTIFRESH', 'FORTEOCARE',
                        'SUPPLEMENT', 'ALIMENTAIRE']
            is_complement = 'COMPLEMENT' in act_upper
            if not is_complement:
                for prod in PRODUITS:
                    if prod in act_upper:
                        is_complement = True
                        break

            if is_complement:
                # Chercher le nom du produit connu dans le bloc (pas la Note médecin)
                block_upper = block.upper()
                product_found = None
                for prod in PRODUITS:
                    if prod in block_upper:
                        product_found = prod
                        break
                if product_found:
                    label = product_found.title()
                    if 'DESSERT' in block_upper:
                        label += ' Dessert'
                    elif product_found == 'PROTEINE' and 'PROTEIN' in block_upper:
                        label += ' Protéiné'
                    description = f"Complément alimentaire ({label})"
                else:
                    description = 'Complément alimentaire'

            category = categorize_care_act(act_name)

            times_in_block = re.findall(r'(\d{2}:\d{2})', block)
            times_in_range = []
            for t_str in times_in_block:
                try:
                    h, m = map(int, t_str.split(':'))
                    t = time(h, m)
                    if heure_debut <= t <= heure_fin:
                        times_in_range.append(t_str)
                except (ValueError, AttributeError):
                    pass

            # Clé de dédoublonnage : description pour les compléments (pour matcher extract_dietary_supplements)
            act_key = re.sub(r'\s+', ' ', description[:50].upper() if is_complement else act_name[:50].upper())

            if times_in_range:
                for t_str in times_in_range:
                    key = (patient, act_key, t_str)
                    if key not in seen:
                        seen.add(key)
                        results.append({
                            'resident': patient,
                            'room': room,
                            'heure': t_str,
                            'description': description,
                            'category': category,
                            'date_debut': date_debut,
                        })
            elif not times_in_block:
                key = (patient, act_key, None)
                if key not in seen:
                    seen.add(key)
                    results.append({
                        'resident': patient,
                        'room': room,
                        'heure': None,
                        'description': description,
                        'category': category,
                        'date_debut': date_debut,
                    })

    doc.close()
    return results

def sort_soins(soins: list) -> list:
    """Trier par heure, puis par résident"""
    def sort_key(s):
        heure = s.get('heure')
        if heure is None:
            return (1, '', s.get('resident', ''))
        try:
            h, m = map(int, heure.split(':'))
            return (0, h * 60 + m, s.get('resident', ''))
        except:
            return (1, '', s.get('resident', ''))
    return sorted(soins, key=sort_key)

def filter_soins(soins: list, categories: list, query: str) -> list:
    q = _normalize((query or "").strip())
    filtered = []
    for soin in soins:
        if categories and soin.get("category") not in categories:
            continue
        if q:
            searchable = _normalize(
                f"{soin.get('resident','')} {soin.get('room','')} {soin.get('description','')} {soin.get('category','')}"
            )
            if q not in searchable:
                continue
        filtered.append(soin)
    return filtered

def format_heure(heure_str) -> str:
    if not heure_str or heure_str == '—':
        return '—'
    return str(heure_str).replace(':', 'h')

def generate_pdf(soins: list, heure_debut: str, heure_fin: str, date_str: str) -> bytes:
    """Générer PDF d'export"""
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

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "TitleStyle", parent=styles["Title"],
        textColor=orange, fontSize=22, spaceAfter=4, fontName="Helvetica-Bold",
    )

    elements = []
    elements.append(Paragraph("TAFPLAN — Planning des soins", title_style))
    elements.append(Spacer(1, 0.5 * cm))
    elements.append(Paragraph(f"<b>Tranche horaire :</b> {heure_debut} — {heure_fin}", styles["Normal"]))
    elements.append(Paragraph(f"<b>Généré le :</b> {date_str}", styles["Normal"]))
    elements.append(Spacer(1, 1 * cm))

    # Tableau
    data = [["Heure", "Résident", "Chambre", "Acte"]]
    for s in soins:
        data.append([
            format_heure(s.get('heure')),
            s.get('resident', ''),
            s.get('room', ''),
            s.get('description', ''),
        ])

    table = Table(data)
    table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), orange),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
        ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, 0), 11),
        ('BOTTOMPADDING', (0, 0), (-1, 0), 12),
        ('BACKGROUND', (0, 1), (-1, -1), light_orange),
        ('GRID', (0, 0), (-1, -1), 1, colors.black),
    ]))

    elements.append(table)
    doc.build(elements)
    buffer.seek(0)
    return buffer.getvalue()

# ═══════════════════════════════════════════════════════════════════════════
# ROUTES FLASK
# ═══════════════════════════════════════════════════════════════════════════

@app.route('/')
def index():
    categories = sorted([cat for cat, _ in CATEGORY_RULES])
    return render_template('index.html', categories=categories)

@app.route('/api/analyze', methods=['POST'])
def analyze():
    """Endpoint d'analyse du PDF"""
    try:
        if 'file' not in request.files:
            return jsonify({'error': 'Pas de fichier'}), 400

        file = request.files['file']
        if file.filename == '':
            return jsonify({'error': 'Fichier vide'}), 400

        pdf_bytes = file.read()

        # Récupérer paramètres
        heure_debut_str = request.form.get('heure_debut', '14:00')
        heure_fin_str = request.form.get('heure_fin', '20:00')
        categories_selected = request.form.getlist('categories[]')

        h_d, m_d = map(int, heure_debut_str.split(':'))
        h_f, m_f = map(int, heure_fin_str.split(':'))
        heure_debut = time(h_d, m_d)
        heure_fin = time(h_f, m_f)

        # Extraction — renvoie TOUS les soins, le filtrage par catégorie se fait côté client
        soins = extract_care_acts(pdf_bytes, heure_debut, heure_fin)
        soins = sort_soins(soins)

        return jsonify({
            'success': True,
            'soins': soins,
            'heure_debut': heure_debut_str,
            'heure_fin': heure_fin_str,
        })

    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/export-pdf', methods=['POST'])
def export_pdf():
    """Export PDF"""
    try:
        data = request.json
        soins = data.get('soins', [])
        heure_debut = data.get('heure_debut', '')
        heure_fin = data.get('heure_fin', '')

        pdf_bytes = generate_pdf(soins, heure_debut, heure_fin,
                                 __import__('datetime').datetime.now().strftime("%d/%m/%Y à %Hh%M"))

        return send_file(
            io.BytesIO(pdf_bytes),
            mimetype='application/pdf',
            as_attachment=True,
            download_name=f'planning_soins_{heure_debut.replace(":", "h")}_{heure_fin.replace(":", "h")}.pdf'
        )
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ═══════════════════════════════════════════════════════════════════════════

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(debug=False, host='0.0.0.0', port=port)
