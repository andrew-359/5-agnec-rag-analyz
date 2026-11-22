"""CLI entry point for plot extract."""

import sys

from .strategies.plot_extract import main


if __name__ == "__main__":
    sys.exit(main())
