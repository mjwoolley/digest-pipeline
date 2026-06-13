"""Interactive wizard for creating a new digest directory."""
import json
import re
import shutil
from pathlib import Path


def _slugify(name: str) -> str:
    """Convert a display name to a URL-safe slug."""
    slug = name.lower().strip()
    slug = re.sub(r'[^a-z0-9]+', '-', slug)
    return slug.strip('-')


def _prompt(label: str, default: str = "") -> str:
    """Prompt user for input with an optional default."""
    if default:
        raw = input(f"  {label} [{default}]: ").strip()
        return raw or default
    while True:
        raw = input(f"  {label}: ").strip()
        if raw:
            return raw
        print("    (required)")


def _prompt_yn(label: str, default: bool = True) -> bool:
    """Prompt for yes/no."""
    hint = "Y/n" if default else "y/N"
    raw = input(f"  {label} [{hint}]: ").strip().lower()
    if not raw:
        return default
    return raw.startswith("y")


def _build_hosts_html(hosts: list[dict]) -> str:
    """Build the hosts HTML block for the landing page."""
    if not hosts:
        return ""
    cards = []
    for h in hosts:
        cards.append(
            f'  <div class="host">\n'
            f'    <div class="name">{h["tag"].title()}</div>\n'
            f'    <div class="role">{h["role"]}</div>\n'
            f'  </div>'
        )
    return (
        '<div class="hosts">\n'
        + "\n".join(cards) + "\n"
        + '</div>'
    )


def run_init():
    """Run the interactive digest init wizard."""
    digests_dir = Path(__file__).resolve().parent.parent / "digests"

    print("\n=== New Digest Setup ===\n")

    # Basic info
    name = _prompt("Display name (e.g. 'The AI Daily Roundup')")
    slug = _prompt("Slug (directory name)", _slugify(name))
    tagline = _prompt("Tagline (short description for the hero section)")
    emoji = _prompt("Emoji", "📰")
    domain = _prompt("Domain (e.g. 'aidailyroundup.com')")

    # Brand image
    brand_src = _prompt("Path to brand image (PNG/JPG)")
    brand_path = Path(brand_src).expanduser().resolve()
    if not brand_path.exists():
        print(f"    Warning: {brand_path} does not exist. You can copy it manually later.")
        brand_path = None

    # Categories
    print("\n  Categories (enter comma-separated IDs, or 'default' for a starter set):")
    print("    Default set: general, industry, tools, research, other")
    cat_input = _prompt("Categories", "default")
    if cat_input == "default":
        categories = [
            {"id": "general", "label": "General", "emoji": "📰",
             "description": "Primary news and developments"},
            {"id": "industry", "label": "Industry & Business", "emoji": "💼",
             "description": "Market moves, funding, partnerships, strategy"},
            {"id": "tools", "label": "Tools & Products", "emoji": "🛠️",
             "description": "New tools, platforms, and product launches"},
            {"id": "research", "label": "Research & Analysis", "emoji": "🔬",
             "description": "Papers, technical approaches, deep analysis"},
            {"id": "other", "label": "Other", "emoji": "🌐",
             "description": "Anything that doesn't fit the above categories"},
        ]
    else:
        categories = []
        for cat_id in [c.strip() for c in cat_input.split(",") if c.strip()]:
            label = _prompt(f"    Label for '{cat_id}'", cat_id.replace("-", " ").title())
            cat_emoji = _prompt(f"    Emoji for '{cat_id}'", "📌")
            desc = _prompt(f"    Description for '{cat_id}'")
            categories.append({"id": cat_id, "label": label, "emoji": cat_emoji,
                               "description": desc})

    # Email delivery
    print("\n  Email delivery:")
    email_from = _prompt("From address (e.g. 'digest@yourdomain.com')", f"digest@{domain}")

    # Podcast hosts
    print("\n  Podcast hosts:")
    hosts = []
    if _prompt_yn("Configure podcast hosts?", default=True):
        while True:
            tag = _prompt("    Host name/tag (e.g. 'Alex')").upper()
            role = _prompt(f"    Role description for {tag}")
            voice = _prompt(f"    Kokoro voice for {tag}", "am_michael" if len(hosts) == 0 else "af_heart")
            hosts.append({"tag": tag, "role": role, "voice_kokoro": voice})
            if not _prompt_yn("    Add another host?", default=len(hosts) < 2):
                break

    # Build config
    config = {
        "digest": {
            "name": name,
            "tagline": slug.upper().replace("-", " ") + " DIGEST",
            "emoji": emoji,
            "tone": "informed, direct, slightly opinionated",
            "max_articles": 20,
        },
        "categories": categories,
        "podcast": {
            "enabled": bool(hosts),
            "name": name,
            "image_url": f"https://{domain}/brand.png",
            "pages_base_url": f"https://{domain}",
            "hosts": hosts,
            "tts_backend": "kokoro",
        },
        "delivery": {
            "email": {
                "method": "resend",
                "resend": {
                    "api_key_env": "RESEND_API_KEY",
                    "from": f"{name} <{email_from}>",
                },
            },
            "notify": {
                "method": "telegram",
                "telegram": {
                    "bot_token_env": "TELEGRAM_BOT_TOKEN",
                    "chat_id_env": "TELEGRAM_CHAT_ID",
                },
            },
        },
        "subscriptions": {
            "public_base_url": f"https://{domain}",
            "cors_origins": [f"https://{domain}"],
            "port": 5100,
        },
        "llm": {
            "provider": "openrouter",
        },
        "sources": {
            "blogs": {},
        },
    }

    # Create directory and files
    digest_dir = digests_dir / slug
    digest_dir.mkdir(parents=True, exist_ok=True)

    # config.json
    config_path = digest_dir / "config.json"
    config_path.write_text(json.dumps(config, indent=2) + "\n")
    print(f"\n  Created {config_path}")

    # subscribers.json
    subs_path = digest_dir / "subscribers.json"
    subs_path.write_text(json.dumps({"subscribers": []}, indent=2) + "\n")
    print(f"  Created {subs_path}")

    # Brand image
    brand_dest = digest_dir / "brand.png"
    if brand_path and brand_path.exists():
        shutil.copy2(brand_path, brand_dest)
        print(f"  Copied brand image to {brand_dest}")
    else:
        print(f"  Brand image: copy your image to {brand_dest}")

    # Landing page from template
    template_path = Path(__file__).resolve().parent / "templates" / "landing_page.html"
    if template_path.exists():
        template = template_path.read_text()
        podcast_feed_url = f"https://{domain}/podcast.xml"
        about_text = (
            f"{name} is an AI-generated digest and podcast. "
            f"Content is gathered from curated sources, processed through an LLM pipeline, "
            f"and delivered as a daily email and podcast."
        )
        hosts_html = _build_hosts_html(hosts)

        html = (template
                .replace("{{DIGEST_NAME}}", name)
                .replace("{{TAGLINE}}", tagline)
                .replace("{{BRAND_IMAGE}}", "brand.png")
                .replace("{{PODCAST_FEED_URL}}", podcast_feed_url)
                .replace("{{ABOUT_TEXT}}", about_text)
                .replace("{{HOSTS_HTML}}", hosts_html))

        index_path = digest_dir / "index.html"
        index_path.write_text(html)
        print(f"  Created {index_path}")

    # Next steps
    print(f"""
=== Next Steps ===

1. Add sources to {config_path}
   (twitter accounts, blog feeds, newsletters, etc.)

2. Set up DNS: point {domain} A record to 178.156.161.33

3. Add a Caddy block to /etc/caddy/Caddyfile:

   {domain} {{
       bind 178.156.161.33
       root * {digest_dir}
       file_server

       handle /api/* {{
           reverse_proxy 127.0.0.1:5100 {{
               header_up X-Digest {slug}
           }}
       }}
       handle /unsubscribe {{
           reverse_proxy 127.0.0.1:5100 {{
               header_up X-Digest {slug}
           }}
       }}
   }}

4. Restart Caddy: sudo systemctl restart caddy

5. Run your first digest: digest-pipeline {config_path}
""")
