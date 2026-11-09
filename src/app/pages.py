"""Pages: what a TV can show full-screen (kiosk route #/page/<id>), managed in Admin → Content.

A page is one of:

- ``slides``: photos and videos from the media library, each for its own time (videos play to
  the end), shown by the slideshow player at /static/player/;
- ``announcement``: a headline and message Xos lays out in the Xos style (built here, so Admin
  and the MCP tools make the same page);
- ``html``: an uploaded HTML file, served at /pages/<id>/ so Admin can scroll it and step
  through its slides;
- ``url``: a link to another site or a path on this server (shown as-is);
- ``playlist``: other pages in rotation.

Built-in pages (the welcome page) are defined here, always listed, and can't be changed.
"""

import html as _html
import json
import re
import time
from collections.abc import Callable
from pathlib import Path

PAGE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}$")
MAX_HTML_BYTES = 5_000_000

# Injected into uploaded pages: lets the kiosk (and so Admin) scroll the page and send keys
# such as ArrowRight for slide decks, through postMessage across the sandbox boundary.
CONTROL_SCRIPT = """<script>
(function () {
  window.addEventListener("message", function (e) {
    var m = e.data || {};
    if (m.xos === "scroll") {
      var h = window.innerHeight * 0.8;
      if (m.dir === "down") window.scrollBy({ top: h, behavior: "smooth" });
      if (m.dir === "up") window.scrollBy({ top: -h, behavior: "smooth" });
      if (m.dir === "top") window.scrollTo({ top: 0, behavior: "smooth" });
      if (m.dir === "bottom") window.scrollTo({ top: document.body.scrollHeight, behavior: "smooth" });
    }
    if (m.xos === "key" && m.key) {
      var opts = { key: m.key, code: m.key, bubbles: true, cancelable: true };
      (document.activeElement || document.body).dispatchEvent(new KeyboardEvent("keydown", opts));
      (document.activeElement || document.body).dispatchEvent(new KeyboardEvent("keyup", opts));
    }
  });
})();
</script>"""


# Shipped with Xos; the default for lobby signage. static/welcome/ holds the page.
BUILTIN = {
    "welcome": {
        "id": "welcome", "name": "Welcome", "kind": "url",
        "url": "/static/welcome/index.html", "builtin": True, "updated": 0,
        "note": "A clock and a welcome message. Replace it with your own pages in Admin → Content.",
    },
}
FITS = ("cover", "contain")
STYLES = ("dark", "gradient", "light")


def announcement_html(tag: str, title: str, body: str, style: str = "dark") -> str:
    """A full-screen announcement in the Xos style (white text on the gradient)."""
    e = _html.escape
    logo = "/static/img/" + ("logo-color" if style == "light" else "logo-white") + ".svg"
    bg = {
        "dark": "radial-gradient(90% 70% at 85% 10%,rgba(37,99,235,.28),transparent 60%),#0b0c10",
        "gradient": "linear-gradient(45deg,#1d4ed8,#0e7490 50%,#14b8a6)",
        "light": "#ffffff",
    }[style]
    ink = "#000" if style == "light" else "#fff"
    sub = {"light": "#525866", "gradient": "#e0f2fe"}.get(style, "#CDD0D5")
    pill = "background:#000;color:#fff" if style == "gradient" else "background:#38bdf8;color:#000"
    body_html = f"<p>{e(body).replace(chr(10), '<br>')}</p>" if body else ""
    tag_html = f'<div class="tag">{e(tag)}</div>' if tag else ""
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>{e(title)}</title>
<style>html,body{{margin:0;height:100%}}
body{{font-family:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;background:{bg};
color:{ink};display:flex;flex-direction:column;justify-content:space-between;padding:7vh 6vw;
box-sizing:border-box}}
header{{display:flex;justify-content:space-between;align-items:center}}header img{{height:4.5vh}}
#c{{font-size:2.4vw;font-weight:700;color:{sub}}}
.tag{{display:inline-block;{pill};font-weight:700;font-size:1.8vw;padding:.5vh 1.4vw;
border-radius:999px;margin-bottom:3vh}}
h1{{font-size:6.4vw;line-height:1.02;margin:0 0 3vh;font-weight:800;letter-spacing:-.01em;
max-width:85vw}}
p{{font-size:2.8vw;line-height:1.35;margin:0;color:{sub};max-width:75vw}}</style></head>
<body><header><img src="{logo}" alt="Xos"><div id="c"></div></header>
<main>{tag_html}<h1>{e(title)}</h1>{body_html}</main><footer></footer>
<script>const c=document.getElementById("c");const t=()=>c.textContent=new Date()
.toLocaleTimeString([],{{hour:"numeric",minute:"2-digit"}});t();setInterval(t,1e4)</script>
</body></html>"""


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:48]
    return slug or "page"


class Pages:
    def __init__(self, data_dir: Path, media_exists: Callable[[str], bool] = lambda _: True):
        self.media_exists = media_exists
        self.dir = data_dir / "pages"
        self.index_path = data_dir / "pages.json"
        try:
            self.items: dict[str, dict] = json.loads(self.index_path.read_text())
        except (FileNotFoundError, ValueError):
            self.items = {}

    def _save(self) -> None:
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.index_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.items))
        tmp.replace(self.index_path)

    def all(self) -> list[dict]:
        mine = sorted(self.items.values(), key=lambda p: p["name"].lower())
        return list(BUILTIN.values()) + mine

    def get(self, page_id: str) -> dict | None:
        return BUILTIN.get(page_id) or self.items.get(page_id)

    def _new_id(self, name: str) -> str:
        base = slugify(name)
        page_id, n = base, 2
        while page_id in self.items or page_id in BUILTIN:
            page_id, n = f"{base}-{n}", n + 1
        return page_id

    def upsert(
        self, page_id: str | None, name: str, html: str | None = None, url: str | None = None,
        playlist: list[dict] | None = None, slides: list[dict] | None = None,
        announcement: dict | None = None, fit: str = "cover",
    ) -> dict:
        if page_id in BUILTIN:
            raise ValueError("built-in pages can't be changed")
        page_id = page_id or self._new_id(name)
        if not PAGE_ID.match(page_id):
            raise ValueError("bad page id")
        page = {"id": page_id, "name": name, "updated": time.time()}
        if announcement is not None:
            a = {k: str(announcement.get(k) or "").strip() for k in ("tag", "title", "body")}
            a["style"] = announcement.get("style") if announcement.get("style") in STYLES else "dark"
            if not a["title"]:
                raise ValueError("an announcement needs a headline")
            html = announcement_html(a["tag"], a["title"], a["body"], a["style"])
            page["announcement"] = a
        if html is not None:
            data = html.encode()
            if len(data) > MAX_HTML_BYTES:
                raise ValueError("HTML is larger than 5 MB")
            folder = self.dir / page_id
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "index.html").write_bytes(data)
            page.update(kind="announcement" if announcement is not None else "html",
                        bytes=len(data))
        elif slides is not None:
            missing = [i["media"] for i in slides if not self.media_exists(i["media"])]
            if not slides or missing:
                raise ValueError("a slideshow needs photos or videos from the media library"
                                 + (f" (not found: {', '.join(missing)})" if missing else ""))
            if fit not in FITS:
                raise ValueError("fit is cover or contain")
            page.update(kind="slides", items=slides, fit=fit)
        elif playlist is not None:
            missing = [i["page"] for i in playlist if not self.get(i["page"])]
            if not playlist or missing or any(i["page"] == page_id for i in playlist):
                raise ValueError("a playlist needs existing pages (and can't contain itself)")
            page.update(kind="playlist", items=playlist)
        elif url:
            if not (url.startswith(("https://", "http://")) or url.startswith("/")):
                raise ValueError("link must start with https://, http:// or /")
            page.update(kind="url", url=url)
        else:
            raise ValueError("give slides, an announcement, an HTML file, a link or a playlist")
        self.items[page_id] = page
        self._save()
        return page

    def delete(self, page_id: str) -> bool:
        if self.items.pop(page_id, None) is None:
            return False
        html = self.dir / page_id / "index.html"
        html.unlink(missing_ok=True)
        self._save()
        return True

    def html(self, page_id: str) -> str | None:
        """Uploaded HTML with the control script added, or None."""
        path = self.dir / page_id / "index.html"
        if self.items.get(page_id, {}).get("kind") not in ("html", "announcement"):
            return None
        if not path.exists():
            return None
        text = path.read_text(errors="replace")
        lower = text.lower()
        at = lower.rfind("</body>")
        return text[:at] + CONTROL_SCRIPT + text[at:] if at >= 0 else text + CONTROL_SCRIPT
