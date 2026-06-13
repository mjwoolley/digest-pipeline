"""Config loading and prompt rendering for the digest pipeline."""
import json
import os
from pathlib import Path


def load_config(config_path: str) -> dict:
    """Load config JSON and resolve data_root from config file's parent dir.

    If the ``DIGEST_ENV`` environment variable is set (e.g. ``staging``), a
    sibling overlay file named ``config.{DIGEST_ENV}.json`` is deep-merged on
    top of the base config. This lets non-production deployments override
    only the fields that differ (public URLs, ports, telegram
    chat id, etc.) without forking the base config. Overlay files are
    gitignored — they are per-deployment artifacts, not source-of-truth.

    Deep-merge semantics: dicts merge recursively; lists and scalars in the
    overlay replace the base value wholesale.

    Args:
        config_path: Path to config.json

    Returns:
        Config dict with added '_data_root' and '_pipeline_dir' keys
    """
    path = Path(config_path).resolve()
    if not path.exists():
        raise FileNotFoundError(f"Config not found: {path}")

    config = json.loads(path.read_text())

    env = os.environ.get("DIGEST_ENV", "").strip()
    if env:
        overlay_path = path.with_name(f"config.{env}.json")
        if overlay_path.exists():
            overlay = json.loads(overlay_path.read_text())
            config = _deep_merge(config, overlay)

    config["_data_root"] = path.parent
    config["_pipeline_dir"] = Path(__file__).resolve().parent

    # Load secrets.env from project root (repo root) if it exists
    secrets_file = config["_pipeline_dir"].parent / "secrets.env"
    if secrets_file.exists():
        for line in secrets_file.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, _, value = line.partition("=")
            if key and value and key not in os.environ:
                os.environ[key] = value

    return config


def _deep_merge(base: dict, overlay: dict) -> dict:
    """Return a new dict with ``overlay`` deep-merged on top of ``base``.

    Dicts merge recursively. Lists and scalar values in the overlay replace
    the base value entirely — there is no list concatenation.
    """
    result = dict(base)
    for key, overlay_value in overlay.items():
        base_value = result.get(key)
        if isinstance(base_value, dict) and isinstance(overlay_value, dict):
            result[key] = _deep_merge(base_value, overlay_value)
        else:
            result[key] = overlay_value
    return result


def render_prompt(template_name: str, config: dict, extra_vars: dict = None) -> str:
    """Load a prompt template and inject config-derived + extra variables.

    Template variables:
        {{CATEGORIES}}     - rendered category list from config
        {{CATEGORY_VALUES}} - pipe-separated category IDs
        {{SECTIONS}}       - rendered section headers from config
        {{DIGEST_HEADER}}  - "emoji TAGLINE — date"
        {{TONE}}           - digest tone
        {{PODCAST_NAME}}   - podcast name
        {{HOSTS}}          - rendered host descriptions
        + any extra_vars passed in
    """
    prompts_dir = config["_pipeline_dir"] / "prompts"
    template = (prompts_dir / template_name).read_text()

    # Build config-derived variables
    categories = config.get("categories", [])
    digest = config.get("digest", {})
    podcast = config.get("podcast", {})

    config_vars = {
        "CATEGORIES": _render_categories(categories),
        "CATEGORY_VALUES": "|".join(c["id"] for c in categories),
        "SECTIONS": _render_sections(categories),
        "DIGEST_HEADER": f"{digest.get('emoji', '📰')} {digest.get('tagline', 'DIGEST')}",
        "TONE": digest.get("tone", ""),
        "PODCAST_NAME": podcast.get("name", "The AI Daily Roundup"),
        "HOSTS": _render_hosts(podcast.get("hosts", [])),
    }

    # Merge extra_vars (they override config_vars)
    if extra_vars:
        config_vars.update(extra_vars)

    # Replace template variables
    for key, value in config_vars.items():
        template = template.replace(f"{{{{{key}}}}}", value)

    return template


def get_voice_map(config: dict, backend: str) -> dict:
    """Build speaker_tag -> voice_id mapping from config.

    Args:
        config: Full config dict
        backend: TTS backend name (e.g. "kokoro")

    Returns:
        Dict like {"ALEX": "am_michael", "SARAH": "af_heart"}
    """
    hosts = config.get("podcast", {}).get("hosts", [])
    voice_key = f"voice_{backend}"
    return {h["tag"]: h[voice_key] for h in hosts if voice_key in h}


def get_speaker_tags(config: dict) -> list[str]:
    """Get list of valid speaker tags from config."""
    return [h["tag"] for h in config.get("podcast", {}).get("hosts", [])]


def _render_categories(categories: list[dict]) -> str:
    """Render categories as markdown list for prompts."""
    lines = []
    for c in categories:
        lines.append(f"- **{c['id']}**: {c['description']}")
    return "\n".join(lines)


def _render_sections(categories: list[dict]) -> str:
    """Render section headers for the format prompt."""
    lines = []
    for c in categories:
        lines.append(f"**{c['emoji']} {c['label']}**\n[{c['id']} items]")
    return "\n\n".join(lines)


def _render_hosts(hosts: list[dict]) -> str:
    """Render host descriptions for podcast prompt."""
    lines = []
    for h in hosts:
        lines.append(f"- **{h['tag']}**: {h['role']}")
    return "\n".join(lines)
