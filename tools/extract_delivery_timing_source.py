"""Read the agreed result workbook only to recover original due dates and speeds.

Run with the bundled spreadsheet runtime; openpyxl is not a service dependency.
"""

import argparse
from collections import defaultdict
from decimal import Decimal
from hashlib import sha256
import json


def main():
    import openpyxl

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    with open(args.input, "rb") as stream:
        digest = sha256(stream.read()).hexdigest()
    book = openpyxl.load_workbook(args.input, read_only=True, data_only=True)
    grouped = defaultdict(list)
    try:
        sheet = book["Sheet"]
        rows = sheet.iter_rows(values_only=True)
        header = next(rows)
        columns = {key: header.index(key) for key in ("订单号", "是否虚拟订单", "欠交量", "宽度", "厚度", "交货日期", "炉区速度", "速度")}
        def number(value):
            return None if value is None or value == "" else Decimal(str(value))
        for index, row in enumerate(rows, 2):
            key = row[columns["订单号"]]
            if key is None or row[columns["是否虚拟订单"]] == "是":
                continue
            if row[columns["是否虚拟订单"]] != "否":
                raise ValueError(f"row {index}: unknown virtual flag")
            grouped[str(key).strip()].append(dict(
                row=index, weight=number(row[columns["欠交量"]]),
                width=number(row[columns["宽度"]]), thickness=number(row[columns["厚度"]]),
                due_date=row[columns["交货日期"]],
                furnace_speed_mpm=number(row[columns["炉区速度"]]),
                process_speed_mpm=number(row[columns["速度"]]),
            ))
    finally:
        book.close()
    orders = []
    for key, rows in grouped.items():
        first = {name: value for name, value in rows[0].items() if name not in ("row", "weight")}
        if any(any(row[name] != value for name, value in first.items()) for row in rows):
            raise ValueError(f"{key}: conflicting split-row specifications or timing")
        orders.append(dict(source_order_id=key, **first, weight=sum(row["weight"] for row in rows), source_rows=[row["row"] for row in rows]))
    data = dict(source_sha256=digest, sheet="Sheet", source_kind="original_fields_join_only", orders=orders)
    with open(args.output, "x", encoding="utf-8") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2, default=str, allow_nan=False)
    print(f"extracted {len(orders)} original orders; source_sha256={digest}")


if __name__ == "__main__":
    main()
