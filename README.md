# ThirtyStash

<p align="center">
<img width="128" height="128" alt="ThirtyStash-logo" src="https://github.com/user-attachments/assets/b1bb6e5a-63f6-451c-b41f-87ac5420bae4" />
</p>

ThirtyStash is a local-first, self-hosted preparedness inventory for tracking a
household's **30-day food and water reserve** plus medical supplies.

AI DISCLAIMER
--

This was made with ChatGPT. I get that some people cringe at the thought of that, but this works and it works well from my testing. 

> **Public beta:** `1.2.0-beta.1`

ThirtyStash uses Flask, SQLite, Docker Compose, and no
external database or message broker to stay as small and light as possible.

<img width="1245" height="846" alt="image" src="https://github.com/user-attachments/assets/9899fc66-47cb-40be-8eb1-4d15b3479cc8" />


## Features

- Household onboarding with additional household members.
- TDEE-based 30-day calorie target using Mifflin-St Jeor + activity level, or a manual calorie target.
- Imperial and metric entry: lb/kg, in/cm, gal/L, and common food mass units.
- Food inventory with UPC/EAN scanning and Open Food Facts lookup.
- Food calories calculated from total food mass, serving mass, and calories per serving.
- Independent food lots/batches with storage date, expiration date, and inspections.
- Water target of **3 L per person per day**, with treatment and inspection tracking.
- Medical inventory organized around MARCH categories plus OTC, prescription, and equipment types.
- Attention dashboard for overdue inspections, expired items, and upcoming expirations.
- Search, filtering, sorting, CSV export, and mobile-friendly inventory views.
- Integrity-checked full SQLite backups, manual save, optional once-daily scheduled backups, restore, and safe reset.
- Local system-status page for database and backup diagnostics.
- Runs on port **3055**.

## Quick start

Requirements:

- Docker Engine
- Docker Compose v2 (`docker compose`)

Clone the repository and start it:

```bash
git clone https://github.com/SixtyAteWhiskey/ThirtyStash
cd ThirtyStash
docker compose up -d --build
```

Open:

```text
http://YOUR-SERVER-IP:3055
```

Check container health:

```bash
docker compose ps
```

Application health endpoint:

```text
http://YOUR-SERVER-IP:3055/healthz
```

The first visit opens household onboarding.

## Updating

Back up ThirtyStash first, then:

```bash
docker compose down
git pull
docker compose up -d --build
```

Do **not** use `docker compose down -v` during a normal update. `-v` removes the
persistent Docker volume containing the live SQLite database and generated
installation secret.

## Data and backups

The live SQLite database is stored in the named Docker volume
`thirtystash_data`.

Saved and scheduled backups are written beneath `/backups` in the containers.
By default that is mapped to `./backups` on the host.

To use another host path, create `.env` beside `docker-compose.yml`:

```text
THIRTYSTASH_BACKUP_DIR=/mnt/nas/ThirtyStash
```

Then recreate the services:

```bash
docker compose up -d --build
```

The folder selected in the ThirtyStash UI is always a subfolder beneath that
configured backup root. ThirtyStash does not get arbitrary host filesystem
access.

A full backup ZIP contains:

```text
thirtystash.db
manifest.json
RESTORE.txt
```

ThirtyStash validates SQLite integrity before creating a backup and before/after
restoring one. Restore and reset also create pre-action safety backups.

## Barcode scanning

ThirtyStash uses **Quagga2 1.12.1** for UPC/EAN-focused barcode scanning.
The pinned browser bundle is downloaded during Docker image build and then
served locally; barcode decoding does not depend on a CDN at runtime.

Two scanning paths are available:

1. **Photo scanning** — works over normal HTTP and is the most reliable mobile fallback.
2. **Live camera scanning** — requires HTTPS (or `localhost`) because browsers restrict `getUserMedia()` to secure contexts.

Manual barcode entry always remains available.

### Open Food Facts

Open Food Facts product **read** requests do not require an API key. ThirtyStash
performs product lookups server-side and identifies itself with `OFF_USER_AGENT`.
Nutrition/package data can be incomplete, so users should confirm the displayed
mass, serving size, and calories before saving an item.

## Security

ThirtyStash currently has **no authentication**.

Use it only on a trusted LAN, over a private VPN, or behind a reverse proxy that
provides authentication. **Do not directly expose port 3055 to the public Internet.**

Public-beta hardening includes:

- CSRF protection on state-changing web requests.
- A unique Flask secret generated automatically per installation and persisted in the data volume.
- No shared default credentials or application secrets in Compose.
- Backup destination path confinement.
- CSV formula-injection protection.
- Restore archive/database validation and SQLite integrity checks.

See [SECURITY.md](SECURITY.md).

## Medical disclaimer

ThirtyStash is an inventory tool, not medical advice or medical training.
Its MARCH/TCCC-inspired categories are general organizational references only.
Users are responsible for following current official guidance, product
instructions, applicable law/policy, and their own training and authorization.

Advanced or invasive interventions should only be acquired, stored, or used as
appropriate for the user's training, authorization, and circumstances.

## Development

Python 3.12 is the reference development version.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
python scripts/fetch_vendor.py
python app.py
```

Run checks:

```bash
python -m compileall -q app.py backup_scheduler.py scripts tests
pytest -q
docker compose config
docker compose build
```

GitHub Actions runs Python checks/tests and launches the Docker Compose stack to verify the `/healthz` endpoint on pushes and pull requests.

## Project status

`1.2.0-beta.1` is the first repository-hardened public beta. The current focus is
single-household reliability and straightforward self-hosting rather than
multi-user features.

See [CHANGELOG.md](CHANGELOG.md) for release history.

## License

ThirtyStash is available under the [MIT License](LICENSE).
Third-party notices are in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
