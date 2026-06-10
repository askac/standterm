import argparse
import importlib
import sys
import traceback
import warnings


REQUIRED_MODULES = [
    "flask",
    "flask_socketio",
    "simple_websocket",
    "paramiko",
    "eventlet",
    "cryptography",
    "serial",
]

if sys.platform == "win32":
    REQUIRED_MODULES.append("winpty")
else:
    REQUIRED_MODULES.append("ptyprocess")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    warnings.filterwarnings("ignore", message=r"\s*Eventlet is deprecated.*")

    ok = True
    for module_name in REQUIRED_MODULES:
        try:
            importlib.import_module(module_name)
            if not args.quiet:
                print(f"OK {module_name}")
        except Exception:
            ok = False
            if not args.quiet:
                print(f"FAIL {module_name}")
                traceback.print_exc()

    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
