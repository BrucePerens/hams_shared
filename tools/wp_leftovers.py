#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""wp_leftovers: remove what WordPress left in HTML that is already stored in Odoo.

Shared by wordpress_to_odoo.py (the converter), odoo_site_migrate.py (the importer's --strip-wordpress) and
odoo_site_wp_cleanup.py (existing posts and pages of a live website). Pure functions on strings: no lxml, no
network, no import of the other tools, so each of them can import it at the top.
"""

import collections
import html as htmllib
import re

_ATTR_RE = re.compile(r"""([\w-]+)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'\]]+))""")


def shortcode_attrs(text):
    return {m.group(1).lower(): next(g for g in m.groups()[1:] if g is not None) for m in _ATTR_RE.finditer(text)}


_CAPTION = re.compile(r"\[caption([^\]]*)\](.*?)\[/caption\]", re.S)
_VIDEO = re.compile(r"\[video([^\]]*)\]\s*(?:\[/video\])?", re.S)
_AUDIO = re.compile(r"\[audio([^\]]*)\]\s*(?:\[/audio\])?", re.S)


# Core shortcodes that would otherwise show as literal text: [gallery], [playlist], [embed]..[/embed]. A
# gallery and a playlist are built from the media library, which the export does not carry, so they are
# removed (and counted, so the report says where a gallery used to be); [embed]URL[/embed] is oEmbed, which
# is not reproduced, so it becomes the link it embedded.
_GALLERY = re.compile(r"\[(?:gallery|playlist)\b[^\]]*\]", re.S)
_EMBED = re.compile(r"\[embed\b[^\]]*\](.*?)\[/embed\]", re.S)
_STRAY_CLOSE = re.compile(r"\[/(?:caption|video|audio|embed|gallery|playlist)\]")


def expand_shortcodes(text, stats):
    """The shortcodes WordPress core registers that appear in classic content: caption, video, audio,
    gallery, playlist, embed. Anything else in square brackets is plain text,
    exactly as WordPress treats an unregistered shortcode."""
    text = re.sub(r"<p>\s*(\[(?:caption|video|audio|gallery|playlist)\b.*?"
                  r"(?:\[/(?:caption|video|audio)\]|\]))\s*(?:<br />)?\s*</p>",
                  r"\1", text, flags=re.S)

    def caption(match):
        stats["shortcode_caption"] += 1
        attrs = shortcode_attrs(match.group(1))
        inner = match.group(2).strip()
        image = re.match(r"^(<a\b[^>]*>\s*<img\b[^>]*>\s*</a>|<img\b[^>]*>)\s*(.*)$", inner, re.S)
        if not image:
            return f"<p>{inner}</p>"
        align = attrs.get("align", "alignnone")
        text_part = re.sub(r"<br\s*/?>", " ", image.group(2)).strip()
        return (f'<figure class="wp2odoo-caption wp2odoo-{align}">{image.group(1)}'
                f"<figcaption>{text_part}</figcaption></figure>")

    def video(match):
        stats["shortcode_video"] += 1
        attrs = shortcode_attrs(match.group(1))
        src = attrs.get("src") or attrs.get("mp4") or attrs.get("webm") or attrs.get("ogv") or attrs.get("m4v")
        if not src:
            return ""
        size = "".join(f' {k}="{attrs[k]}"' for k in ("width", "height") if attrs.get(k, "").isdigit())
        return f'<video controls="controls" preload="metadata"{size} src="{htmllib.escape(src, quote=True)}"></video>'

    def audio(match):
        stats["shortcode_audio"] += 1
        attrs = shortcode_attrs(match.group(1))
        src = attrs.get("src") or attrs.get("mp3") or attrs.get("ogg") or attrs.get("wav") or attrs.get("m4a")
        if not src:
            return ""
        return f'<audio controls="controls" preload="none" src="{htmllib.escape(src, quote=True)}"></audio>'

    def embed(match):
        stats["shortcode_embed"] += 1
        url = match.group(1).strip()
        if not re.match(r"^https?://[^\s<>\"]+$", url):
            return url
        return f'<a href="{htmllib.escape(url, quote=True)}">{htmllib.escape(url, quote=False)}</a>'

    def removed(key):
        def replace(match):
            stats[key] += 1
            return ""
        return replace

    text = _CAPTION.sub(caption, text)
    text = _VIDEO.sub(video, text)
    text = _AUDIO.sub(audio, text)
    text = _EMBED.sub(embed, text)
    text = _GALLERY.sub(removed("shortcode_gallery_removed"), text)
    return _STRAY_CLOSE.sub(removed("shortcode_stray_closer_removed"), text)


ALIGN_CLASSES = {
    "alignleft": "float-start me-3 mb-2", "alignright": "float-end ms-3 mb-2",
    "aligncenter": "mx-auto d-block text-center", "alignnone": "",
}
# WordPress's emoji and smilies are images of a character: s.w.org/images/core/emoji/..., or the smilies under
# /wp-includes/images/smilies/. The alt text of each is the character itself, so the text replaces the image.
EMOJI_IMAGE = re.compile(r"s\.w\.org/images/core/emoji/|/wp-includes/images/(?:smilies|core/emoji)/", re.I)
# Links into WordPress itself (admin, login, REST, feeds, plugin and theme files, attachment pages) go
# nowhere on the new site. The link is dropped and its text and images stay.
WORDPRESS_LINK = re.compile(
    r"^(?:/wp-(?:admin|login\.php|json|includes|cron\.php|comments-post\.php)|/xmlrpc\.php"
    r"|/wp-content/(?:plugins|themes|mu-plugins)/)|[?&](?:attachment_id|replytocom)=\d*|[?&]rest_route=", re.I)


# --------------------------------------------------------------------------------------------
# Content already stored in Odoo
# --------------------------------------------------------------------------------------------
# The Cleaner above rebuilds HTML from a WordPress export. These functions sweep markup that is already in an
# Odoo database (blog.post.content, the body of a page's QWeb arch): they only remove or translate the leftovers
# of WordPress and touch nothing else, so Odoo's own classes and a QWeb arch pass through unchanged and running
# them again changes nothing (strip_wordpress_leftovers(strip_wordpress_leftovers(x)) == strip_wordpress_leftovers(x)).

_SCRIPT_BLOCK = re.compile(r"<script\b[^>]*?/>|<script\b[^>]*>.*?</script\s*>", re.S | re.I)
_WP_COMMENT = re.compile(r"<!--\s*(?:/?wp:.*?|more\b.*?|nextpage|noteaser|wpnl)\s*-->", re.S | re.I)
_START_TAG = re.compile(
    r"<([A-Za-z][\w:.-]*)((?:\s+[^\s=/>\"']+(?:\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^\s\"'>]+))?)*)\s*(/?)>")
_ATTRIBUTE = re.compile(r"([^\s=/>\"']+)(?:\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s\"'>]+))?")
WP_CLASS = re.compile(
    r"^(?:wp-[\w-]+|wp2odoo-[\w-]+|size-[\w-]+|attachment-[\w-]+|align(?:left|right|center|none|wide|full)"
    r"|has-(?!validation$)[\w-]+|is-(?:layout|style|content-justification|vertically-aligned|type|provider|resized"
    r"|nowrap|stacked-on-mobile)[\w-]*|emoji)$")
_BLANK_RUN = r"(?:\s|&nbsp;|&#160;|&#[xX][aA]0;|\u00a0|\u200b|<br\s*/?>)*"
_BLANK_ELEMENT = re.compile(r"<(p|div|span|strong|em|b|i|u)\b[^>]*>" + _BLANK_RUN + r"</\1\s*>", re.I)
_EMOJI_TAG = re.compile(r"<img\b[^>]*>", re.I)
_ANCHOR = re.compile(r"<a\b([^>]*)>(.*?)</a\s*>", re.S | re.I)
_FIGURE = re.compile(r"<figure\b[^>]*>.*?</figure\s*>", re.S | re.I)


def _attributes(raw):
    """[(name, value or None, quote)] of an attribute string, in order."""
    result = []
    for match in _ATTRIBUTE.finditer(raw):
        value = match.group(2)
        quote = value[0] if value and value[0] in "\"'" else ""
        result.append((match.group(1), value[1:-1] if quote else value, quote))
    return result


def _wordpress_class_tokens(tag, tokens):
    """The class tokens of an element with WordPress's own classes removed or translated. The same
    translations the Cleaner makes: alignment becomes Bootstrap's, a block image keeps its centring."""
    kept, added = [], []
    if tag == "figure" and any(t.startswith("wp2odoo-") for t in tokens):
        added.append("figure")  # what a [caption] became in the converter's output
    for token in tokens:
        align = token[8:] if token.startswith("wp2odoo-") else token
        if align in ALIGN_CLASSES:
            if tag in ("img", "figure"):
                added.extend(ALIGN_CLASSES[align].split())
            elif align == "aligncenter":
                added.append("text-center")
        elif token == "wp-block-image" and tag == "figure":
            added.extend(["figure", "text-center"])
        elif token == "wp-block-table" and tag == "figure":
            added.append("figure")
        elif not WP_CLASS.match(token):
            kept.append(token)
    return kept + [c for c in dict.fromkeys(added) if c not in kept]


def _rewrite_start_tag(match, stats):
    tag, raw, closer = match.group(1).lower(), match.group(2), match.group(3)
    if tag == "t" or not raw.strip():
        return match.group(0)
    changed, parts = False, []
    for name, value, quote in _attributes(raw):
        lower = name.lower()
        if lower == "style":
            stats["style_attributes_removed"] += 1
            changed = True
            continue
        if lower.startswith("on") and lower[2:].isalpha():
            stats["event_handlers_removed"] += 1
            changed = True
            continue
        if lower == "class" and value is not None:
            tokens = htmllib.unescape(value).split()
            kept = _wordpress_class_tokens(tag, tokens)
            if kept != tokens:
                stats["wordpress_classes_removed"] += sum(1 for t in tokens if WP_CLASS.match(t) or t.startswith("wp2odoo-"))
                changed = True
                if kept:
                    parts.append(f'class="{htmllib.escape(" ".join(kept), quote=True)}"')
                continue
        parts.append(name if value is None else f"{name}={quote}{value}{quote}")
    if not changed:
        return match.group(0)
    return f"<{match.group(1)}{''.join(' ' + part for part in parts)}{' /' if closer else ''}>"


def _replace_emoji(match, stats):
    tag = match.group(0)
    attrs = {name.lower(): value for name, value, _ in _attributes(tag[4:].rstrip("/>"))}
    if not EMOJI_IMAGE.search(attrs.get("src") or ""):
        return tag
    stats["emoji_images_replaced"] += 1
    return htmllib.escape(htmllib.unescape(attrs.get("alt") or ""), quote=False)


def _unwrap_wordpress_anchor(match, stats):
    href = next((value for name, value, _ in _attributes(match.group(1)) if name.lower() == "href"), None)
    if href and WORDPRESS_LINK.search(htmllib.unescape(href)):
        stats["wordpress_links_unwrapped"] += 1
        return match.group(2)
    return match.group(0)


def prune_blank_elements(markup, stats=None):
    """Removes paragraphs and wrappers that show nothing (`<p></p>`, `<p>&nbsp;</p>`, `<p><br /></p>`,
    `<p><span> </span></p>`), nested ones included."""
    stats = stats if stats is not None else collections.Counter()
    while True:
        markup, number = _BLANK_ELEMENT.subn("", markup)
        if not number:
            return markup
        stats["empty_elements_removed"] += number


# [@ANCHOR: wp_leftovers:strip_wordpress_leftovers]
# Verified by [@ANCHOR: test_wp_leftovers:strip_wordpress_leftovers]
def strip_wordpress_leftovers(markup, stats=None):
    """Removes what WordPress left in stored HTML (or in the body of a QWeb arch): injected scripts, `<!-- wp:... -->`
    block comments, `[caption]` `[gallery]` `[embed]` shortcodes, `wp-*` `size-*` `align*` `has-*` classes (alignment is
    translated to Bootstrap's), inline `style` and `on*` attributes, emoji images (replaced by the character), links
    into wp-admin/wp-login/wp-json/attachment pages (the text stays) and the empty paragraphs these leave. Idempotent."""
    stats = stats if stats is not None else collections.Counter()
    if not markup or not isinstance(markup, str):
        return markup
    text, number = _SCRIPT_BLOCK.subn("", markup)
    stats["scripts_removed"] += number
    text, number = _WP_COMMENT.subn("", text)
    stats["wordpress_comments_removed"] += number
    text = expand_shortcodes(text, stats)
    text = _EMOJI_TAG.sub(lambda m: _replace_emoji(m, stats), text)
    text = _ANCHOR.sub(lambda m: _unwrap_wordpress_anchor(m, stats), text)
    text = _START_TAG.sub(lambda m: _rewrite_start_tag(m, stats), text)
    return prune_blank_elements(text, stats)


# [@ANCHOR: wp_leftovers:remove_references]
# Verified by [@ANCHOR: test_wp_leftovers:remove_references]
def remove_references(markup, patterns, stats=None):
    """Removes the links and pictures that point at files the site does not have. `patterns` are regular
    expressions matched inside a `src` or `href` value. A `<figure>` holding such a reference goes whole
    (picture and caption); a link around a picture goes with the picture; a text link loses the link and keeps
    its text; a bare picture goes. The text around them stays. Idempotent: once removed they are not there."""
    stats = stats if stats is not None else collections.Counter()
    if not markup or not patterns:
        return markup
    alternatives = "|".join(f"(?:{p})" for p in patterns)
    reference = re.compile(r"""\b(?:src|href)\s*=\s*(?:"[^"]*(?:%s)[^"]*"|'[^']*(?:%s)[^']*')""" % (alternatives, alternatives), re.I)

    def figure(match):
        if reference.search(match.group(0)):
            stats["figures_removed"] += 1
            return ""
        return match.group(0)

    def anchor(match):
        if not reference.search(match.group(1)):
            return match.group(0)
        if re.search(r"<img\b", match.group(2), re.I):
            stats["linked_pictures_removed"] += 1
            return ""
        stats["links_unwrapped"] += 1
        return match.group(2)

    def picture(match):
        if reference.search(match.group(0)):
            stats["pictures_removed"] += 1
            return ""
        return match.group(0)

    text = _FIGURE.sub(figure, markup)
    text = _ANCHOR.sub(anchor, text)
    text = _EMOJI_TAG.sub(picture, text)
    return prune_blank_elements(text, stats)


# The shape page_arch() gives a page: everything before the body (layout call, container, <h1>), the body, and
# the closing tags. A page edited in the website builder no longer has it and is left alone.
_PAGE_ARCH = re.compile(
    r"^(?P<head>.*?<div class=\"col-lg-10 offset-lg-1\">\s*<h1>.*?</h1>)(?P<body>.*)"
    r"(?P<tail></div></div></div></section></div></t></t>\s*)$", re.S)


def split_page_arch(arch):
    """(head, body, tail) of a page arch written by page_arch(), or None."""
    match = _PAGE_ARCH.match(arch or "")
    return (match.group("head"), match.group("body"), match.group("tail")) if match else None
