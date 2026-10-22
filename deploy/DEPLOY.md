# Deployment (CI/CD to a DigitalOcean VPS)

```
push to master ─▶ GitHub Actions
                   ├─ test    ruff, ~280 tests (≥95% coverage), Django deploy checks
                   ├─ build   Docker image ─▶ ghcr.io/eyob-smax/spotter-backend:sha-<commit> and :latest
                   └─ deploy  SSH ─▶ VPS: pull image, restart, wait for health checks,
                              roll back automatically if unhealthy, then check https://<domain>/api/health
VPS (docker compose):  Caddy (automatic HTTPS) ─▶ gunicorn/Django ─▶ Redis
```

- Pull requests and other branches only run **test**.
- The server never builds code: it pulls the image CI built and tested.
- The deploy job uses the `production` environment, so you can add required reviewers in GitHub (*Settings → Environments*) if you want a manual approval step.
- **Manual re-deploy:** *Actions → CI/CD → Run workflow* on `master`.
- **Roll back to an earlier commit:** on the server, run `bash /opt/spotter/deploy/remote-deploy.sh ghcr.io/eyob-smax/spotter-backend:sha-<commit>`.

## Server
| | |
|---|---|
| IP | `164.90.224.22` |
| API domain | `api.164-90-224-22.sslip.io`. sslip.io resolves this name to the IP with no DNS setup, and Caddy gets a Let's Encrypt certificate for it. You can switch to your own domain later by pointing an `A` record at the IP and changing `API_DOMAIN` |
| App directory | `/opt/spotter`, owned by user `deploy` |

## One-time setup

### 1. Create a deploy key for CI (on your machine)
```bash
ssh-keygen -t ed25519 -N "" -C "github-actions-deploy" -f ~/.ssh/spotter_deploy
```

### 2. Bootstrap the server (as root)
```bash
scp deploy/bootstrap-server.sh root@164.90.224.22:/root/
ssh root@164.90.224.22 "bash /root/bootstrap-server.sh \"$(cat ~/.ssh/spotter_deploy.pub)\""
```
This installs Docker, creates the `deploy` user with the CI key, creates `/opt/spotter` and opens ports 22/80/443.

> **Port 80/443:** this server currently runs a system **nginx**, showing its default page. Caddy needs those ports.
> - **If nginx serves nothing else,** re-run the script with `--stop-nginx` added after the key.
> - **If nginx hosts other sites,** don't stop it. Ask to switch this deployment to run behind nginx instead of Caddy.

### 3. Create the server's `.env` (secrets live only on the server)
```bash
ssh root@164.90.224.22
sudo -u deploy nano /opt/spotter/.env
```
```ini
DEBUG=0
SECRET_KEY=<python3 -c "import secrets; print(secrets.token_urlsafe(50))">
ALLOWED_HOSTS=api.164-90-224-22.sslip.io
API_DOMAIN=api.164-90-224-22.sslip.io
CORS_ALLOWED_ORIGINS=https://<your-app>.vercel.app,http://localhost:5173
NUM_PROXIES=1
TOMTOM_API_KEY=<your key>
LOG_LEVEL=INFO
```
Then run `chmod 600 /opt/spotter/.env`.

### 4. GitHub repository settings (*Settings → Secrets and variables → Actions*)
| Kind | Name | Value |
|---|---|---|
| Secret | `DEPLOY_HOST` | `164.90.224.22` |
| Secret | `DEPLOY_USER` | `deploy` |
| Secret | `DEPLOY_SSH_KEY` | Contents of `~/.ssh/spotter_deploy`, the **private** key |
| Secret | `DEPLOY_KNOWN_HOSTS` | Output of `ssh-keyscan -t ed25519,ecdsa,rsa 164.90.224.22`. Compare the fingerprint with the droplet console (`ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub`) |
| Variable | `API_DOMAIN` | `api.164-90-224-22.sslip.io` |
| Variable | `DEPLOY_PATH` | *(optional)* defaults to `/opt/spotter` |

With the GitHub CLI:
```bash
gh secret set DEPLOY_HOST --body 164.90.224.22
gh secret set DEPLOY_USER --body deploy
gh secret set DEPLOY_SSH_KEY < ~/.ssh/spotter_deploy
ssh-keyscan -t ed25519,ecdsa,rsa 164.90.224.22 2>/dev/null | gh secret set DEPLOY_KNOWN_HOSTS
gh variable set API_DOMAIN --body api.164-90-224-22.sslip.io
```

### 5. Deploy
Push to `master`. When the run is green:
```bash
curl https://api.164-90-224-22.sslip.io/api/health      # {"status":"ok","cache":"ok"}
```
The interactive docs are at <https://api.164-90-224-22.sslip.io/api/docs>.

## Operating
```bash
ssh deploy@164.90.224.22
cd /opt/spotter
docker compose -f docker-compose.prod.yml ps               # status and health
docker compose -f docker-compose.prod.yml logs -f backend  # request ids appear in [brackets]
cat .deployed-image                                        # currently deployed image
```

## Frontend (Vercel)
Set `VITE_API_URL=https://api.164-90-224-22.sslip.io` in the Vercel project, and add the Vercel URL to `CORS_ALLOWED_ORIGINS` in the server `.env`. The browser calls the API directly, with no Vercel rewrite:
- A rewrite would hide client IPs from rate limiting.
- Vercel's proxy timeout is shorter than the slowest cross-country route lookups (about 20 s).
