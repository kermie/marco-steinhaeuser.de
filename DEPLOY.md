# Running the blog from Nextcloud

```
Nextcloud folder  --WebDAV-->  GitHub Action: publish.py  --FTPS-->  hosting
(Markdown, media)               pull, build.py, upload       (plain HTML)
```

Every 30 minutes the workflow `.github/workflows/publish.yml` pulls the
changes from Nextcloud, builds the site and uploads only what changed. The
hosting just receives files -- no PHP, no database. No server of our own.

## 1. Nextcloud (once)

1. Create a folder for the website (any name and place) and upload **the
   contents of** `content/` into it: `posts/`, `pages/`, `media/`,
   `comments/`, `site.yml`, `Readme.md` (~40 MB).
2. Personal settings -> Security -> "Create new app password" (name it
   e.g. "website-publish"). This is what GitHub uses; revoke it there any time.

## 2. Hosting (once)

- FTP host, user, password, and the folder the domain points to.
- **FTPS** (FTP over TLS, port 21) should be offered. Use the server name
  the hoster's certificate is issued for as host (often a hoster name, not
  your own domain) -- otherwise the certificate check fails.
- The site gets **its own empty folder** (e.g. `website` next to the
  WordPress files). All links are relative, so the same files work at
  `https://marco-steinhaeuser.de/website/` (test phase) and, once the
  domain's document root points at that folder, at the top of the domain
  -- no rebuild, switch back any time. Never upload into a folder with
  other files: `publish.py` only deletes files it uploaded itself.
- The build writes a `.htaccess` (Apache): redirects for the old WordPress
  addresses (`/slug.html`, `/wp-content/uploads/...`, `/?s=`, feeds),
  `404.html`, security headers. The redirects and the 404 page only
  work once the folder is the document root.
- After go-live: back up and delete the WordPress files and database.

## 3. GitHub (once)

Repo -> Settings -> Secrets and variables -> Actions.

**Secrets:**

| Name | Value |
|---|---|
| `NEXTCLOUD_URL` | WebDAV address of the folder: `https://cloud.marco-steinhaeuser.de/remote.php/dav/files/USERNAME/FOLDER` (Nextcloud: Files -> Files settings -> WebDAV shows the first part) |
| `NEXTCLOUD_USER` | the Nextcloud user name |
| `NEXTCLOUD_APP_PASSWORD` | the app password from step 1 |
| `FTP_HOST` | e.g. `wp123.example-hoster.de` |
| `FTP_USER`, `FTP_PASSWORD` | FTP login |
| `FTP_REMOTE_DIR` | the site's own folder on the FTP server, e.g. `/website` |

From a terminal (asks for each value, nothing lands in the shell history):

```bash
for s in NEXTCLOUD_URL NEXTCLOUD_USER NEXTCLOUD_APP_PASSWORD \
         FTP_HOST FTP_USER FTP_PASSWORD FTP_REMOTE_DIR; do
  gh secret set "$s" -R kermie/marco-steinhaeuser.de
done
```

**Variables** (all optional):

| Name | Value |
|---|---|
| `PUBLISH_ENABLED` | `true` switches on the 30-minute schedule. Only once the hosting is ready. Delete it or set `false` to pause. |
| `FTP_TLS` | `no` only if the hosting really can't do FTPS (plain FTP sends the password unencrypted) |
| `FTP_PORT` | if not 21 |

Then: Actions -> "Publish website" -> **Run workflow**, mode `dry-run`
(pulls, builds, lists what it *would* upload). If that looks right, run it
with mode `publish` (first run: ~800 files), check the site, then set
`PUBLISH_ENABLED` to `true`.

## Day to day

- **Logs:** Actions -> "Publish website" -> a run. A quiet run says
  `pull: 0 downloaded ...` / `upload: website is up to date`.
- **Failed runs** are red and GitHub emails you. The `STOPPED: ...` line
  says why. Nothing was uploaded; the site stays as it was.
- **In a hurry?** Run workflow -> `publish`.
- **Mass-delete guard:** a run that would delete more than a quarter of all
  files stops. Check Nextcloud; if intended, Run workflow -> `allow-mass-delete`.
- **New hosting or lost files:** Run workflow -> `full-upload`.
- **Templates, CSS, build.py:** commit and push; the next run publishes it.
- **Cost:** a run every 30 minutes uses ~1,500 Action minutes a month --
  free and unlimited for a public repository, within the 2,000 free minutes
  for a private one (shared with your other private repos).

## Without GitHub

`publish.py` runs the same on any Linux machine: settings in `publish.ini`
(see `publish.ini.example`) and a cron line such as

```
*/30 * * * * cd /opt/blog && python3 publish.py >> publish.log 2>&1
```
