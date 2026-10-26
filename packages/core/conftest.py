"""pytest options shared by every test directory."""


def pytest_addoption(parser):
    parser.addoption(
        "--update-golden",
        action="store_true",
        default=False,
        help="rewrite tests/cli/golden/ from the current output instead of comparing against it",
    )
