"""One command starts the entire app and owns every child process. No automatic live connection."""
from integration.launcher import main

if __name__ == '__main__':
    raise SystemExit(main())
