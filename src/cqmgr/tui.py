"""Late-imported Textual entry-point seam."""


def run() -> None:
    """Import Textual only after bootstrap confirms an interactive terminal."""
    from textual.app import App  # noqa: PLC0415

    App().run()
