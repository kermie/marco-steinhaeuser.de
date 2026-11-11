#!/usr/bin/env python3
"""Builds the blog: content/ (Markdown) + templates/ + assets/ -> public/ (plain HTML).

    python3 build.py                 # build into public/
    python3 build.py --serve         # build, then preview on the first free port from 8000
    python3 build.py --serve 8123    # ... on a specific port

Content (the Nextcloud folder, see content/Readme.md):

    site.yml                      title, menu, sidebar, ...
    posts/20210708-good-bye-oxid.md   -> /2021/07/08/good-bye-oxid/
    pages/legal-information-and-contact-html.md   -> /legal-information-and-contact-html/
    comments/good-bye-oxid.yml    old comments of that post (read-only, no new ones)
    media/                        pictures and files, linked as /media/name.jpg

Every .md file starts with a header between two "---" lines (title, date,
tags, excerpt, image). "draft: true" or a file name starting with "_" keeps a
file off the website; Nextcloud conflict copies are ignored.

The addresses are the ones WordPress used, so links from elsewhere keep
working. Raw HTML in Markdown is allowed (captions, tables, embeds), but
scripts, event handlers, forms and iframes from unknown sites are removed,
and media/ only publishes pictures, videos, PDFs and office files.
"""
import datetime
import hashlib
import html
import http.server
import json
import math
import os
import pathlib
import re
import shutil
import sys
import urllib.parse

ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / ".pylib"))

import jinja2  # noqa: E402
import markdown  # noqa: E402
import yaml  # noqa: E402
from bs4 import BeautifulSoup  # noqa: E402
try:
    from PIL import Image, ImageOps  # noqa: E402
except ImportError:
    Image = None

CONTENT = ROOT / "content"
PUBLIC = ROOT / "public"
TEMPLATES = ROOT / "templates"
ASSETS = ROOT / "assets"
CACHE = ROOT / ".cache"
# Named like WordPress' sitemap, so search consoles need no change.
SITEMAP = "wp-sitemap.xml"

CARD_WIDTH = 600         # the theme's card and featured images: 600 wide, not cropped
CONTENT_WIDTH = 1024     # wider pictures in posts get a smaller copy (linked to the original)

MONTHS = ["January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December"]
CONFLICT_COPY = re.compile(r"conflicted copy|Konflikt", re.I)
DATE_PREFIX = re.compile(r"^(\d{4})-?(\d{2})-?(\d{2})-(.+)$")
# What media/ may publish. Everything else could run on the web server
# (.php) or in the visitor's browser (.html, .svg) -- see .htaccess too.
MEDIA_TYPES = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif", ".pdf",
               ".mp4", ".webm", ".m4v", ".mov", ".mp3", ".ogg", ".zip",
               ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".odt", ".ods", ".odp", ".txt"}
RESIZABLE = {".jpg", ".jpeg", ".png", ".webp"}
# Apache may run "x.php.jpg" as PHP too, so every part of the name counts.
EXECUTABLE_PART = re.compile(r"\.(php\d*|phtml|pht|phar|cgi|pl|py|sh|shtml|html?|svg|js)(\.|$)", re.I)
# Embeds that may stay in posts (and that the Content-Security-Policy allows).
IFRAME_HOSTS = {"www.slideshare.net", "www.youtube.com", "www.youtube-nocookie.com", "player.vimeo.com"}
DROP_TAGS = {"script", "style", "form", "input", "button", "textarea", "select", "object", "embed",
             "base", "meta", "link", "frame", "frameset", "applet", "svg", "math"}
URL_ATTRS = {"href", "src", "action", "poster", "cite", "formaction"}

warnings = []


def warn(where, message):
    warnings.append(f"{where.relative_to(ROOT) if isinstance(where, pathlib.Path) else where}: {message}")


def wp_slug(name):
    """WordPress' sanitize_title(): what the old tag addresses were made of."""
    value = html.unescape(name).lower()
    for a, b in (("ä", "a"), ("ö", "o"), ("ü", "u"), ("ß", "ss"), ("é", "e"), ("è", "e"), ("á", "a"), ("à", "a"),
                 ("ó", "o"), ("ò", "o"), ("í", "i"), ("ú", "u"), ("ñ", "n"), ("ç", "c"), ("ł", "l"), ("ń", "n")):
        value = value.replace(a, b)
    value = value.replace(".", "-")
    value = re.sub(r"[^a-z0-9 _-]", "", value)
    value = re.sub(r"\s+", "-", value)
    return re.sub(r"-+", "-", value).strip("-")


def date_text(value):
    return f"{MONTHS[value.month - 1]} {value.day}, {value.year}"


def time_text(value):
    hour = value.hour % 12 or 12
    return f"{date_text(value)}, {hour}:{value.minute:02d} {'am' if value.hour < 12 else 'pm'}"


BLOCK_TAGS = ["p", "div", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "td", "th", "br",
              "blockquote", "pre", "figure", "figcaption", "table", "hr", "dt", "dd"]


def plain_text(fragment):
    """Text only; inline tags (links, emphasis) don't add spaces, blocks do."""
    soup = BeautifulSoup(fragment, "html.parser")
    for tag in soup.find_all(BLOCK_TAGS):
        tag.insert_after(" ")
    return " ".join(soup.get_text("").split())


def read_time(text):
    """The Bizberg theme's estimate: 200 words a minute, seconds below one minute."""
    # WordPress counted the text as typed, with "it's" -- not the curly "it’s".
    words = len(re.findall(r"[A-Za-z'’-]+", text))
    if words < 200:
        return f"{round(words * 0.3)} sec read"
    return f"{words // 200} min read"


def parse_date(value, path):
    if isinstance(value, datetime.datetime):
        return value
    if isinstance(value, datetime.date):
        return datetime.datetime.combine(value, datetime.time(12))
    try:
        return datetime.datetime.fromisoformat(str(value).strip())
    except ValueError:
        warn(path, f'date "{value}" not understood (use 2026-03-04 or 2026-03-04 17:48)')
        return None


def outside_site(path):
    path = urllib.parse.unquote(path)
    return not path.startswith("/") or path.startswith("//") or "\\" in path or ".." in path.split("/")


def output_file(url):
    # Web servers decode the request path before looking for the file.
    url = urllib.parse.unquote(url.split("#")[0].split("?")[0])
    if url.endswith("/"):
        return PUBLIC / url.strip("/") / "index.html" if url != "/" else PUBLIC / "index.html"
    return PUBLIC / url.lstrip("/")


def relative_links(page, depth):
    """"/x" -> "x" or "../x": relative links work wherever the site lives,
    in a sub-folder (for a test) or at the top of the domain."""
    up = "../" * depth

    def fix(url):
        return (up + url[1:]) or "./"

    page = re.sub(r'((?:href|src|action|poster)=")(/(?!/)[^"]*)"', lambda m: m.group(1) + fix(m.group(2)) + '"', page)
    return re.sub(r'(srcset=")([^"]*)"', lambda m: m.group(1) + re.sub(
        r"(^|,\s*)(/(?!/)[^\s,]*)", lambda n: n.group(1) + fix(n.group(2)), m.group(2)) + '"', page)


HTACCESS = r"""# Written by build.py -- changes here are overwritten.
DirectoryIndex index.html
Options -Indexes
ErrorDocument 404 /404.html

<IfModule mod_rewrite.c>
RewriteEngine On

# One address only: www.marco-steinhaeuser.de -> marco-steinhaeuser.de
RewriteCond %{HTTP_HOST} ^www\.(.+)$ [NC]
RewriteRule ^ https://%1%{REQUEST_URI} [R=301,L]

# WordPress addresses that are gone. They work in a sub-folder too: %1 is
# whatever comes before them.
# Search: /?s=word -> /search/?s=word (the query string goes along)
RewriteCond %{QUERY_STRING} (?:^|&)s=
RewriteCond %{REQUEST_URI} !/search/$
RewriteCond %{REQUEST_URI} ^(.*/)$
RewriteRule ^ %1search/ [R=301,L]
# Uploads: /wp-content/uploads/photo-600x400.jpg -> /media/photo.jpg
RewriteCond %{REQUEST_URI} ^(.*/)wp-content/uploads/(.+)-\d+x\d+(\.\w+)$
RewriteRule ^ %1media/%2%3 [R=301,L]
RewriteCond %{REQUEST_URI} ^(.*/)wp-content/uploads/(.+)$
RewriteRule ^ %1media/%2 [R=301,L]
# Feeds: /feed/, /comments/feed/, /tag/x/feed/, a post's /feed/ -> the one feed
RewriteRule ^(?:(?:comments|tag/[^/]+|category/[^/]+|\d{4}/\d\d/\d\d/[^/]+)/)?feed(?:/(?:rss2?|atom|rdf))?/?$ feed.xml [L]
</IfModule>

# The website is plain HTML: no script file is ever served or run, even if
# one got onto the hosting some other way (build.py doesn't publish any).
<IfModule mod_authz_core.c>
<FilesMatch "\.(php\d*|phtml|pht|phar|cgi|pl|py|sh|shtml)(\.|$)">
Require all denied
</FilesMatch>
</IfModule>

<IfModule mod_mime.c>
AddCharset utf-8 .txt .xml .json
</IfModule>
<Files "feed.xml">
ForceType application/rss+xml
</Files>

# What browsers may load and run on these pages: our own files, pictures from
# anywhere (old posts show some from other sites) and the allowed embeds.
<IfModule mod_headers.c>
Header always set Content-Security-Policy "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: https:; font-src 'self'; media-src 'self'; frame-src FRAMES; connect-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'; object-src 'none'"
Header always set X-Content-Type-Options "nosniff"
Header always set X-Frame-Options "DENY"
Header always set Referrer-Policy "strict-origin-when-cross-origin"
Header always set Permissions-Policy "camera=(), microphone=(), geolocation=()"
</IfModule>
"""


# --------------------------------------------------------------------------
# Content
# --------------------------------------------------------------------------

def split_front_matter(text, path):
    if text.startswith("﻿"):
        text = text[1:]
    if text.startswith("---"):
        parts = re.split(r"^---\s*$", text, maxsplit=2, flags=re.M)
        if len(parts) == 3:
            try:
                meta = yaml.safe_load(parts[1]) or {}
            except yaml.YAMLError as error:
                warn(path, f"header is not valid YAML, file skipped: {error}")
                return None, None
            if not isinstance(meta, dict):
                warn(path, "header is not a list of 'key: value' lines, file skipped")
                return None, None
            return meta, parts[2]
    return {}, text


def visible_files(folder, suffix):
    if not folder.is_dir():
        return []
    return sorted(p for p in folder.rglob(f"*{suffix}")
                  if p.is_file() and not p.name.startswith(("_", ".")) and not CONFLICT_COPY.search(p.name))


class Site:
    def __init__(self):
        config_file = CONTENT / "site.yml"
        if not config_file.exists():
            sys.exit("content/site.yml is missing -- is content/ the copy of the Nextcloud folder?")
        try:
            self.config = yaml.safe_load(config_file.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as error:
            sys.exit(f"content/site.yml is not valid YAML: {error}")
        self.config.setdefault("sidebar", {})
        self.config.setdefault("banner", {})
        self.site_url = str(self.config.get("site_url", "")).rstrip("/")
        self.md = markdown.Markdown(extensions=["extra", "sane_lists"], output_format="html")
        self.env = jinja2.Environment(loader=jinja2.FileSystemLoader(TEMPLATES), autoescape=True,
                                      trim_blocks=False, lstrip_blocks=False)
        self.env.filters["safe_url"] = safe_url
        self.written = set()
        self.media = {}          # "/media/x.jpg" -> source path
        self.links = []          # (page url, href) of every internal link, checked at the end
        self.version = self.asset_version()

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def asset_version():
        digest = hashlib.sha256()
        for path in sorted(ASSETS.rglob("*")):
            if path.is_file():
                digest.update(path.relative_to(ASSETS).as_posix().encode())
                digest.update(path.read_bytes())
        return digest.hexdigest()[:10]

    def absolute(self, url):
        return self.site_url + url

    def media_source(self, url):
        """content/media/... for a "/media/..." address, or None."""
        path = urllib.parse.unquote(urllib.parse.urlsplit(url).path)
        if not path.startswith("/media/") or outside_site(path):
            return None
        source = CONTENT / path.lstrip("/")
        return source if source.is_file() and source.suffix.lower() in MEDIA_TYPES and not EXECUTABLE_PART.search(source.name) else None

    def resized(self, url, width, height=None):
        """A smaller copy of a /media/ picture in /thumbs/: width wide, or
        cropped to width x height. Returns (url, width, height); the original
        if it's small enough already, isn't a photo, or Pillow is missing."""
        source = self.media_source(url)
        if source is None or Image is None or source.suffix.lower() not in RESIZABLE:
            return url, None, None
        try:
            with Image.open(source) as probe:
                size = ImageOps.exif_transpose(probe).size if hasattr(ImageOps, "exif_transpose") else probe.size
        except Exception as error:  # broken or not really a picture
            warn(source, f"can't read picture: {error}")
            return url, None, None
        if size[0] <= width and (height is None or size[1] <= height):
            return url, size[0], size[1]
        if height and (size[0] < width or size[1] < height):
            return url, size[0], size[1]
        rel = source.relative_to(CONTENT / "media")
        suffix = f"-{width}x{height}" if height else f"-{width}w"
        target_url = "/thumbs/" + urllib.parse.quote(rel.with_name(rel.stem + suffix + rel.suffix).as_posix())
        target = PUBLIC / "thumbs" / rel.with_name(rel.stem + suffix + rel.suffix)
        stamp = f"{source.stat().st_size}-{source.stat().st_mtime_ns}"
        cached = CACHE / "thumbs" / target.relative_to(PUBLIC / "thumbs")
        stamp_file = cached.with_name(cached.name + ".stamp")
        if not (cached.exists() and stamp_file.exists() and stamp_file.read_text() == stamp):
            resample = getattr(getattr(Image, "Resampling", Image), "LANCZOS")
            with Image.open(source) as image:
                image = ImageOps.exif_transpose(image)
                if height:
                    image = ImageOps.fit(image, (width, height), method=resample)
                else:
                    image = image.resize((width, round(size[1] * width / size[0])), resample)
                if image.mode not in ("RGB", "RGBA", "L") or (source.suffix.lower() in (".jpg", ".jpeg") and image.mode != "RGB"):
                    image = image.convert("RGB")
                cached.parent.mkdir(parents=True, exist_ok=True)
                image.save(cached, quality=85, optimize=True)
            stamp_file.write_text(stamp)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(cached, target)
        self.written.add(target)
        with Image.open(cached) as done:
            return target_url, done.size[0], done.size[1]

    def clean_html(self, fragment, where):
        """Raw HTML in posts is allowed, but nothing that runs code or sends
        data elsewhere. Also: local links -> addresses inside the site,
        big pictures -> a smaller copy that links to the original."""
        soup = BeautifulSoup(fragment, "html.parser")
        for tag in soup.find_all(DROP_TAGS):
            warn(where, f"<{tag.name}> removed")
            tag.decompose()
        for tag in soup.find_all(True):
            for attr in list(tag.attrs):
                # data-*: Bootstrap 3 (theme) builds HTML from some of them.
                if attr.lower().startswith(("on", "data-")) or attr.lower() in ("style", "srcdoc", "formaction"):
                    del tag[attr]
            for attr in URL_ATTRS & set(tag.attrs):
                value = str(tag[attr]).strip()
                scheme = urllib.parse.urlsplit(value).scheme.lower()
                if scheme and scheme not in ("http", "https", "mailto", "tel"):
                    warn(where, f'{attr}="{value[:60]}" removed')
                    del tag[attr]
        for frame in soup.find_all("iframe"):
            src = frame.get("src", "")
            host = urllib.parse.urlsplit(urllib.parse.urljoin("https:", src)).netloc
            if host not in IFRAME_HOSTS:
                warn(where, f"embed from {host or '?'} replaced by a link (allowed: IFRAME_HOSTS in build.py)")
                link = soup.new_tag("a", href=urllib.parse.urljoin("https:", src))
                link.string = urllib.parse.urljoin("https:", src)
                frame.replace_with(link)
            else:
                frame["src"] = urllib.parse.urljoin("https:", src)
                frame["loading"] = "lazy"
        for img in soup.find_all("img"):
            src = img.get("src", "")
            img["loading"] = "lazy"
            img["decoding"] = "async"
            if src.startswith("/media/"):
                if self.media_source(src) is None:
                    warn(where, f"picture {src} not found in media/")
                    continue
                new, w, h = self.resized(src, CONTENT_WIDTH)
                if w:
                    img["width"], img["height"] = str(w), str(h)
                if new != src:
                    img["src"] = new
                    if img.parent.name != "a":
                        link = soup.new_tag("a", href=src)
                        img.wrap(link)
            elif src.startswith("http://"):
                img["src"] = "https://" + src[len("http://"):]  # no mixed content
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if href.startswith("/") and not href.startswith("//"):
                self.links.append((where, href))
            elif href.startswith(("http://", "https://")):
                host = urllib.parse.urlsplit(href).netloc.lower()
                if host in ("marco-steinhaeuser.de", "www.marco-steinhaeuser.de"):
                    parts = urllib.parse.urlsplit(href)
                    a["href"] = urllib.parse.urlunsplit(("", "", parts.path or "/", parts.query, parts.fragment))
                    self.links.append((where, a["href"]))
        return str(soup)

    def render_markdown(self, text, where):
        self.md.reset()
        return self.clean_html(self.md.convert(text), where)

    # -- reading -----------------------------------------------------------

    def read_post(self, path):
        meta, body = split_front_matter(path.read_text(encoding="utf-8", errors="replace"), path)
        if meta is None or meta.get("draft"):
            return None
        match = DATE_PREFIX.match(path.stem)
        slug = match.group(4) if match else path.stem
        if not re.fullmatch(r"[a-z0-9_-]+", slug):
            warn(path, f'file name "{slug}" should only use a-z, 0-9 and "-" (it becomes the address)')
            slug = wp_slug(slug)
        date = parse_date(meta["date"], path) if meta.get("date") else None
        if date is None and match:
            date = datetime.datetime(int(match.group(1)), int(match.group(2)), int(match.group(3)), 12)
        if date is None:
            date = datetime.datetime.fromtimestamp(path.stat().st_mtime)
        title = str(meta.get("title") or "").strip()
        if not title:
            heading = re.search(r"^# (.+)$", body, re.M)
            title = heading.group(1).strip() if heading else slug.replace("-", " ").capitalize()
            if heading:
                body = body.replace(heading.group(0), "", 1)
        html_body = self.render_markdown(body, path)
        text = plain_text(html_body)
        tags = meta.get("tags") or []
        if isinstance(tags, str):
            tags = [t.strip() for t in tags.split(",")]
        url = f"/{date:%Y/%m/%d}/{slug}/"
        return {
            "kind": "post", "source": path, "slug": slug, "title": title, "date": date,
            "date_text": date_text(date), "url": url, "month_url": f"/{date:%Y/%m}/",
            "html": html_body, "text": text, "read_time": read_time(text),
            "excerpt": " ".join(str(meta.get("excerpt") or "").split()) or text,
            "has_excerpt": bool(meta.get("excerpt")),
            "tag_names": [str(t) for t in tags if str(t).strip()],
            "image": meta.get("image"), "comments_file": slug,
        }

    def read_page(self, path):
        meta, body = split_front_matter(path.read_text(encoding="utf-8", errors="replace"), path)
        if meta is None or meta.get("draft"):
            return None
        slug = path.relative_to(CONTENT / "pages").with_suffix("").as_posix()
        title = str(meta.get("title") or slug.rsplit("/", 1)[-1].replace("-", " ").capitalize())
        html_body = self.render_markdown(body, path)
        return {"kind": "page", "source": path, "title": title, "url": f"/{slug}/", "html": html_body,
                "text": plain_text(html_body)}

    def read_comments(self, post):
        path = CONTENT / "comments" / f"{post['comments_file']}.yml"
        if not path.exists():
            return []
        try:
            items = yaml.safe_load(path.read_text(encoding="utf-8")) or []
        except yaml.YAMLError as error:
            warn(path, f"not valid YAML, comments not shown: {error}")
            return []
        out = []
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict) or not item.get("text"):
                continue
            date = parse_date(item.get("date", ""), path) or post["date"]
            url = str(item.get("url") or "")
            out.append({
                "id": item.get("id") or len(out) + 1, "author": str(item.get("author") or "Anonymous"),
                "url": url if url.startswith(("http://", "https://")) else "",
                "date": date, "date_text": time_text(date),
                "html": self.render_markdown(str(item["text"]), path),
            })
        return sorted(out, key=lambda c: c["date"])

    def load(self):
        self.posts = [p for p in map(self.read_post, visible_files(CONTENT / "posts", ".md")) if p]
        self.posts.sort(key=lambda p: p["date"], reverse=True)
        seen = {}
        for post in self.posts:
            if post["url"] in seen:
                warn(post["source"], f"same address as {seen[post['url']].relative_to(ROOT)} -- rename one")
            seen[post["url"]] = post["source"]
        self.pages = [p for p in map(self.read_page, visible_files(CONTENT / "pages", ".md")) if p]
        for comment_file in visible_files(CONTENT / "comments", ".yml"):
            if comment_file.stem not in {p["comments_file"] for p in self.posts}:
                warn(comment_file, "no post with this name -- comments not shown")

        self.tags = {}
        for post in self.posts:
            post["comments"] = self.read_comments(post)
            post["tags"] = []
            for name in post["tag_names"]:
                slug = wp_slug(name)
                if not slug:
                    continue
                tag = self.tags.setdefault(slug, {"name": name, "slug": slug, "url": f"/tag/{slug}/", "posts": []})
                tag["posts"].append(post)
                post["tags"].append(tag)
            image = post["image"]
            post["thumb"] = post["featured"] = None
            if image:
                if self.media_source(image) is None:
                    warn(post["source"], f"image {image} not found in media/")
                else:
                    src, w, h = self.resized(image, CARD_WIDTH)
                    post["thumb"] = post["featured"] = {"src": src, "width": w or CARD_WIDTH, "height": h or ""}

    # -- writing -----------------------------------------------------------

    def write(self, url, template, **context):
        target = output_file(url)
        depth = 0 if template == "404.html" else len(url.strip("/").split("/")) if url.strip("/") else 0
        stylesheets = [f"/assets/{name}?v={self.version}" for name in (
            "bizberg/assets/bootstrap/css/bootstrap.min.css", "bizberg/assets/icons/font-awesome-5/css/all.css",
            "bizberg/assets/css/main.css", "bizberg/assets/css/component.css", "bizberg/assets/css/style.css",
            "bizberg/assets/css/responsive.css", "bizberg/style.css", "green-globe/style.css",
            "fonts/lato.css", "wp-inline.css", "site.css")]
        scripts = [f"/assets/{name}?v={self.version}" for name in (
            "wp/jquery.min.js", "wp/jquery-migrate.min.js", "bizberg/assets/bootstrap/js/bootstrap.min.js",
            "wp/imagesloaded.min.js", "wp/masonry.min.js", "bizberg/assets/js/jquery.mousewheel.min.js",
            "bizberg/assets/js/jquery.inview.min.js", "bizberg/assets/js/jquery.slicknav.min.js",
            "bizberg/assets/js/jquery.matchHeight-min.js", "bizberg/assets/js/swiper.js",
            "bizberg/assets/js/prognroll.js", "bizberg/assets/js/theia-sticky-sidebar.js",
            "theme-config.js", "bizberg/assets/js/custom.js", "site.js")]
        scripts += [f"/assets/{name}?v={self.version}" for name in context.pop("extra_scripts", [])]
        page = self.env.get_template(template).render(
            site=self.config, stylesheets=stylesheets, scripts=scripts, version=self.version, root="../" * depth,
            year=datetime.date.today().year,
            canonical=self.absolute(url) if self.site_url and template != "404.html" else None, **context)
        if template != "404.html":
            page = relative_links(page, depth)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(page, encoding="utf-8")
        self.written.add(target)

    def write_text(self, name, text):
        target = PUBLIC / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        self.written.add(target)


    def pagination(self, base, page, pages):
        """WordPress' paginate_links(): first and last page, current +-1, dots between."""
        if pages <= 1:
            return None

        def url(n):
            return base if n == 1 else f"{base}page/{n}/"
        items, dotted = [], False
        for n in range(1, pages + 1):
            if n in (1, pages) or abs(n - page) <= 1:
                items.append({"number": n, "url": url(n), "current": n == page})
                dotted = False
            elif not dotted:
                items.append({"dots": True})
                dotted = True
        return {"links": items, "prev": url(page - 1) if page > 1 else None,
                "next": url(page + 1) if page < pages else None}

    def write_list(self, base, posts, title, heading, breadcrumb, body_class, is_home=False):
        per_page = int(self.config.get("posts_per_page") or 10)
        pages = max(1, math.ceil(len(posts) / per_page))
        for page in range(1, pages + 1):
            url = base if page == 1 else f"{base}page/{page}/"
            self.write(url, "list.html", posts=posts[(page - 1) * per_page:page * per_page],
                       page_title=title if not is_home else None, page_number=page if is_home and page > 1 else None,
                       heading=heading, breadcrumb=breadcrumb, is_home=is_home,
                       body_class=body_class + (f" paged paged-{page}" if page > 1 else "")
                       + (" hfeed bizberg_transparent_header" if is_home else " hfeed"),
                       pagination=self.pagination(base, page, pages),
                       description=self.config.get("description") if is_home and page == 1 else None)

    def write_feed(self):
        items = []
        for post in self.posts[:10]:
            body = relative_to_absolute(post["html"], self.site_url)
            items.append(
                "<item>"
                f"<title>{html.escape(post['title'])}</title>"
                f"<link>{html.escape(self.absolute(post['url']))}</link>"
                f"<guid isPermaLink=\"true\">{html.escape(self.absolute(post['url']))}</guid>"
                f"<pubDate>{post['date'].strftime('%a, %d %b %Y %H:%M:00')} +0000</pubDate>"
                + "".join(f"<category>{html.escape(t['name'])}</category>" for t in post["tags"])
                + f"<description>{html.escape(post['excerpt'] if post['has_excerpt'] else post['excerpt'][:400])}</description>"
                f"<content:encoded><![CDATA[{body.replace(']]>', ']]]]><![CDATA[>')}]]></content:encoded>"
                "</item>")
        feed = ('<?xml version="1.0" encoding="UTF-8"?>\n'
                        '<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/" '
                        'xmlns:atom="http://www.w3.org/2005/Atom"><channel>'
                        f"<title>{html.escape(self.config['title'])}</title>"
                        f"<atom:link href=\"{self.absolute('/feed/')}\" rel=\"self\" type=\"application/rss+xml\"/>"
                        f"<link>{self.absolute('/')}</link>"
                        f"<description>{html.escape(str(self.config.get('tagline', '')))}</description>"
                        f"<language>{self.config.get('language', 'en-US')}</language>"
                + "\n".join(items) + "</channel></rss>\n")
        self.write_text("feed.xml", feed)
        # nginx ignores .htaccess: /feed/ itself must hold the feed (feed
        # readers go by the content, not the file name).
        self.write_text("feed/index.html", feed)

    def write_search_engine_files(self):
        urls = ["/"] + [p["url"] for p in self.posts] + [p["url"] for p in self.pages] + \
               [t["url"] for t in self.tags.values()]
        self.write_text("robots.txt", f"User-agent: *\nDisallow:\n\nSitemap: {self.absolute('/' + SITEMAP)}\n")
        self.write_text(SITEMAP, '<?xml version="1.0" encoding="UTF-8"?>\n'
                        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
                        + "".join(f"<url><loc>{html.escape(self.absolute(u))}</loc></url>\n" for u in urls)
                        + "</urlset>\n")
        out = [f"# {self.config['title']}", "", f"> {self.config.get('description', '')}", "", "## Pages"]
        out += [f"- [{p['title']}]({self.absolute(p['url'])})" for p in self.pages]
        out += ["", "## Posts (newest first)"]
        out += [f"- [{p['title']}]({self.absolute(p['url'])}) ({p['date_text']})"
                + (f": {p['excerpt']}" if p["has_excerpt"] else "") for p in self.posts]
        self.write_text("llms.txt", "\n".join(out) + "\n")

    def write_redirect(self, path, target):
        """A tiny page that forwards to target: works on any web server
        (the host runs nginx, which ignores .htaccess)."""
        depth = path.count("/")
        rel = "../" * depth + target.lstrip("/")
        url = html.escape(rel)
        self.write_text(path, f'<!doctype html><meta charset="utf-8"><title>Moved</title>'
                        f'<meta name="robots" content="noindex">'
                        f'<link rel="canonical" href="{html.escape(self.absolute(target))}">'
                        f'<meta http-equiv="refresh" content="0; url={url}">'
                        f'<p>This page has moved: <a href="{url}">{url}</a></p>\n')

    def write_old_addresses(self):
        """/slug, /slug.html (the addresses before 2020) and other old
        WordPress addresses -> where they are now."""
        taken = {output_file(p["url"]) for p in self.pages} | {PUBLIC / "feed", PUBLIC / "search", PUBLIC / "tag",
                                                                PUBLIC / "category", PUBLIC / "page", PUBLIC / "media"}
        for post in self.posts:
            for path in (f"{post['slug']}.html", f"{post['slug']}/index.html"):
                if PUBLIC / path not in self.written and (PUBLIC / path).parent not in taken \
                        and output_file("/" + path) not in taken:
                    self.write_redirect(path, post["url"])
        legal = "/legal-information-and-contact-html/"
        for path in ("legal-information-and-contact.html", "legal-information-and-contact/index.html", "about/index.html"):
            self.write_redirect(path, legal)

    def slug_redirects(self):
        """/slug, /slug/ and /slug.html (the addresses before 2020) -> the
        post's dated address, like WordPress guessed them."""
        lines = ["", "# Short and pre-2020 post addresses: /slug, /slug.html -> /YYYY/MM/DD/slug/",
                 "<IfModule mod_rewrite.c>"]
        taken = {p["url"].strip("/") for p in self.pages}
        for post in sorted(self.posts, key=lambda p: p["slug"]):
            if post["slug"] in taken:
                continue
            # The pattern is relative to the site's folder (so a post's own
            # /YYYY/MM/DD/slug/ doesn't match); %1 is that folder.
            lines.append(f"RewriteCond %{{REQUEST_URI}} ^(.*/){post['slug']}")
            lines.append(f"RewriteRule ^{post['slug']}(?:\\.html|/)?$ %1{post['url'].lstrip('/')} [R=301,L]")
        # Old addresses of the legal page; /about is what old posts linked to.
        lines.append("RewriteCond %{REQUEST_URI} ^(.*/)(?:legal-information-and-contact|about)")
        lines.append("RewriteRule ^(?:legal-information-and-contact(?:\\.html)?|about)/?$ %1legal-information-and-contact-html/ [R=301,L]")
        lines.append("</IfModule>")
        return "\n".join(lines) + "\n"

    def write_search_index(self):
        docs = [{"t": p["title"], "u": p["url"].lstrip("/"), "d": p["date_text"], "s": p["date"].isoformat(),
                 "g": " ".join(t["name"] for t in p["tags"]), "x": p["text"]} for p in self.posts]
        docs += [{"t": p["title"], "u": p["url"].lstrip("/"), "x": p["text"]} for p in self.pages]
        self.write_text("search-index.json", json.dumps(docs, ensure_ascii=False, separators=(",", ":")))

    def copy_tree(self, source, prefix, only_media=False):
        for path in sorted(source.rglob("*")):
            if not path.is_file() or path.name.startswith(".") or CONFLICT_COPY.search(path.name):
                continue
            if only_media and (path.suffix.lower() not in MEDIA_TYPES or EXECUTABLE_PART.search(path.name)):
                warn(path, "not published (only pictures, videos, PDFs and office files)")
                continue
            target = PUBLIC / prefix / path.relative_to(source)
            if not (target.exists() and target.stat().st_size == path.stat().st_size
                    and target.stat().st_mtime_ns == path.stat().st_mtime_ns):
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, target)
            self.written.add(target)

    def check_links(self):
        for where, href in self.links:
            path = urllib.parse.urlsplit(href).path
            if not path or path.startswith("/search/"):
                continue
            target = output_file(path if path.endswith("/") or "." in path.rsplit("/", 1)[-1] else path + "/")
            if target not in self.written:
                warn(where, f"links to {href}, which doesn't exist on the new site")

    def build(self):
        PUBLIC.mkdir(exist_ok=True)
        self.load()

        self.write_list("/", self.posts, None, "", [], "home blog", is_home=True)
        for i, post in enumerate(self.posts):
            self.write(post["url"], "post.html", post=post, page_title=post["title"],
                       breadcrumb=[{"title": "Uncategorized", "url": "/category/uncategorized/"},
                                   {"title": post["title"]}],
                       body_class="single single-post" + (" has_single_page_image" if post["featured"] else ""),
                       description=post["excerpt"][:300],
                       og={"title": post["title"], "image": self.absolute(post["featured"]["src"]) if post["featured"] else None},
                       previous=self.posts[i + 1] if i + 1 < len(self.posts) else None,
                       next=self.posts[i - 1] if i > 0 else None)
        for page in self.pages:
            self.write(page["url"], "page.html", page=page, page_title=page["title"],
                       breadcrumb=[{"title": page["title"]}], body_class="page")
        for tag in self.tags.values():
            self.write_list(tag["url"], tag["posts"], tag["name"], f"Tag: <span>{html.escape(tag['name'])}</span>",
                            [{"title": tag["name"]}], "archive tag")
        self.write_list("/category/uncategorized/", self.posts, "Uncategorized",
                        "Category: <span>Uncategorized</span>", [{"title": "Uncategorized"}], "archive category")
        years, months = {}, {}
        for post in self.posts:
            years.setdefault(post["date"].year, []).append(post)
            months.setdefault((post["date"].year, post["date"].month), []).append(post)
        for year, posts in years.items():
            self.write_list(f"/{year}/", posts, str(year), f"Year: <span>{year}</span>",
                            [{"title": str(year)}], "archive date")
        for (year, month), posts in months.items():
            name = f"{MONTHS[month - 1]} {year}"
            self.write_list(f"/{year}/{month:02d}/", posts, name, f"Month: <span>{name}</span>",
                            [{"title": str(year), "url": f"/{year}/"}, {"title": MONTHS[month - 1][:3]}], "archive date")
        self.write("/search/", "search.html", page_title="Search", breadcrumb=[{"title": "Search"}],
                   body_class="search", noindex=True, extra_scripts=["search.js"])
        self.write("/404.html", "404.html", page_title="Page not found", breadcrumb=[{"title": "404"}],
                   body_class="error404", noindex=True)

        self.write_feed()
        self.write_search_engine_files()
        self.write_search_index()
        frames = " ".join(f"https://{h}" for h in sorted(IFRAME_HOSTS))
        self.write_text(".htaccess", HTACCESS.replace("FRAMES", frames) + self.slug_redirects())
        self.write_old_addresses()

        self.copy_tree(ASSETS, "assets")
        # Browsers ask for /favicon.ico on their own, whatever the page says.
        shutil.copy2(ASSETS / "favicon.ico", PUBLIC / "favicon.ico")
        self.written.add(PUBLIC / "favicon.ico")
        if (CONTENT / "media").exists():
            self.copy_tree(CONTENT / "media", "media", only_media=True)
        self.check_links()

        # Remove whatever an earlier build left behind (deleted posts etc.).
        for path in sorted(PUBLIC.rglob("*"), reverse=True):
            if path.is_file() and path not in self.written:
                path.unlink()
            elif path.is_dir() and not any(path.iterdir()):
                path.rmdir()


def safe_url(value):
    """Links from site.yml: web addresses and paths only (no javascript:)."""
    value = str(value or "").strip()
    scheme = urllib.parse.urlsplit(value).scheme.lower()
    if scheme and scheme not in ("http", "https", "mailto", "tel"):
        warn("content/site.yml", f'link "{value[:60]}" ignored')
        return "#"
    return value


def relative_to_absolute(fragment, site_url):
    return re.sub(r'((?:href|src)=")/(?!/)', lambda m: m.group(1) + site_url + "/", fragment)


def preview_server(port=None):
    class Handler(http.server.SimpleHTTPRequestHandler):
        def send_error(self, code, message=None, explain=None):
            if code == 404 and (PUBLIC / "404.html").exists():
                body = (PUBLIC / "404.html").read_bytes()
                self.send_response(404)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                super().send_error(code, message, explain)

        def log_message(self, *args):
            pass

    for candidate in [port] if port else range(8000, 8100):
        try:
            server = http.server.ThreadingHTTPServer(("127.0.0.1", candidate), Handler)
            break
        except OSError:
            if port:
                sys.exit(f"Port {port} is already in use -- try another one.")
    return server, f"http://localhost:{candidate}"


def main():
    args = sys.argv[1:]
    if not (args == [] or (args[:1] == ["--serve"] and len(args) <= 2 and all(a.isdigit() for a in args[1:]))):
        sys.exit(__doc__)
    server, preview_url = preview_server(int(args[1]) if len(args) == 2 else None) if args else (None, None)
    site = Site()
    site.build()
    print(f"Built {len(site.posts)} posts, {len(site.pages)} pages, {len(site.tags)} tags -> {PUBLIC.relative_to(ROOT)}/")
    for message in warnings:
        print(f"  ! {message}")
    if server:
        os.chdir(PUBLIC)
        print(f"Preview: {preview_url}  (Ctrl+C to stop)", flush=True)
        server.serve_forever()


if __name__ == "__main__":
    main()
