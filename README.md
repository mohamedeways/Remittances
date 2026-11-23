# Remittance Tracker

Salesmen log in, upload deposit receipts / bank transfers; the finance team reviews and
settles them; each salesman sees only his own uploads and their status.

Accepts receipt photos and PDF transfer confirmations (PDFs are shown to finance as an image; the original can be downloaded).

The salesman's typed amount is kept as entered; finance records the confirmed amount actually received
(required before marking Transferred). Totals and Power BI exports show both, plus the difference.

Statuses: Pending -> Confirmed -> Transferred (or Rejected with a reason).
Roles: salesman, finance, admin (admin also manages users).

## Run locally
    pip install -r requirements.txt
    COOKIE_SECURE=false ADMIN_PASSWORD=ChangeMe123 uvicorn main:app --reload
    # open http://localhost:8000 and log in as admin

## Deploy
Full step-by-step guide with the security checklist: see DEPLOYMENT.md (docker-compose + automatic HTTPS).
Quick summary for any Docker host:
1. Create the service from this folder (it has a Dockerfile).
2. Set environment variables: SECRET_KEY, ADMIN_USERNAME, ADMIN_PASSWORD (see .env.example).
3. Attach a persistent disk mounted at /data. It stores the database and the receipt images.
   Without it, data is lost on every redeploy.
4. Use HTTPS (the platforms above provide it). Cookies are secure-only by default.
5. Open the site, log in as admin, create finance and salesman users in the Users tab, and
   share the link with the salesmen.

## PostgreSQL (optional)
Set DATABASE_URL=postgresql://user:pass@host:5432/db. Tables are created automatically.
Images still live in DATA_DIR, so keep the persistent disk.

## Power BI / Excel
- Finance users: "Export CSV" button (respects the filters).
- Scheduled refresh: set EXPORT_KEY, then use https://YOUR-SITE/api/export.csv?key=YOUR_KEY
  in Power BI (Web connector). Anyone with the key can read the data, so keep it private.

## Backups
Back up the /data folder (database + uploads) regularly.
