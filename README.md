# garmin-ai

Pulls your own Garmin Connect data (read-only) and publishes a private,
passphrase-protected dashboard.

```
sync_garmin.py     # logs into Garmin, saves data to garmin/data.json + markdown notes
build_site.py      # turns garmin/data.json into the static dashboard in docs/
docs/              # the dashboard (index.html + vendored Chart.js); data is added at build time
.github/workflows/sync.yml   # runs the two scripts daily and deploys to GitHub Pages
```

## How privacy works

GitHub Pages on a free account is publicly reachable, so the dashboard **never
ships readable data**. `build_site.py` encrypts the data with AES-256-GCM (key
derived from your passphrase via PBKDF2-SHA256, 250k iterations) and writes
`docs/data.enc`. The page asks for the passphrase and decrypts in your browser.
The passphrase is never sent anywhere and is not stored in the repo.

Anyone who finds the URL sees only a passphrase prompt and ciphertext.

## Local use

```bash
# one-time login (prompts for email / password / 2FA in your terminal)
./.venv/bin/python sync_garmin.py login

# pull data and preview the dashboard unencrypted on localhost
./.venv/bin/python sync_garmin.py sync 10
./.venv/bin/python build_site.py
python3 -m http.server -d docs 8000      # -> http://localhost:8000
```

With no `SITE_PASSPHRASE` set, `build_site.py` writes plaintext `docs/data.json`
for local preview. Set `SITE_PASSPHRASE` to produce the encrypted `docs/data.enc`
instead. In CI it refuses to build without the passphrase.

## Cloud setup (one time)

1. **Create a private GitHub repo** and push this folder to it.

2. **Add two repository secrets**
   (repo *Settings -> Secrets and variables -> Actions -> New repository secret*):

   | Name | Value |
   |------|-------|
   | `GARMIN_TOKENS_JSON` | the full contents of `.garmin_tokens/garmin_tokens.json` |
   | `SITE_PASSPHRASE` | a strong passphrase you choose for viewing the dashboard |

3. **Enable Pages**: repo *Settings -> Pages -> Build and deployment -> Source:
   GitHub Actions*.

4. **Run it once**: *Actions -> "Sync Garmin & deploy dashboard" -> Run workflow*.
   When it finishes, the dashboard URL is
   `https://<you>.github.io/<repo>/` (also shown on the deploy job).

The workflow then runs every day at 11:00 UTC, commits the new data back to the
repo, and redeploys.

## Maintenance

- **The Garmin token lasts about a year.** When the sync job starts failing with
  an auth error, run `sync_garmin.py login` locally again and update the
  `GARMIN_TOKENS_JSON` secret with the new token file.
- **Change the schedule** in `.github/workflows/sync.yml` (`cron:`).
- **Rotate the view passphrase** by updating the `SITE_PASSPHRASE` secret and
  re-running the workflow.
