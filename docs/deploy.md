# Deploying OpsPilot to AWS EC2

One instance runs everything with Docker Compose (`docker-compose.prod.yml`): Postgres with
pgvector, the app (FastAPI, one uvicorn worker) and Nginx in front with the dashboard. Only
Nginx's port 80 is reachable from outside. Images come from GHCR, pushed by CI on every merge to
main; they contain no secrets. Secrets live only in `~/opspilot/.env` on the instance, which you
create there by hand.

Prices below are not verified here. Check the current EC2, EBS and public IPv4 rates for your
region on the AWS pricing pages before you launch.

## 1. Launch the instance (AWS console, EC2 -> Launch instance)

- **Name:** `opspilot`
- **AMI:** Ubuntu Server 24.04 LTS (x86_64). The images are built for linux/amd64, so do not pick
  an Arm (Graviton) AMI or instance type.
- **Instance type:** `t3.medium` (2 vCPU, 4 GiB).
- **Key pair:** create one, type ED25519, format `.pem`. The browser downloads it once. On your Mac:

  ```bash
  mv ~/Downloads/opspilot.pem ~/.ssh/ && chmod 400 ~/.ssh/opspilot.pem
  ```

  Never commit, paste or share the `.pem`.
- **Network settings -> Create security group** (`opspilot-sg`), inbound rules:

  | Type | Port | Source | Why |
  | --- | --- | --- | --- |
  | SSH | 22 | **My IP** | only your current address can try to log in |
  | HTTP | 80 | Anywhere (0.0.0.0/0) | the dashboard and API, through Nginx |

  **Never add 5432 (Postgres) or 8001/8000 (the app).** Postgres holds the data and is guarded
  only by a password; the app port would bypass Nginx (and its X-Forwarded-For rewrite, which
  the per-IP rate limit depends on). Compose does not publish them either, so they are closed
  twice. If your home IP changes, edit the SSH rule back to "My IP".
- **Storage:** 30 GiB, **gp3**.
- Launch.

**Optional, Elastic IP** (a fixed address that survives Stop/Start; otherwise the public IP
changes every time you start the instance): EC2 -> Elastic IPs -> Allocate -> Associate with the
instance. AWS bills public IPv4 addresses hourly, including an Elastic IP while it is allocated
(attached or not). I have not verified the current rate: check the VPC pricing page. Release it
when the project is done.

## 2. Install Docker on the instance

```bash
ssh -i ~/.ssh/opspilot.pem ubuntu@<PUBLIC_IP>
```

Then, on the instance (Docker's official apt repository):

```bash
sudo apt-get update && sudo apt-get install -y ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo $VERSION_CODENAME) stable" | sudo tee /etc/apt/sources.list.d/docker.list
sudo apt-get update && sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
sudo usermod -aG docker ubuntu && exit
```

Log in again (the group change needs a new session) and check `docker compose version`.

**Swap (2 GiB).** 4 GiB is tight while the embedder, Postgres and an image pull run at once;
swap turns a memory spike into slowness instead of the kernel killing a container:

```bash
sudo fallocate -l 2G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
free -m
```

`free -m` should show `Swap: 2047`. The fstab line turns it back on after a Stop/Start.

## 3. Get the compose file and pull the images

The GHCR packages are private by default. Either make them public (GitHub -> your profile ->
Packages -> `opspilot` and `opspilot-proxy` -> Package settings -> Change visibility -> Public;
they contain no secrets), or log in on the instance with a classic personal access token that
has only `read:packages` (`docker login ghcr.io -u PatelPrince11`, paste the token at the
prompt, on the instance, never in a chat).

```bash
mkdir -p ~/opspilot && cd ~/opspilot
curl -fsSLO https://raw.githubusercontent.com/PatelPrince11/opspilot/main/docker-compose.prod.yml
curl -fsSL -o .env https://raw.githubusercontent.com/PatelPrince11/opspilot/main/.env.prod.example
docker compose -f docker-compose.prod.yml pull
```

## 4. Create the server `.env`

```bash
chmod 600 .env && nano .env
```

Fill in every `replace-me` (see the comments in the file).

**The site is HTTP only, so the API key travels in cleartext** (the dashboard and the smoke test
send it in the `X-API-Key` header), and anyone on the network path can read it. Use a throwaway
key made for the demo, keep `DAILY_SPEND_LIMIT_USD` low (e.g. 0.50) so a leaked key can cost
little, and rotate it afterwards: put the new key in `API_KEYS`, run `docker compose -f
docker-compose.prod.yml up -d`, and drop the old one.
 For passwords and the API key, make
random values on the instance with `openssl rand -hex 32`. `POSTGRES_PASSWORD` must also appear
inside `DATABASE_URL`. Keep the API key in your password manager; you need it for the dashboard
and the smoke test. Never commit or paste this file.

## 5. Start the stack

```bash
docker compose -f docker-compose.prod.yml up -d
docker compose -f docker-compose.prod.yml ps
```

`migrate` runs once and exits 0; `db`, `app` and `proxy` stay up (and come back by themselves
after a Stop/Start, `restart: unless-stopped`). Logs: `docker compose -f
docker-compose.prod.yml logs -f app`.

## 6. Ingest the corpus

```bash
docker compose -f docker-compose.prod.yml run --rm ingest
```

This downloads LogHub (background noise, not committed), builds `data/processed`, then loads and
embeds the corpus on the instance's CPU (the embedding model downloads on first use into the
`cache` volume). Run it once; it is idempotent if you repeat it.

## 7. Smoke test (from your Mac, in the repo)

```bash
read -rs OPSPILOT_API_KEY && export OPSPILOT_API_KEY
```

```bash
scripts/smoke_remote.sh <PUBLIC_IP>
```

`read -s` keeps the key out of your shell history and off the screen. The script checks
`/health`, runs the demo investigation through Nginx (about $0.005 the first time; a repeat is a
cache hit, $0) and prints `smoke: OK`. Then open `http://<PUBLIC_IP>/` in a browser and paste
the key into the API key field.

If it fails: `/health` refused -> security group port 80 or `proxy` not running; 502 -> `app`
not up (`logs app`); 401 -> the key differs from `API_KEYS`; an error event about a provider ->
a provider key in `.env`; no sources -> step 6 not run. After editing `.env`, run `docker compose
-f docker-compose.prod.yml up -d` again (it recreates the containers that use it).

## 7b. Memory during the Part B latency run

The latency run goes over HTTP from your Mac, so it cannot see the instance's memory. In a
second SSH session, start this right before the run and stop it (Ctrl-C) right after:

```bash
free -m > ~/free_before.txt; free -m -s 5 | tee ~/free_during.txt
```

Then copy the files to the repo for the record (they hold memory figures only, nothing secret):

```bash
scp -i ~/.ssh/opspilot.pem 'ubuntu@<PUBLIC_IP>:free_*.txt' scratchpad/deploy/
```

## 8. Updating

```bash
cd ~/opspilot && docker compose -f docker-compose.prod.yml pull && docker compose -f docker-compose.prod.yml up -d
```

## 9. When you are done: Stop, do not Terminate

EC2 -> Instances -> select -> Instance state -> **Stop**. A stopped instance does not bill compute,
but its 30 GiB gp3 volume (and an allocated Elastic IP) still bill. Terminate deletes the disk and
the database with it; do that only when the project is finished. Check Billing -> Bills a day
later to see what was charged.
