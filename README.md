# Zibris VCD Inventory

Web UI to list **vApps** and **VMs** from VMware Cloud Director (Zibris Cloud).

Sign in with an org user → overview with CPU, RAM, HDD → filter by VDC → export as **Excel** or **CSV**.

Runs exclusively as a **Docker container**.

The user interface is in **German**.

## Requirements

- [Docker](https://docs.docker.com/get-docker/)
- [Docker Compose](https://docs.docker.com/compose/)
- Access to VMware Cloud Director (username, organization, password)

## Quick start

```bash
git clone https://github.com/nycon/zibris-inventory-tool.git
cd zibris-inventory-tool
docker compose up -d --build
```

Open the UI: [http://127.0.0.1:8080](http://127.0.0.1:8080)

Stop:

```bash
docker compose down
```

Logs:

```bash
docker compose logs -f
```

## Login

In the browser:

| Field | Example |
|-------|---------|
| Portal URL | `https://portal.zibris-cloud.ch` |
| Username | `username` |
| Organization | `org name` |
| Password | your password |

The API authenticates as `username@organization` (Basic Auth against `/cloudapi/1.0.0/sessions`).

## Features

- List all **vApps** with status, VDC, CPU, RAM, HDD
- Expand → related **VMs** (OS, network, resources)
- Search and filters (status, VDC)
- Totals: vApps, VMs, CPU, RAM, storage (total in TiB from 1024 GiB)
- **Excel / CSV export**
  - Filter **All VDCs** → full inventory
  - Filter **one VDC** → only vApps/VMs of that VDC

## Configuration

Environment variables in `docker-compose.yml`:

| Variable | Default | Description |
|----------|---------|-------------|
| `VCD_BASE_URL` | `https://portal.zibris-cloud.ch` | Cloud Director portal |
| `VCD_API_VERSION` | `39.1` | API version in the `Accept` header |
| `TZ` | `Europe/Zurich` | Time zone for the “Stand” timestamp |
| `SESSION_SECRET` | `zibris-local-dev-secret-change-on-server` | Secret for session cookies – **change on the server** |
| `SESSION_HTTPS_ONLY` | `false` | Set to `true` when the app runs behind HTTPS |
| `PORT` | `8080` | Port inside the container (Compose mapping: `8080:8080`) |

Example for a server with HTTPS behind a reverse proxy:

```yaml
environment:
  SESSION_SECRET: "long-random-secret"
  SESSION_HTTPS_ONLY: "true"
```

## Architecture

```
Browser  →  FastAPI (Docker)  →  VMware Cloud Director API
                 │
                 ├─ Login: POST /cloudapi/1.0.0/sessions
                 └─ Data:  GET  /api/query (vApp / vm)
```

Passwords are not stored permanently. After login, the inventory is kept in server memory (session); after a container restart, you must sign in again.

## Project layout

```
zibris-inventory-tool/
├── app/
│   ├── main.py           # FastAPI app, login, export
│   ├── vcd_client.py     # Cloud Director client
│   ├── templates/        # HTML
│   └── static/           # CSS / JS
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

## Notes

- The user must be **enabled** in Cloud Director.
- VM templates are filtered out (`isVAppTemplate==false`).
- Health check: `GET /healthz`

## License

Internal / private – adjust as needed.
