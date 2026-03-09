"""CLI entry point for digest-pipeline."""
import sys


def main():
    """Run digest pipeline, then podcast (podcast failure is non-fatal).

    Usage:
      digest-pipeline /path/to/config.json [--dry-run]
      digest-pipeline /path/to/config.json --digest-only [--dry-run]
      digest-pipeline /path/to/config.json --podcast-only [--dry-run] [date]
    """
    args = sys.argv[1:]

    if not args or args[0] in ("-h", "--help"):
        print(main.__doc__.strip())
        sys.exit(0)

    digest_only = "--digest-only" in args
    podcast_only = "--podcast-only" in args

    if digest_only and podcast_only:
        print("Cannot use both --digest-only and --podcast-only")
        sys.exit(1)

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
