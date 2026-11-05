"""Turn Markdown (from runbooks, incidents and, later, Claude) into safe HTML.

Two steps, each a safety layer:

1. **markdown-it-py** converts Markdown to HTML. Raw HTML typed into the
   Markdown is *not* passed through (html=False): `<script>` comes out as the
   text "&lt;script&gt;". It also refuses dangerous link targets like
   `javascript:`.

2. **nh3** then cleans the HTML against an *allowlist*: only the tags and
   attributes listed below survive; everything else is removed. So even if
   step 1 had a bug, nothing like `<script>`, `onclick=` or `style=` can reach
   the page. ("Allowlist" = name what's allowed; safer than listing what's
   banned, because new tricks aren't on any ban list.)

Images aren't allowed for now: an image from another site would tell that site
who's reading the runbook. File attachments come in Phase 5.
"""

import nh3
from markdown_it import MarkdownIt
from markupsafe import Markup

# CommonMark (the standard Markdown) plus tables and ~~strikethrough~~.
_md = MarkdownIt("commonmark", {"html": False}).enable(["table", "strikethrough"])

ALLOWED_TAGS = {
    "p", "br", "hr",
    "h1", "h2", "h3", "h4", "h5", "h6",
    "strong", "em", "s", "del", "code", "pre", "kbd", "blockquote",
    "ul", "ol", "li",
    "table", "thead", "tbody", "tr", "th", "td",
    "a",
}  # fmt: skip
ALLOWED_ATTRIBUTES = {
    "a": {"href", "title"},
    "ol": {"start"},  # a list that starts at 3
    "th": {"align"},  # table column alignment
    "td": {"align"},
}
URL_SCHEMES = {"http", "https", "mailto"}


def render_markdown(text: str | None) -> Markup:
    """Markdown in, sanitised HTML out.

    Returns `Markup`, which tells Jinja2 "this is already safe HTML, don't
    escape it again". Only this function should ever create Markup from
    record content.
    """
    if not text:
        return Markup("")
    html = _md.render(text)
    clean = nh3.clean(
        html,
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRIBUTES,
        url_schemes=URL_SCHEMES,
        # Links to other sites can't control this tab (noopener), aren't told
        # where the reader came from (noreferrer), and get no search ranking
        # credit (nofollow).
        link_rel="noopener noreferrer nofollow",
    )
    return Markup(clean)  # noqa: S704  (sanitised by nh3 just above)
