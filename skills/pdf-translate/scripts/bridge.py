#!/usr/bin/env python3
"""Cross-platform CLITranslator bridge for pdf2zh_next ('dump' and 'replay')."""
import hashlib
import json
import os
import sys


def _lock_file(f):
    if os.name == "nt":
        import msvcrt
        f.seek(0, os.SEEK_END)
        msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)
    else:
        import fcntl
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)


def _unlock_file(f):
    if os.name == "nt":
        import msvcrt
        f.seek(0, os.SEEK_END)
        try:
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
    else:
        import fcntl
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def main():
    if len(sys.argv) != 3 or sys.argv[1] not in ("dump", "replay"):
        print("Usage: bridge.py <dump|replay> <data_path>", file=sys.stderr)
        sys.exit(2)

    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    mode = sys.argv[1]
    data_path = sys.argv[2]
    text = sys.stdin.read()
    key = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]

    if mode == "dump":
        record = json.dumps({"id": key, "text": text}, ensure_ascii=False) + "\n"
        with open(data_path, "a", encoding="utf-8") as f:
            _lock_file(f)
            f.write(record)
            f.flush()
            _unlock_file(f)
        sys.stdout.write(text)
    else:
        with open(data_path, "r", encoding="utf-8") as f:
            translations = json.load(f)
        translated = translations.get(key)
        if translated is None:
            print(f"[bridge] WARNING: missing key {key}", file=sys.stderr)
            sys.stdout.write(text)
        else:
            sys.stdout.write(translated)


if __name__ == "__main__":
    main()
