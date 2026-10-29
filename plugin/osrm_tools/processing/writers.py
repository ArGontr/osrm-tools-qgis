# -*- coding: utf-8 -*-
"""Escritores CSV/XLSX compartidos."""
import csv
import os


def write_csv(path, header, rows):
    with open(path, 'w', newline='', encoding='utf-8-sig') as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


def append_csv(path, header, rows):
    """Anexa filas; escribe encabezado si el archivo es nuevo o vacio."""
    write_header = not os.path.exists(path) or os.path.getsize(path) == 0
    with open(path, 'a', newline='', encoding='utf-8-sig') as f:
        w = csv.writer(f)
        if write_header:
            w.writerow(header)
        w.writerows(rows)


def read_ids(path, col=0):
    """Lee la columna de IDs de un CSV existente (para reanudar)."""
    ids = set()
    if not path or not os.path.exists(path):
        return ids
    with open(path, 'r', encoding='utf-8-sig') as f:
        reader = csv.reader(f)
        for i, row in enumerate(reader):
            if i == 0:
                continue
            if len(row) > col:
                ids.add(row[col])
    return ids


def write_xlsx(path, sheet_name, header, csv_path):
    """Convierte un CSV existente a XLSX (streaming). False si no hay openpyxl."""
    try:
        from openpyxl import Workbook
    except ImportError:
        return False
    wb = Workbook(write_only=True)
    ws = wb.create_sheet(title=(sheet_name or 'Resultados')[:31])
    with open(csv_path, 'r', encoding='utf-8-sig') as f:
        for row in csv.reader(f):
            ws.append(row)
    wb.save(path)
    return True
