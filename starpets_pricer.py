import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from starpets_relist import price_checker_main, c, C

if __name__ == "__main__":
    try:
        price_checker_main()
    except KeyboardInterrupt:
        print(c(C.GREY, "\nPrice checker stopped."))
    except Exception:
        import traceback
        print("\nCRASH:\n" + traceback.format_exc())
        input("Press Enter to exit...")
