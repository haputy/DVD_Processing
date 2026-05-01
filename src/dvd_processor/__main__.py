import sys
from dvd_processor.cli import main

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "gui":
        from dvd_processor.gui import run
        sys.argv.pop(1)
        run()
    else:
        main()
