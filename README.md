# Zibris VCD Inventory

Web-UI zum Auslesen von **vApps** und **VMs** aus VMware Cloud Director (Zibris Cloud).

Login mit Org-User → Übersicht mit CPU, RAM, HDD → Filter nach VDC → Export als **Excel** oder **CSV**.

Läuft ausschließlich als **Docker-Container**.

## Voraussetzungen

- [Docker](https://docs.docker.com/get-docker/)
- [Docker Compose](https://docs.docker.com/compose/)
- Zugang zu VMware Cloud Director (Benutzer, Organisation, Passwort)

## Schnellstart

```bash
git clone https://github.com/nycon/zibris-inventory-tool.git
cd zibris-inventory-tool
docker compose up -d --build
```

UI öffnen: [http://127.0.0.1:8080](http://127.0.0.1:8080)

Stoppen:

```bash
docker compose down
```

Logs:

```bash
docker compose logs -f
```

## Login

Im Browser:

| Feld | Beispiel |
|------|----------|
| Portal URL | `https://portal.zibris-cloud.ch` |
| Benutzer | `username` |
| Organisation | `org name` |
| Passwort | dein Passwort |

Die API meldet sich als `benutzer@organisation` an (Basic Auth gegen `/cloudapi/1.0.0/sessions`).

## Funktionen

- Liste aller **vApps** mit Status, VDC, CPU, RAM, HDD
- Aufklappen → zugehörige **VMs** (OS, Netz, Ressourcen)
- Suche und Filter (Status, VDC)
- Kennzahlen: vApps, VMs, CPU, RAM, Storage (Total in TiB ab 1024 GiB)
- **Excel / CSV Export**
  - Filter **Alle VDCs** → gesamtes Inventory
  - Filter **ein VDC** → nur vApps/VMs dieses VDCs

## Konfiguration

Umgebungsvariablen in `docker-compose.yml`:

| Variable | Default | Beschreibung |
|----------|---------|--------------|
| `VCD_BASE_URL` | `https://portal.zibris-cloud.ch` | Cloud-Director-Portal |
| `VCD_API_VERSION` | `39.1` | API-Version im `Accept`-Header |
| `TZ` | `Europe/Zurich` | Zeitzone für den Zeitstempel „Stand“ |
| `SESSION_SECRET` | `zibris-local-dev-secret-change-on-server` | Geheimnis für Session-Cookies – **auf dem Server ändern** |
| `SESSION_HTTPS_ONLY` | `false` | Auf `true` setzen, wenn die App hinter HTTPS läuft |
| `PORT` | `8080` | Port im Container (Mapping in Compose: `8080:8080`) |

Beispiel für einen Server mit HTTPS hinter Reverse Proxy:

```yaml
environment:
  SESSION_SECRET: "langes-zufaelliges-geheimnis"
  SESSION_HTTPS_ONLY: "true"
```

## Architektur

```
Browser  →  FastAPI (Docker)  →  VMware Cloud Director API
                 │
                 ├─ Login: POST /cloudapi/1.0.0/sessions
                 └─ Daten:  GET  /api/query (vApp / vm)
```

Passwörter werden nicht dauerhaft gespeichert. Nach dem Login liegt das Inventory im Server-Speicher (Session); nach Container-Neustart ist ein erneuter Login nötig.

## Projektstruktur

```
zibris-inventory-tool/
├── app/
│   ├── main.py           # FastAPI-App, Login, Export
│   ├── vcd_client.py     # Cloud-Director-Client
│   ├── templates/        # HTML
│   └── static/           # CSS / JS
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

## Hinweise

- Der User muss in Cloud Director **aktiviert** sein.
- Templates werden bei VMs ausgefiltert (`isVAppTemplate==false`).
- Healthcheck: `GET /healthz`

## Lizenz

Intern / privat – nach Bedarf anpassen.
