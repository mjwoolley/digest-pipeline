"""CLI entry point for digest-pipeline."""
import sys


def main():
    """Run digest pipeline, then podcast (podcast failure is non-fatal).

    Usage:
      digest-pipeline /path/to/config.json [--dry-run]
      digest-pipeline /path/to/config.json --digest-only [--dry-run]
      digest-pipeline /path/to/config.json --podcast-only [--dry-run] [date]
      digest-pipeline /path/to/config.json --backfill
      digest-pipeline /path/to/config.json --subscribe user@example.com
      digest-pipeline /path/to/config.json --unsubscribe user@example.com
      digest-pipeline /path/to/config.json --list-subscribers
      digest-pipeline --serve [--digests-dir DIR] [--port PORT]
      digest-pipeline /path/to/config.json --serve [--port PORT]
      digest-pipeline --console [--digests-dir DIR] [--port PORT]
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
    from .archive import git_publish_daily
    from .config import load_config

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

    # Archive & publish: commit daily artifacts to git (after all stages)
    dry_run = "--dry-run" in args
    config_path = next((a for a in args if not a.startswith("--")), None)
    if config_path:
        try:
            config = load_config(config_path)
            git_publish_daily(config, dry_run=dry_run)
        except Exception as e:
            print(f"Archive/publish failed (non-fatal): {e}")


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
