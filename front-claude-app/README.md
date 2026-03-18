# Front + Claude — Assistant Email

Application web qui connecte Front (frontapp) avec Claude pour rédiger des brouillons de réponse email assistés par IA.

## Fonctionnalités

1. **Connexion Front API** — Charge vos conversations et messages
2. **Sélection de conversation** — Choisissez la conversation à traiter
3. **Chat avec Claude** — Échangez avec Claude pour rédiger la réponse idéale
4. **Envoi du brouillon** — Le brouillon validé est poussé dans Front

## Installation

```bash
cd front-claude-app
pip install -r requirements.txt
cp .env.example .env
# Éditez .env avec vos tokens
```

## Configuration

Renseignez dans `.env` :

- `FRONT_API_TOKEN` — Votre token API Front (Settings > Developers > API tokens)
- `ANTHROPIC_API_KEY` — Votre clé API Anthropic
- `FLASK_SECRET_KEY` — Une chaîne aléatoire pour les sessions Flask

## Lancement

```bash
python app.py
```

Puis ouvrez http://localhost:5000

## Flux d'utilisation

1. Sélectionnez une boîte de réception (optionnel) et chargez les conversations
2. Cliquez sur une conversation pour voir ses messages
3. Donnez vos instructions à Claude (ton, contenu, points à aborder)
4. Itérez avec Claude jusqu'à obtenir le brouillon souhaité
5. Cliquez "Utiliser comme brouillon" puis validez
6. Le brouillon est créé dans Front, prêt à être envoyé
