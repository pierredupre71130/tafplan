# TAFPLAN — Planning des soins EHPAD

Application Streamlit d'analyse de planning de soins par IA (Groq).  
Extrait automatiquement les **actes de soins infirmiers** d'un PDF de planning EHPAD, filtre par tranche horaire et génère un planning exportable en PDF.

---

## Fonctionnalités

- Import d'un PDF de planning EHPAD
- Sélection d'une tranche horaire (ex. 14h00 – 20h00)
- Extraction des actes infirmiers uniquement (hors médicaments) via IA
- Affichage sous forme de tableau clair, trié par heure
- Export du planning en PDF
- Interface entièrement en français
- Thème orange, adapté aux professionnels de santé

---

## Déploiement sur Streamlit Community Cloud (1 clic, gratuit)

### Étape 1 — Clé API Groq gratuite

1. Aller sur **https://console.groq.com**
2. Créer un compte (gratuit, sans carte bancaire)
3. Cliquer sur **"API Keys"** → **"Create API Key"**
4. Copier la clé (commence par `gsk_`)

> Limites du tier gratuit : ~14 400 tokens/min, 500 requêtes/jour — largement suffisant pour un usage EHPAD quotidien.

---

### Étape 2 — Déployer sur Streamlit Cloud

1. Aller sur **https://share.streamlit.io**
2. Se connecter avec GitHub
3. Cliquer **"New app"**
4. Renseigner :
   - **Repository** : `pierredupre71130/tafplan`
   - **Branch** : `main`
   - **Main file path** : `app.py`
5. Cliquer **"Advanced settings"** → onglet **"Secrets"**
6. Coller exactement :
   ```toml
   GROQ_API_KEY = "gsk_votre_cle_ici"
   ```
7. Cliquer **"Deploy"**

L'application est disponible en ligne en moins de 2 minutes, avec une URL publique permanente.

---

### Utilisation en local

```bash
# 1. Cloner le dépôt
git clone https://github.com/pierredupre71130/tafplan.git
cd tafplan

# 2. Installer les dépendances
pip install -r requirements.txt

# 3. Configurer la clé API
cp .streamlit/secrets.toml.example .streamlit/secrets.toml
# Éditer .streamlit/secrets.toml et renseigner votre clé Groq

# 4. Lancer l'application
streamlit run app.py
```

> **Important** : Ne jamais committer `.streamlit/secrets.toml` sur GitHub (déjà dans `.gitignore`).

---

## Architecture

```
tafplan/
├── app.py                          # Application Streamlit principale
├── requirements.txt                # Dépendances Python
├── .gitignore
├── .streamlit/
│   ├── config.toml                 # Thème orange Streamlit
│   └── secrets.toml.example        # Modèle de configuration secrets
└── README.md
```

**Dépendances clés :**
- `streamlit` — Interface web
- `pdfplumber` — Extraction texte PDF
- `groq` — Client API Groq (LLM llama-3.3-70b-versatile)
- `reportlab` — Génération du planning PDF export

---

## Sécurité & RGPD

- Les données PDF sont traitées en mémoire uniquement, sans persistance entre sessions
- La clé API Groq est stockée dans les secrets Streamlit (chiffrés), jamais dans le code
- Les données transmises à l'API Groq sont soumises à leur [politique de confidentialité](https://groq.com/privacy-policy/)
- Usage réservé au personnel soignant habilité

---

## Licence

Usage interne EHPAD. Tous droits réservés.
