"""CLI entry point for digest-pipeline."""
import sys


def main():
    """Run digest pipeline, then podcast (podcast failure is non-fatal).

    Usage:
      digest-pipeline /path/to/config.json [--dry-run]
      digest-pipeline /path/to/config.json --digest-only [--dry-run]
      digest-pipeline /path/to/config.json --podcast-only [--dry-run] [date]
      digest-pipeline /path/to/config.json --backfill
      digest-pipeline --backfill-source-history [--digests-dir DIR] [--digest SLUG]
      digest-pipeline --backfill-titles [--digests-dir DIR] [--digest SLUG] [--force] [--dry-run]
      digest-pipeline --audit-sources [--digests-dir DIR] [--digest SLUG] [--dry-run | --json]
      digest-pipeline --discover-twitter [--digests-dir DIR] [--digest SLUG] [--dry-run] [--json]
      digest-pipeline --add-twitter-account HANDLE [HANDLE ...] --digest SLUG [--digests-dir DIR] [--dry-run]
      digest-pipeline /path/to/config.json --subscribe user@example.com
      digest-pipeline /path/to/config.json --unsubscribe user@example.com
      digest-pipeline /path/to/config.json --list-subscribers
      digest-pipeline --serve [--digests-dir DIR] [--port PORT]
      digest-pipeline /path/to/config.json --serve [--port PORT]
      digest-pipeline --console [--digests-dir DIR] [--port PORT]
      digest-pipeline --podcast-stats [--digests-dir DIR]
      digest-pipeline init
    """
    args = sys.argv[1:]

    if not args or args[0] in ("-h", "--help"):
        print(main.__doc__.strip())
        sys.exit(0)

    # Init wizard (no config required)
    if args[0] == "init":
        from .init import run_init
        run_init()
        return

    backfill = "--backfill" in args
    digest_only = "--digest-only" in args
    podcast_only = "--podcast-only" in args

    # Subscriber management commands
    if "--subscribe" in args:
        _run_subscriber_cmd(args, "subscribe")
        return
    if "--unsubscribe" in args:
        _run_subscriber_cmd(args, "unsubscribe")
        return
    if "--list-subscribers" in args:
        _run_subscriber_cmd(args, "list")
        return

    # Subscription API server
    if "--serve" in args:
        _run_serve(args)
        return

    # Management console
    if "--console" in args:
        _run_console(args)
        return

    if "--podcast-stats" in args:
        _run_podcast_stats(args)
        return

    if "--backfill-source-history" in args:
        _run_backfill_source_history(args)
        return

    if "--backfill-titles" in args:
        _run_backfill_titles(args)
        return

    if "--audit-sources" in args:
        _run_source_audit(args)
        return

    if "--discover-twitter" in args:
        _run_discover_twitter(args)
        return

    if "--add-twitter-account" in args:
        _run_add_twitter_account(args)
        return

    if digest_only and podcast_only:
        print("Cannot use both --digest-only and --podcast-only")
        sys.exit(1)

    if backfill:
        _run_backfill(args)
        return

    # Strip our flags before forwarding to subcommands
    forward_args = [a for a in args if a not in ("--digest-only", "--podcast-only")]

    from .digest import main as digest_main
    from .podcast import main as podcast_main

    if not podcast_only:
        # Build sys.argv for digest.main()
        sys.argv = ["digest-pipeline"] + forward_args
        digest_main()

    if not digest_only:
        sys.argv = ["digest-pipeline"] + forward_args
        try:
            podcast_main()
        except SystemExit as e:
            if e.code and e.code != 0:
                print("Podcast generation failed (non-fatal)")
        except Exception as e:
            print(f"Podcast generation failed (non-fatal): {e}")


def _run_subscriber_cmd(args, action):
    """Handle --subscribe, --unsubscribe, and --list-subscribers commands."""
    from .config import load_config
    from .subscribers import add_subscriber, remove_subscriber, list_subscribers

    config_path = next((a for a in args if not a.startswith("--")), None)
    if not config_path:
        print("Error: config path required")
        sys.exit(1)

    config = load_config(config_path)
    data_root = config["_data_root"]

    if action == "list":
        subs = list_subscribers(data_root)
        if not subs:
            print("No subscribers.")
        else:
            print(f"{len(subs)} subscriber(s):")
            for s in subs:
                print(f"  {s['email']}  (token: {s['token']})")
        return

    # Get email argument (the value after --subscribe or --unsubscribe)
    flag = f"--{action}"
    try:
        idx = args.index(flag)
        email = args[idx + 1]
    except (ValueError, IndexError):
        print(f"Error: {flag} requires an email address")
        sys.exit(1)

    if action == "subscribe":
        added, msg = add_subscriber(data_root, email, source="cli")
    else:
        removed, msg = remove_subscriber(data_root, email=email)

    print(msg)


def _run_serve(args):
    """Start the subscription API server."""
    from pathlib import Path
    from .subscription_api import create_app

    # Parse --port
    port = 5100
    if "--port" in args:
        try:
            idx = args.index("--port")
            port = int(args[idx + 1])
        except (ValueError, IndexError):
            print("Error: --port requires a number")
            sys.exit(1)

    # Parse --digests-dir
    digests_dir = None
    if "--digests-dir" in args:
        try:
            idx = args.index("--digests-dir")
            digests_dir = args[idx + 1]
        except (ValueError, IndexError):
            print("Error: --digests-dir requires a path")
            sys.exit(1)

    config_path = next((a for a in args
                        if not a.startswith("--") and a not in (digests_dir, str(port))),
                       None)

    if digests_dir:
        app = create_app(digests_dir=digests_dir)
    elif config_path:
        # Backward compat: single config mode
        from .config import load_config
        config = load_config(config_path)
        if "--port" not in args:
            port = config.get("subscriptions", {}).get("port", 5100)
        app = create_app(config_path=config_path)
    else:
        # Auto-discover: look for digests/ relative to the package
        default_dir = Path(__file__).resolve().parent.parent / "digests"
        if default_dir.is_dir():
            app = create_app(digests_dir=str(default_dir))
        else:
            print("Error: no config or --digests-dir provided, and no digests/ directory found")
            sys.exit(1)

    print(f"Starting subscription API on http://127.0.0.1:{port}")
    app.run(host="127.0.0.1", port=port)


def _run_podcast_stats(args):
    """Generate cached podcast stats artifacts for one or all digests."""
    from pathlib import Path
    from .config import load_config
    from .podcast_stats import write_podcast_metrics

    config_path = next((a for a in args if not a.startswith("--")), None)
    if config_path and Path(config_path).exists() and config_path.endswith(".json"):
        cfg = load_config(config_path)
        stats, downloads = write_podcast_metrics(cfg, cfg["_data_root"])
        print(f"Updated podcast stats for {cfg['digest']['name']}: {stats.get('estimated_subscribers', 0)} subscribers, {len((downloads.get('episodes') or {}))} episodes")
        return

    digests_dir = None
    if "--digests-dir" in args:
        try:
            idx = args.index("--digests-dir")
            digests_dir = args[idx + 1]
        except (ValueError, IndexError):
            print("Error: --digests-dir requires a path")
            sys.exit(1)
    if not digests_dir:
        candidate = Path(__file__).resolve().parent.parent / "digests"
        if candidate.exists():
            digests_dir = str(candidate)
        else:
            print("Error: provide --digests-dir or a config path")
            sys.exit(1)

    configs = sorted(Path(digests_dir).glob("*/config.json"))
    if not configs:
        print(f"No digest configs found in {digests_dir}")
        return

    for cfg_file in configs:
        cfg = load_config(str(cfg_file))
        stats, downloads = write_podcast_metrics(cfg, cfg["_data_root"])
        print(f"Updated {cfg['digest']['name']}: {stats.get('estimated_subscribers', 0)} subscribers, {len((downloads.get('episodes') or {}))} episodes")


def _run_console(args):
    """Start the management console server."""
    from pathlib import Path
    from .console_api import create_app

    # Parse --port
    port = 5200
    if "--port" in args:
        try:
            idx = args.index("--port")
            port = int(args[idx + 1])
        except (ValueError, IndexError):
            print("Error: --port requires a number")
            sys.exit(1)

    # Parse --host
    host = "127.0.0.1"
    if "--host" in args:
        try:
            idx = args.index("--host")
            host = args[idx + 1]
        except (ValueError, IndexError):
            print("Error: --host requires an address")
            sys.exit(1)

    # Parse --digests-dir
    digests_dir = None
    if "--digests-dir" in args:
        try:
            idx = args.index("--digests-dir")
            digests_dir = args[idx + 1]
        except (ValueError, IndexError):
            print("Error: --digests-dir requires a path")
            sys.exit(1)

    if digests_dir:
        app = create_app(digests_dir=digests_dir)
    else:
        # Auto-discover: look for digests/ relative to the package
        default_dir = Path(__file__).resolve().parent.parent / "digests"
        if default_dir.is_dir():
            app = create_app(digests_dir=str(default_dir))
        else:
            print("Error: no --digests-dir provided, and no digests/ directory found")
            sys.exit(1)

    print(f"Starting management console on http://{host}:{port}")
    app.run(host=host, port=port)


def _run_backfill_source_history(args):
    """Rebuild source_history.jsonl by walking each digest's work/<date>/ artifacts."""
    import logging
    import re
    from pathlib import Path
    from .config import load_config
    from . import source_history

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    digests_dir = None
    if "--digests-dir" in args:
        try:
            idx = args.index("--digests-dir")
            digests_dir = args[idx + 1]
        except (ValueError, IndexError):
            print("Error: --digests-dir requires a path")
            sys.exit(1)
    if not digests_dir:
        candidate = Path(__file__).resolve().parent.parent / "digests"
        if candidate.is_dir():
            digests_dir = str(candidate)
        else:
            print("Error: provide --digests-dir or run from a checkout with digests/")
            sys.exit(1)

    target_slug = None
    if "--digest" in args:
        try:
            idx = args.index("--digest")
            target_slug = args[idx + 1]
        except (ValueError, IndexError):
            print("Error: --digest requires a slug")
            sys.exit(1)

    date_re = re.compile(r"^\d{4}-\d{2}-\d{2}$")
    configs = sorted(Path(digests_dir).glob("*/config.json"))
    if not configs:
        print(f"No digest configs found in {digests_dir}")
        return

    for cfg_path in configs:
        slug = cfg_path.parent.name
        if target_slug and slug != target_slug:
            continue
        cfg = load_config(str(cfg_path))
        data_root = cfg["_data_root"]
        total = source_history.rebuild(data_root, date_re)
        print(f"  {slug}: wrote {total} rows -> {data_root / source_history.LEDGER_FILENAME}")


def _run_backfill_titles(args):
    """Generate one-line episode titles for past episodes missing a .title file.

    For each digest, every podcast script (`podcasts/<date>.txt`) without a
    matching `<date>.title` gets a title generated from that day's digest
    markdown (preferred) or the script itself. Afterwards the RSS feed and
    landing page are rebuilt so the new titles take effect.
    """
    import logging
    import re
    from pathlib import Path
    from .config import load_config
    from . import podcast as podcast_mod

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logger = logging.getLogger("backfill-titles")

    force = "--force" in args
    dry_run = "--dry-run" in args

    digests_dir = None
    if "--digests-dir" in args:
        try:
            idx = args.index("--digests-dir")
            digests_dir = args[idx + 1]
        except (ValueError, IndexError):
            print("Error: --digests-dir requires a path")
            sys.exit(1)
    if not digests_dir:
        candidate = Path(__file__).resolve().parent.parent / "digests"
        if candidate.is_dir():
            digests_dir = str(candidate)
        else:
            print("Error: provide --digests-dir or run from a checkout with digests/")
            sys.exit(1)

    target_slug = None
    if "--digest" in args:
        try:
            idx = args.index("--digest")
            target_slug = args[idx + 1]
        except (ValueError, IndexError):
            print("Error: --digest requires a slug")
            sys.exit(1)

    date_re = re.compile(r"^\d{4}-\d{2}-\d{2}$")
    configs = sorted(Path(digests_dir).glob("*/config.json"))
    if not configs:
        print(f"No digest configs found in {digests_dir}")
        return

    for cfg_path in configs:
        slug = cfg_path.parent.name
        if target_slug and slug != target_slug:
            continue
        cfg = load_config(str(cfg_path))
        if not cfg.get("podcast", {}).get("enabled", True):
            continue
        data_root = cfg["_data_root"]
        podcasts_dir = data_root / "podcasts"
        if not podcasts_dir.is_dir():
            continue

        made = 0
        for script in sorted(podcasts_dir.glob("*.txt")):
            ep_date = script.stem
            if not date_re.match(ep_date):
                continue
            title_file = podcasts_dir / f"{ep_date}.title"
            if title_file.exists() and not force:
                continue
            digest_md = data_root / f"{ep_date}.md"
            source = (digest_md.read_text(errors="replace") if digest_md.is_file()
                      else script.read_text(errors="replace"))
            title = podcast_mod._generate_title(source, cfg, logger)
            if not title:
                print(f"  {slug} {ep_date}: FAILED to generate")
                continue
            print(f"  {slug} {ep_date}: {title}")
            if not dry_run:
                title_file.write_text(title + "\n")
                made += 1

        if made:
            podcast_mod._update_rss_feed(podcasts_dir, "", {}, cfg, logger)
            podcast_mod._update_landing_page(podcasts_dir, cfg, logger)
            print(f"  {slug}: wrote {made} titles, rebuilt feed + landing page")
        elif dry_run:
            print(f"  {slug}: dry run — no files written")


def _run_source_audit(args):
    """Classify sources by health/cause and either print or push the report."""
    import json
    import logging
    from pathlib import Path
    from .config import load_config
    from . import delivery, source_audit, twitter_discovery

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    dry_run = "--dry-run" in args
    json_mode = "--json" in args
    if dry_run and json_mode:
        print("Error: --dry-run and --json are mutually exclusive")
        sys.exit(1)
    digests_dir = None
    if "--digests-dir" in args:
        try:
            idx = args.index("--digests-dir")
            digests_dir = args[idx + 1]
        except (ValueError, IndexError):
            print("Error: --digests-dir requires a path")
            sys.exit(1)
    if not digests_dir:
        candidate = Path(__file__).resolve().parent.parent / "digests"
        if candidate.is_dir():
            digests_dir = str(candidate)
        else:
            print("Error: provide --digests-dir or run from a checkout with digests/")
            sys.exit(1)

    target_slug = None
    if "--digest" in args:
        try:
            idx = args.index("--digest")
            target_slug = args[idx + 1]
        except (ValueError, IndexError):
            print("Error: --digest requires a slug")
            sys.exit(1)

    configs = sorted(Path(digests_dir).glob("*/config.json"))
    if not configs:
        print(f"No digest configs found in {digests_dir}")
        return

    json_payload: dict = {}
    for cfg_path in configs:
        slug = cfg_path.parent.name
        if target_slug and slug != target_slug:
            continue
        cfg = load_config(str(cfg_path))
        data_root = cfg["_data_root"]
        report = source_audit.audit(data_root, cfg)

        if json_mode:
            json_payload[slug] = json.loads(source_audit.to_json(report))
            continue

        message = source_audit.format_telegram(report)
        actionable = len(report.actionable)
        print(f"=== {slug}: {actionable} actionable findings, "
              f"{report.healthy_count}/{report.total_sources} healthy ===")

        disc_report = twitter_discovery.discover(cfg)
        disc_message = twitter_discovery.format_telegram(disc_report)
        if disc_message:
            message = _combine_with_cap(message, disc_message)
            print(f"    + {len(disc_report.candidates)} suggested new Twitter accounts")

        if dry_run:
            print(message)
            print()
        else:
            success = delivery.send_notification(message, cfg)
            if not success:
                print(f"[{slug}] Telegram delivery failed; falling back to stdout:")
                print(message)

    if json_mode:
        print(json.dumps(json_payload, indent=2))


def _combine_with_cap(primary: str, secondary: str, cap: int = 3800) -> str:
    """Join two messages, truncating the secondary first if needed."""
    sep = "\n\n"
    budget_for_secondary = cap - len(primary) - len(sep)
    if budget_for_secondary <= 100:
        return primary
    if len(secondary) > budget_for_secondary:
        secondary = secondary[: budget_for_secondary - 20] + "\n…(truncated)"
    return primary + sep + secondary


def _run_discover_twitter(args):
    """Run the Twitter discovery module standalone.

    --json mode: write the DiscoveryReport as JSON to stdout, suppress
    Telegram delivery, route all log output to stderr. The /discover-twitter
    slash command depends on stdout being valid JSON only.
    """
    import json
    import logging
    from pathlib import Path
    from .config import load_config
    from . import delivery, twitter_discovery

    json_mode = "--json" in args
    log_stream = sys.stderr if json_mode else sys.stdout
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=log_stream)

    def status(msg: str) -> None:
        # Status lines go to stderr in JSON mode so stdout stays clean.
        print(msg, file=log_stream)

    dry_run = "--dry-run" in args
    digests_dir = None
    if "--digests-dir" in args:
        try:
            idx = args.index("--digests-dir")
            digests_dir = args[idx + 1]
        except (ValueError, IndexError):
            print("Error: --digests-dir requires a path", file=sys.stderr)
            sys.exit(1)
    if not digests_dir:
        candidate = Path(__file__).resolve().parent.parent / "digests"
        if candidate.is_dir():
            digests_dir = str(candidate)
        else:
            print("Error: provide --digests-dir or run from a checkout with digests/",
                  file=sys.stderr)
            sys.exit(1)

    target_slug = None
    if "--digest" in args:
        try:
            idx = args.index("--digest")
            target_slug = args[idx + 1]
        except (ValueError, IndexError):
            print("Error: --digest requires a slug", file=sys.stderr)
            sys.exit(1)

    if json_mode and not target_slug:
        print("Error: --json requires --digest <slug>", file=sys.stderr)
        sys.exit(1)

    configs = sorted(Path(digests_dir).glob("*/config.json"))
    if not configs:
        print(f"No digest configs found in {digests_dir}", file=log_stream)
        return

    for cfg_path in configs:
        slug = cfg_path.parent.name
        if target_slug and slug != target_slug:
            continue
        cfg = load_config(str(cfg_path))
        report = twitter_discovery.discover(cfg)

        if json_mode:
            print(json.dumps(report.to_dict(), indent=2))
            return

        message = twitter_discovery.format_telegram(report)
        status(f"=== {slug}: {len(report.candidates)} candidates "
               f"(searched {len(report.keywords_searched)} keywords, "
               f"skipped {report.skipped_existing} existing) ===")
        if not message:
            status(f"[{slug}] discovery disabled or no keywords configured")
            continue
        if dry_run:
            print(message)
            print()
        else:
            success = delivery.send_notification(message, cfg)
            if not success:
                status(f"[{slug}] Telegram delivery failed; falling back to stdout:")
                print(message)


def _run_add_twitter_account(args):
    """Append one or more Twitter handles to a digest's config.json."""
    from pathlib import Path
    from .config_writer import add_twitter_accounts

    dry_run = "--dry-run" in args

    digests_dir = None
    if "--digests-dir" in args:
        try:
            idx = args.index("--digests-dir")
            digests_dir = args[idx + 1]
        except (ValueError, IndexError):
            print("Error: --digests-dir requires a path", file=sys.stderr)
            sys.exit(1)
    if not digests_dir:
        candidate = Path(__file__).resolve().parent.parent / "digests"
        if candidate.is_dir():
            digests_dir = str(candidate)
        else:
            print("Error: provide --digests-dir or run from a checkout with digests/",
                  file=sys.stderr)
            sys.exit(1)

    if "--digest" not in args:
        print("Error: --digest <slug> is required", file=sys.stderr)
        sys.exit(1)
    try:
        idx = args.index("--digest")
        slug = args[idx + 1]
    except (ValueError, IndexError):
        print("Error: --digest requires a slug", file=sys.stderr)
        sys.exit(1)

    # Gather handles: every arg that isn't a known flag or a flag value.
    known_flag_values = {"--digests-dir", "--digest"}
    skip_next = False
    handles: list[str] = []
    for i, a in enumerate(args):
        if skip_next:
            skip_next = False
            continue
        if a == "--add-twitter-account":
            continue
        if a in known_flag_values:
            skip_next = True
            continue
        if a in ("--dry-run",):
            continue
        if a.startswith("--"):
            continue
        handles.append(a)

    if not handles:
        print("Error: provide one or more handles after --add-twitter-account",
              file=sys.stderr)
        sys.exit(1)

    config_path = Path(digests_dir) / slug / "config.json"
    if not config_path.exists():
        print(f"Error: no config at {config_path}", file=sys.stderr)
        sys.exit(1)

    try:
        result = add_twitter_accounts(config_path, handles, dry_run=dry_run)
    except (ValueError, FileNotFoundError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    label = "Would add" if dry_run else "Added"
    if result.added:
        print(f"{label}: {', '.join('@' + h for h in result.added)}")
    else:
        print(f"{label}: (none)")
    if result.already_present:
        print(f"Already present: {', '.join('@' + h for h in result.already_present)}")
    print(f"Config: {result.config_path}")


def _run_backfill(args):
    """Run backfill to populate .seen_embeddings.json from archived digests."""
    import logging
    from .config import load_config
    from . import llm
    from .seen_articles import backfill, simulate_dedup

    # Find config path (first arg that isn't a flag)
    config_path = next((a for a in args if not a.startswith("--")), None)
    if not config_path:
        print("Error: config path required for --backfill")
        sys.exit(1)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    config = load_config(config_path)
    data_root = config["_data_root"]

    provider = config.get("llm", {}).get("provider", "openrouter")
    llm.configure(provider)

    # Step 1: Backfill embeddings
    print("=== Backfilling .seen_embeddings.json ===\n")
    result = backfill(data_root)
    if result:
        for date, count in sorted(result.items()):
            print(f"  {date}: embedded {count} articles")
        print(f"\n  Total: {sum(result.values())} articles across {len(result)} days")
    else:
        print("  Nothing to backfill (all dates already in store)")

    # Step 2: Simulate dedup to show what would have been caught
    print("\n=== Cross-Day Dedup Simulation ===\n")
    sim = simulate_dedup(data_root)
    for date in sorted(sim):
        skipped = sim[date]
        if skipped:
            print(f"  {date}: would skip {len(skipped)} articles:")
            for title in skipped:
                print(f"    - {title}")
        else:
            print(f"  {date}: first day or no repeats")
    print()
