# Deployment (CI/CD to a shared DigitalOcean VPS)

```
push to master ─▶ GitHub Actions
                   ├─ test    ruff, ~280 tests (≥95% coverage), Django deploy checks
                   ├─ build   Docker image ─▶ ghcr.io/eyob-smax/spotter-backend:sha-<commit> and :latest
                   └─ deploy  SSH ─▶ VPS: pull image, restart, wait for health checks,
                              roll back automatically if unhealthy, then check https://<domain>/api/health

VPS:  nginx (existing, HTTPS via certbot) ─▶ 127.0.0.1:8010 ─▶ docker compose project "spotter":
                                                              backend (gunicorn/Django) + redis
```

**This server also hosts other apps,** so the deployment stays out of their way:
- The compose project is named `spotter`; its containers, network and Redis are separate from other projects.
- The backend is published only on `127.0.0.1:8010`. Redis isn't published at all.
- nginx gets one extra site file. Nothing else in nginx, the firewall or Docker changes.
- Image cleanup removes only old `spotter-backend` images, and keeps the last 3 for rollbacks.

**Running the pipeline:**
- Pull requests and other branches run **test** only.
- The deploy job uses the `production` environment. To require manual approval, add reviewers under *Settings → Environments*.
- **Manual re-deploy:** *Actions → CI/CD → Run workflow* on `master`.
- **Roll back to an earlier commit:** on the server, run `bash /opt/spotter/deploy/remote-deploy.sh ghcr.io/eyob-smax/spotter-backend:sha-<commit>`.

## Server
| | |
|---|---|
| IP | `164.90.224.22` |
| API domain | `api.spotter.wearecerta.app`. The DNS `A` record (managed in cPanel) must point to `164.90.224.22`, with no `AAAA` record, before running certbot |
| App directory | `/opt/spotter`, owned by user `deploy` |
| Backend port | `127.0.0.1:8010`. To change it, set `BACKEND_PORT` in `.env` and update `proxy_pass` in the nginx site |

## One-time setup

### 1. Create a deploy key for CI (on your machine)
```bash
ssh-keygen -t ed25519 -N "" -C "github-actions-deploy" -f ~/.ssh/spotter_deploy
```

### 2. Prepare the server (as root)
```bash
scp deploy/bootstrap-server.sh root@164.90.224.22:/root/
ssh root@164.90.224.22 "bash /root/bootstrap-server.sh \"$(cat ~/.ssh/spotter_deploy.pub)\""
```
This checks that Docker is present, creates the `deploy` user with the CI key, creates `/opt/spotter`, and warns you if port 8010 is already taken.

### 3. Server `.env` (secrets live only on the server)
```bash
sudo -u deploy nano /opt/spotter/.env && sudo chmod 600 /opt/spotter/.env
```
```ini
DEBUG=0
SECRET_KEY=<python3 -c "import secrets; print(secrets.token_urlsafe(50))">
ALLOWED_HOSTS=api.spotter.wearecerta.app
CORS_ALLOWED_ORIGINS=https://<your-app>.vercel.app,http://localhost:5173
NUM_PROXIES=1
BACKEND_PORT=8010
TOMTOM_API_KEY=<your key>
LOG_LEVEL=INFO
```

### 4. nginx site and HTTPS
```bash
scp deploy/nginx/spotter-api.conf root@164.90.224.22:/etc/nginx/sites-available/spotter-api.conf
ssh root@164.90.224.22
ln -s /etc/nginx/sites-available/spotter-api.conf /etc/nginx/sites-enabled/
nginx -t && systemctl reload nginx
certbot --nginx -d api.spotter.wearecerta.app      # issues the certificate and adds the HTTPS server + redirect
```

### 5. GitHub repository settings (*Settings → Secrets and variables → Actions*)
| Kind | Name | Value |
|---|---|---|
| Secret | `DEPLOY_HOST` | `164.90.224.22` |
| Secret | `DEPLOY_USER` | `deploy` |
| Secret | `DEPLOY_SSH_KEY` | Contents of `~/.ssh/spotter_deploy`, the **private** key |
| Secret | `DEPLOY_KNOWN_HOSTS` | Output of `ssh-keyscan -t ed25519,ecdsa,rsa 164.90.224.22` |
| Variable | `API_DOMAIN` | `api.spotter.wearecerta.app` |
| Variable | `DEPLOY_PATH` | *(optional)* defaults to `/opt/spotter` |

```bash
gh secret set DEPLOY_HOST --body 164.90.224.22
gh secret set DEPLOY_USER --body deploy
gh secret set DEPLOY_SSH_KEY < ~/.ssh/spotter_deploy
ssh-keyscan -t ed25519,ecdsa,rsa 164.90.224.22 2>/dev/null | gh secret set DEPLOY_KNOWN_HOSTS
gh variable set API_DOMAIN --body api.spotter.wearecerta.app
```

### 6. Deploy
Push to `master`, or re-run the workflow. When the run is green:
```bash
curl https://api.spotter.wearecerta.app/api/health      # {"status":"ok","cache":"ok"}
```

## Operating
```bash
ssh deploy@164.90.224.22
cd /opt/spotter
docker compose -f docker-compose.prod.yml ps               # status and health
docker compose -f docker-compose.prod.yml logs -f backend  # request ids appear in [brackets]
cat .deployed-image                                        # currently deployed image
```

## Frontend (Vercel)
- Set `VITE_API_URL=https://api.spotter.wearecerta.app` in the Vercel project.
- Add the Vercel URL to `CORS_ALLOWED_ORIGINS` in `/opt/spotter/.env`, then run `docker compose -f docker-compose.prod.yml up -d`.
- The browser calls the API directly, with no Vercel rewrite, so rate limiting sees real client IPs.
