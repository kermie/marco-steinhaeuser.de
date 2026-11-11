# marco-steinhaeuser.de

The blog as plain static HTML, built from Markdown. It replaces the old
WordPress site and looks the same (the Bizberg/Green Globe theme's CSS and
scripts are in `assets/`); every old address still works.

- **Content** lives in a Nextcloud folder, not in this repo. The editor
  guide is `Readme.md` in that folder.
- **`build.py`** turns the Markdown into `public/` (templates in
  `templates/`, CSS/JS/fonts in `assets/`, settings in the content's
  `site.yml`).
- **`publish.py`** pulls from Nextcloud, builds and uploads the changes to
  the hosting over FTPS. The GitHub Action `.github/workflows/publish.yml`
  runs it every 30 minutes.
- **`migrate.py`** was the one-time WordPress -> Markdown conversion
  (needs the WordPress dump in `archive/`, not in git).

Setup and day-to-day operation: [DEPLOY.md](DEPLOY.md).

Local preview (needs a copy of the content in `content/`):

```bash
pip install -r requirements.txt
python3 build.py --serve 8123
```
