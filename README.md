# CSP - Cutting Solution Platform

Een multi-tenant SaaS-platform voor zaagplan optimalisatie met MKG ERP integratie.

## Functionaliteiten

- Multi-tenant: elke gebruiker heeft eigen data en omgeving
- Gebruikersaccounts (registreren, inloggen, JWT-authenticatie)
- Per-gebruiker MKG ERP koppeling (credentials versleuteld opgeslagen)
- Unieke webhook-URL per gebruiker/omgeving
- Zaagplan optimalisatie (First Fit Decreasing)
- REST API + Web interface
- SQLite (lokaal) / PostgreSQL (productie)

## Lokale installatie

1. Clone repository en navigeer naar directory
2. python -m venv .venv ; .venv\Scripts\activate
3. pip install -r requirements.txt
4. copy .env.example .env  (vul SECRET_KEY in via python generate_secret_key.py)
5. python main.py

Draait op http://localhost:8000

## Deployment op Railway

1. Push naar GitHub
2. Nieuw project op railway.app, koppel GitHub repo
3. Voeg PostgreSQL plugin toe (DATABASE_URL wordt automatisch ingesteld)
4. Stel in via Railway dashboard: SECRET_KEY, DEBUG=False, APP_BASE_URL
5. Railway start automatisch via Procfile

## API Endpoints

- POST /api/auth/register
- POST /api/auth/login
- GET/PUT /api/auth/environment
- GET /api/auth/webhook-info
- POST /api/webhook/mkg/{webhook_token}
- GET /api/orders
- GET /api/cutting-plans

## Web Interface

- / Dashboard
- /cutting-plans Overzicht
- /settings MKG omgevingsinstellingen

## Licentie

Proprietary - Alle rechten voorbehouden
