#!/usr/bin/env python3
"""Refresh analytical exports without rerunning extraction or model calls."""
from __future__ import annotations

from collector.collect import connect
from processor.pipeline import export_data


def main() -> None:
    db = connect()
    export_data(db)
    db.close()
    print("processed exports refreshed")


if __name__ == "__main__":
    main()
