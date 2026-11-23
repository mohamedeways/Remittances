# Remittance Tracker: Deployment & Security Guide

For the IT department and the app owner. Follow the parts in order.

---

## Part A. What to request from IT

Copy this into your request.

**Server**
- Linux VM, Ubuntu 22.04 or 24.04 LTS.
- 2 vCPU, 2 GB RAM (1 GB works but leaves no margin).
- 50 GB SSD to start. Receipt images are about 150-300 KB each, so roughly 10 GB per year at 100 uploads per day (estimate; check after the first month).
- Docker Engine with the Compose plugin installed, or permission to install them.
- A sudo-capable account for the person deploying.

**Network** (salesmen use the app from the field on mobile data, so it must be reachable from the internet)
- A static public IP, or a reverse proxy / DMZ entry pointing to the server.
- A DNS name, for example `remit.yourcompany.com`, pointing to that IP.
- Inbound: TCP 443 and TCP 80 from anywhere (80 is only for the HTTPS certificate and redirect). SSH (22) only from the IT/admin network.
- Outbound: allowed (system updates, certificate renewal, Google Fonts for the page font).
- If company policy forbids public access: IT supplies a certificate and VPN access for salesmen instead (see "Own certificate" in Part B, step 6).

**Operations**
- Daily backup of the folder `/opt/remit/data` to a separate location, with a tested restore.
- OS security updates enabled.

**Data**
- The receipts contain customer payment details. Confirm with IT/legal whether they must be hosted in-country or follow other data-handling rules.

---

## Part B. Deployment steps

Run these on the server, as a user with sudo.

### 1. Prepare the server
```bash
sudo apt update && sudo apt -y upgrade
sudo apt -y install unattended-upgrades ufw unzip
sudo dpkg-reconfigure -plow unattended-upgrades      # enable automatic security updates
```
Install Docker Engine + Compose plugin following the official guide for Ubuntu:
https://docs.docker.com/engine/install/ubuntu/

Check: `docker --version` and `docker compose version` both print versions.

### 2. Firewall
```bash
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow from <ADMIN_NETWORK_CIDR> to any port 22 proto tcp
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw enable
sudo ufw status
```
The app container (port 8000) is never published, so it cannot be reached directly.

### 3. DNS
Create an `A` record `remit.yourcompany.com` -> the server's public IP. Wait until
`nslookup remit.yourcompany.com` returns it.

### 4. Copy the project
From your PC:
```bash
scp remittance-app.zip youruser@SERVER_IP:/tmp/
```
On the server:
```bash
sudo unzip /tmp/remittance-app.zip -d /opt         # creates /opt/remit
sudo chown -R $USER /opt/remit
cd /opt/remit
mkdir -p data && sudo chown -R 1000:1000 data       # the container runs as user 1000
```
You should now see `main.py`, `docker-compose.yml`, `Caddyfile`, `Dockerfile` in `/opt/remit`.

### 5. Configure
```bash
cp .env.example .env
chmod 600 .env
nano .env
```
Fill in:
- `DOMAIN` = your DNS name.
- `SECRET_KEY` = output of `openssl rand -hex 32`.
- `ADMIN_USERNAME` and `ADMIN_PASSWORD` = a strong password (16+ characters).
- Leave `COOKIE_SECURE=true`.
- `EXPORT_KEY` only if Power BI will refresh from the CSV link (use `openssl rand -hex 24`).

### 6. Start
```bash
docker compose up -d --build
docker compose ps                 # both services "running", app "healthy"
docker compose logs -f            # Ctrl+C to leave
```
Caddy requests the HTTPS certificate automatically on first start (takes under a minute).

**Own certificate (internal or VPN-only servers).** Put `fullchain.pem` and `privkey.pem` in `/opt/remit/certs`,
uncomment the `./certs` volume in `docker-compose.yml` and the `tls` line in `Caddyfile`, then
`docker compose up -d`.

### 7. First login and lock-down
1. Open `https://remit.yourcompany.com`, log in as the admin.
2. Click **Change password** and set a new strong password.
3. On the server, delete the `ADMIN_PASSWORD=` line from `.env` (it is only used on the very first start), then `docker compose up -d`.
4. In **Users**, create the finance users and the salesmen. Give each person their own account, never a shared one.
5. Send each salesman the link and his username and password in a private message.

### 8. Backups
Daily database backup (consistent while the app runs), keeps 30 days:
```bash
sudo crontab -e
# add this line:
0 2 * * * cd /opt/remit && docker compose exec -T app python backup.py >> /var/log/remit-backup.log 2>&1
```
IT's own backup job must copy `/opt/remit/data` (contains `app.db`, `backups/`, `uploads/`) and `/opt/remit/.env` to another system. Receipt images are only ever added, so incremental copies are efficient.

**Restore test (do this once before go-live):**
```bash
cd /opt/remit && docker compose down
cp data/backups/app-YYYYMMDD-HHMM.db data/app.db      # choose the backup to restore
docker compose up -d
```
Images are restored by copying back the `uploads/` folder.

### 9. Updating the app later
```bash
cd /opt/remit
# replace the changed files, then:
docker compose up -d --build
docker image prune -f
```
Data is kept in `./data`, so updates do not touch it.

### Useful commands
```bash
docker compose logs --tail 100 app     # app log
docker compose restart app
docker compose down                    # stop everything (data stays)
du -sh data/uploads                    # storage used by images
```

---

## Part C. Security checklist

**What the app already does**
- Passwords stored as bcrypt hashes; login attempts limited (8 per 10 minutes per user and address).
- Session in an HttpOnly, SameSite=Strict, Secure cookie; sessions expire after 12 hours.
- Every receipt image is behind login. A salesman can only open his own; finance and admin see all.
- Server-side role checks on every action; disabled users are locked out immediately.
- Uploads are limited to 15 MB. Images are re-encoded as clean JPEG with EXIF data removed.
- PDFs are validated and rendered to an image on the server (first 3 pages), so finance views a picture,
  not the PDF itself. The original PDF is kept and offered as a download. Password-protected, damaged
  and fake PDFs are rejected.
- Browser protections: strict Content-Security-Policy (no inline scripts), no framing, HSTS, no-sniff.
- Full history of who confirmed, rejected or transferred each item, including any difference between the
  amount the salesman typed and the amount finance confirmed.
- API docs endpoints are switched off.

**What IT should check**
- [ ] HTTPS only; the certificate renews automatically (Caddy) or is managed by IT.
- [ ] Only ports 80 and 443 are open to the internet; SSH is limited to the admin network and uses keys, not passwords (`PasswordAuthentication no` in `/etc/ssh/sshd_config`).
- [ ] Automatic security updates are on; the server is rebooted for kernel updates.
- [ ] `.env` is readable only by the deploying user (`chmod 600`) and is never committed or emailed.
- [ ] Backups are copied off the server and a restore has been tested.
- [ ] Optional: put a WAF or CDN with DDoS protection in front, and monitor the logs.

**What the business owner should do**
- [ ] One personal account per person; disable accounts the day someone leaves (Users tab).
- [ ] Strong passwords (8 characters is the technical minimum; ask for 12+).
- [ ] Review the user list monthly; keep admin accounts to one or two people.
- [ ] Keep the Power BI export key private; rotate it by changing `EXPORT_KEY` and restarting.

**Known limits**
- No two-factor login and no email password reset; the admin resets passwords.
- One server instance with SQLite is fine for dozens of users. For many hundreds, switch to PostgreSQL (`DATABASE_URL`).
- Rate limiting is per running process and resets on restart.
- Uploaded files are not virus-scanned. Keep the server's antivirus/endpoint protection on, and tell finance to open downloaded PDFs only from the trusted site.

---

## Part D. Go-live test (10 minutes)

1. `https://remit.yourcompany.com` opens with a valid padlock.
2. `http://` redirects to `https://`.
3. Salesman uploads a receipt from a phone on mobile data.
4. Finance sees it, confirms it, marks it transferred.
5. The salesman sees "Transferred".
6. A second salesman cannot see the first one's receipts.
7. Export CSV opens in Excel with Arabic names intact.
8. `docker compose restart` and confirm data is still there.
9. The cron backup runs once: a file appears in `/opt/remit/data/backups`.
10. Admin password changed and `ADMIN_PASSWORD` removed from `.env`.
