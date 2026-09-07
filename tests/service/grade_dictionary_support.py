import sqlite3

from apsgo_v7_service.grade_dictionary import (
    GradeDictionaryEntry,
    GradeDictionarySnapshot,
    normalize_grade,
)


def sample_grade_dictionary() -> GradeDictionarySnapshot:
    values = (
        ("DC01", "软钢", True),
        ("HC340LA+Z", "硬钢", True),
        ("DISABLED", "软钢", False),
    )
    return GradeDictionarySnapshot(
        "GQGA4",
        tuple(
            GradeDictionaryEntry(
                source_grade=grade,
                normalized_grade=normalize_grade(grade),
                soft_hard_class=classification,
                roll_type="大辊",
                steel_classes="测试类别",
                is_if_steel=False,
                enabled=enabled,
                source_file="test-grade-dictionary.xlsx",
                source_row_count=1,
                source_rows=str(index + 2),
                remark="",
            )
            for index, (grade, classification, enabled) in enumerate(values)
        ),
    )


def write_v3_grade_dictionary_database(database_path, snapshot=None):
    snapshot = sample_grade_dictionary() if snapshot is None else snapshot
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            CREATE TABLE aps_gqga4_grade_dictionary (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                product_line_code TEXT NOT NULL,
                grade TEXT NOT NULL,
                roll_type TEXT NOT NULL,
                soft_hard_class TEXT NOT NULL,
                steel_classes TEXT NOT NULL,
                is_if_steel INTEGER NOT NULL,
                enabled INTEGER NOT NULL,
                source_file TEXT NOT NULL,
                source_row_count INTEGER NOT NULL,
                source_rows TEXT NOT NULL,
                remark TEXT NOT NULL
            )
            """
        )
        connection.executemany(
            """
            INSERT INTO aps_gqga4_grade_dictionary (
                product_line_code, grade, roll_type, soft_hard_class, steel_classes,
                is_if_steel, enabled, source_file, source_row_count, source_rows, remark
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                (
                    snapshot.product_line_code,
                    item.source_grade,
                    item.roll_type,
                    item.soft_hard_class,
                    item.steel_classes,
                    int(item.is_if_steel),
                    int(item.enabled),
                    item.source_file,
                    item.source_row_count,
                    item.source_rows,
                    item.remark,
                )
                for item in snapshot.entries
            ),
        )
    return database_path
