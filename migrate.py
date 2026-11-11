#!/usr/bin/env python3
"""One-time conversion of the WordPress blog into Markdown files.

    python3 migrate.py        # archive/ (REST API dump + uploads) -> content/

Input (not in git, see .gitignore):
  archive/wp-api/*.json   the WordPress REST API (posts, pages, tags, media, comments)
  archive/uploads/        every file under /wp-content/uploads/ the posts refer to

Output:
  content/posts/YYYYMMDD-slug.md     one file per post, front matter + Markdown
  content/pages/slug.md              the two pages
  content/comments/slug.yml          the approved comments of a post (read-only now)
  content/media/                     the original uploads (not WordPress' resized copies)
  migration-report.md                what was removed, kept as HTML or looks odd
"""
import html
import json
import pathlib
import re
import shutil
import sys
import urllib.parse

ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / ".pylib"))

from bs4 import BeautifulSoup, NavigableString  # noqa: E402
from markdownify import markdownify  # noqa: E402

RAW = ROOT / "archive" / "wp-api"
UPLOADS = ROOT / "archive" / "uploads"
CONTENT = ROOT / "content"
SITE_HOSTS = {"marco-steinhaeuser.de", "www.marco-steinhaeuser.de"}
MEDIA_URL = "/media/"
# WordPress' resized copies: photo-600x400.jpg -> photo.jpg
SIZE_SUFFIX = re.compile(r"-\d+x\d+(?=\.\w+$)")
# Comments flagged by hand while reviewing the 67 approved ones.
SPAM_COMMENTS = {"graco magnum"}


def load(name):
    return json.loads((RAW / f"{name}.json").read_text(encoding="utf-8"))


def text(value):
    return " ".join(BeautifulSoup(html.unescape(value), "html.parser").get_text(" ").split())


def yaml_value(value):
    # JSON strings/lists are valid YAML.
    return json.dumps(value, ensure_ascii=False)


def tidy(markdown):
    markdown = re.sub(r"[ \t]+\n", lambda m: "  \n" if m.group(0).startswith("  ") else "\n", markdown)
    markdown = re.sub(r"\n{3,}", "\n\n", markdown)
    return markdown.strip() + "\n"


class Converter:
    def __init__(self):
        self.media = {m["id"]: m for m in load("media")}
        self.tags = {t["id"]: t for t in load("tags")}
        self.posts = load("posts")
        self.pages = load("pages")
        self.comments = load("comments")
        self.copied = set()
        self.report = {}
        # Current address of every post and page, by slug and by WordPress id.
        self.by_slug, self.by_id = {}, {}
        for item in self.posts + self.pages:
            url = urllib.parse.urlparse(item["link"]).path
            self.by_slug[urllib.parse.unquote(item["slug"])] = url
            self.by_id[str(item["id"])] = url
        # The legal page was once /legal-information-and-contact.html.
        self.by_slug["legal-information-and-contact"] = self.by_slug.get("legal-information-and-contact-html")
        # An About page that no longer exists ("please turn to me") -> contact.
        self.by_slug["about"] = self.by_slug.get("legal-information-and-contact-html")

    def note(self, path, message):
        self.report.setdefault(path, []).append(message)

    def local_media(self, url):
        """Copies an upload into content/media/ (the original, not a resized
        copy) and returns its new address, or None if we don't have it."""
        path = urllib.parse.unquote(urllib.parse.urlparse(url).path)
        if "/wp-content/uploads/" not in path:
            return None
        rel = path.split("/wp-content/uploads/", 1)[1]
        source = UPLOADS / rel
        original = source.with_name(SIZE_SUFFIX.sub("", source.name))
        if original.is_file():
            source, rel = original, original.relative_to(UPLOADS).as_posix()
        if not source.is_file():
            return None
        if rel not in self.copied:
            target = CONTENT / "media" / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            self.copied.add(rel)
        return MEDIA_URL + urllib.parse.quote(rel)

    def rewrite_url(self, url, path):
        url = url.strip()
        if url.lower().startswith("%20"):  # a pasted link with a leading space
            url = url[3:]
        if not url or url.startswith(("#", "mailto:", "tel:")):
            return url
        absolute = urllib.parse.urljoin("https://marco-steinhaeuser.de/", url)
        parsed = urllib.parse.urlparse(absolute)
        if parsed.netloc in SITE_HOSTS:
            if "/wp-content/uploads/" in parsed.path:
                new = self.local_media(absolute)
                if not new:
                    self.note(path, f"upload not found, link kept: {url}")
                return new or absolute
            if parsed.path.startswith(("/wp-admin", "/wp-login", "/wp-json", "/xmlrpc")):
                self.note(path, f"link into WordPress admin removed: {url}")
                return None
            fragment = f"#{parsed.fragment}" if parsed.fragment else ""
            query = dict(urllib.parse.parse_qsl(parsed.query))
            if query.get("p") in self.by_id or query.get("page_id") in self.by_id:
                return self.by_id[query.get("p") or query["page_id"]] + fragment
            # Links from before 2020, when addresses were /slug.html.
            old = re.fullmatch(r"/([^/]+?)(?:\.html)?/?", parsed.path)
            if old and old.group(1) in self.by_slug:
                new = self.by_slug[old.group(1)]
                if new != parsed.path:
                    self.note(path, f"old link {parsed.path} -> {new}")
                return new + fragment
            if parsed.query:
                self.note(path, f"link with ?{parsed.query} kept, check it: {url}")
            return parsed.path + (f"?{parsed.query}" if parsed.query else "") + fragment
        return url

    def best_src(self, img):
        candidates = [(0, img.get("src") or "")]
        for part in (img.get("srcset") or "").split(","):
            bits = part.split()
            if len(bits) == 2 and bits[1].endswith("w"):
                candidates.append((int(bits[1][:-1]), bits[0]))
        return max(candidates)[1]

    def to_markdown(self, root, path):
        kept = []

        def keep_html(el):
            """Things Markdown can't express (captions, galleries, tables,
            embeds) stay as HTML -- build.py cleans it like everything else."""
            for tag in el.find_all(True):
                tag.attrs = {k: v for k, v in tag.attrs.items()
                             if k in ("src", "href", "alt", "class", "width", "height", "title", "colspan", "rowspan", "allowfullscreen")}
                if tag.name == "img":
                    tag["loading"] = "lazy"
            kept.append(str(el))
            el.replace_with(NavigableString(f"\n\nXXKEPT{len(kept) - 1}XX\n\n"))

        for el in root.find_all(["script", "style", "noscript"]):
            el.decompose()
        for el in root.find_all("object"):
            embed = el.find("embed")
            src = (embed or el).get("src") or ""
            self.note(path, f"Flash video (dead since 2020) replaced by a link: {src}")
            el.replace_with(BeautifulSoup(f'<p><em>(Video, no longer available: <a href="{html.escape(src)}">{html.escape(src)}</a>)</em></p>', "html.parser"))
        for el in root.find_all("embed"):
            el.decompose()

        for a in root.find_all("a", href=True):
            new = self.rewrite_url(a["href"], path)
            img = a.find("img")
            # A link around an image that leads to the image itself or to the
            # WordPress attachment page: the build adds its own link.
            if img and not a.get_text(strip=True) and (new is None or new.startswith(MEDIA_URL) or "attachment" in a["href"]):
                a.unwrap()
            elif new is None:
                a.unwrap()
            else:
                a["href"] = new
        for img in root.find_all("img"):
            src = self.best_src(img)
            new = self.rewrite_url(src, path)
            if new and not new.startswith(MEDIA_URL):
                self.note(path, f"external image kept as remote link: {new}")
            img.attrs = {"src": new or src, "alt": img.get("alt", "")}

        for el in root.find_all("iframe"):
            el["src"] = urllib.parse.urljoin("https:", el.get("src", ""))
            self.note(path, f"embed kept as HTML: {el['src']}")
            keep_html(el)
        for el in root.select("figure.wp-block-gallery, figure:has(figcaption), div.wp-caption, table"):
            if el.parent is not None:
                keep_html(el)

        markdown = markdownify(str(root), heading_style="ATX", bullets="-", code_language="",
                               escape_underscores=False, escape_misc=False, escape_asterisks=False)
        for i, block in enumerate(kept):
            markdown = markdown.replace(f"XXKEPT{i}XX", block)
        return tidy(markdown)

    def excerpt(self, item, body_text):
        """Only hand-written excerpts are kept; WordPress' automatic ones
        (the first 55 words + "[…]") are made by the build again."""
        rendered = item.get("excerpt", {}).get("rendered", "")
        if "[&hellip;]" in rendered or "&hellip;" in rendered[-40:] or "[…]" in rendered:
            return None
        excerpt = text(rendered)
        # The theme's automatic excerpt is the whole text, untruncated.
        if not excerpt or excerpt.replace(" ", "") == body_text.replace(" ", ""):
            return None
        return excerpt

    def convert(self, item, kind):
        slug = urllib.parse.unquote(item["slug"])
        if kind == "post":
            path = f"posts/{item['date'][:10].replace('-', '')}-{slug}.md"
        else:
            path = f"pages/{slug}.md"
        soup = BeautifulSoup(item["content"]["rendered"], "html.parser")
        body = self.to_markdown(soup, path)

        meta = {"title": text(item["title"]["rendered"]), "date": item["date"].replace("T", " ")[:16]}
        if item.get("modified") and item["modified"][:10] != item["date"][:10]:
            meta["updated"] = item["modified"].replace("T", " ")[:16]
        if kind == "post":
            meta["tags"] = [html.unescape(self.tags[t]["name"]) for t in item.get("tags", []) if t in self.tags]
            excerpt = self.excerpt(item, text(item["content"]["rendered"]))
            if excerpt:
                meta["excerpt"] = excerpt
        featured = self.media.get(item.get("featured_media"))
        if featured:
            meta["image"] = self.local_media(featured["source_url"]) or featured["source_url"]
        if kind == "post" and item.get("comment_status") == "closed":
            meta["comments"] = "closed"

        front = "\n".join(f"{key}: {yaml_value(value)}" for key, value in meta.items())
        target = CONTENT / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"---\n{front}\n---\n\n{body}", encoding="utf-8")
        return slug

    def write_comments(self, post_slugs):
        by_post = {}
        for c in self.comments:
            if c["author_name"].strip().lower() in SPAM_COMMENTS:
                self.note("comments", f"spam comment left out: {c['author_name']} on post {c['post']}")
                continue
            by_post.setdefault(c["post"], []).append(c)
        for post_id, items in by_post.items():
            slug = post_slugs.get(post_id)
            if not slug:
                continue
            lines = [
                "# Comments from the WordPress days, shown read-only below the post.",
                "# Delete an entry (from \"- author:\" to the next one) to remove it.",
                "",
            ]
            for c in sorted(items, key=lambda c: c["date"]):
                body = markdownify(c["content"]["rendered"], escape_underscores=False, escape_misc=False,
                                   escape_asterisks=False).strip()
                body = re.sub(r"\n{3,}", "\n\n", body)
                entry = {"author": html.unescape(c["author_name"]), "date": c["date"].replace("T", " ")[:16]}
                if c.get("author_url"):
                    entry["url"] = c["author_url"]
                entry["id"] = c["id"]
                lines.append(f"- author: {yaml_value(entry['author'])}")
                for key in ("date", "url", "id"):
                    if key in entry:
                        lines.append(f"  {key}: {yaml_value(entry[key])}")
                lines.append("  text: |")
                lines += [f"    {line}".rstrip() for line in body.split("\n")]
                lines.append("")
            target = CONTENT / "comments" / f"{slug}.yml"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("\n".join(lines), encoding="utf-8")

    def write_report(self):
        out = ["# Migration report", "",
               "Written by `migrate.py`: everything the conversion removed, kept as",
               "HTML or found odd -- worth a look.", ""]
        for path in sorted(self.report):
            out.append(f"## `{path}`")
            out += [f"- {m}" for m in dict.fromkeys(self.report[path])]
            out.append("")
        (ROOT / "migration-report.md").write_text("\n".join(out), encoding="utf-8")

    def run(self):
        for folder in ("posts", "pages", "comments", "media"):
            shutil.rmtree(CONTENT / folder, ignore_errors=True)
        slugs = {p["id"]: self.convert(p, "post") for p in self.posts}
        for page in self.pages:
            self.convert(page, "page")
        self.write_comments(slugs)
        # Uploads no post refers to any more: keep the originals too, so old
        # links to /wp-content/uploads/... still find them (via .htaccess).
        for source in sorted(UPLOADS.rglob("*")):
            rel = source.relative_to(UPLOADS).as_posix()
            if source.is_file() and source.name != "urls.txt" and not SIZE_SUFFIX.search(source.name):
                self.local_media("/wp-content/uploads/" + rel)
        self.write_report()
        print(f"{len(self.posts)} posts, {len(self.pages)} pages, {len(self.copied)} media files -> content/")


if __name__ == "__main__":
    Converter().run()
