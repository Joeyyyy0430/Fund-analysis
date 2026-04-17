import argparse

import pandas as pd


def inspect_excel(input_file):
    try:
        df = pd.read_excel(input_file)
        print("Columns found in Excel:")
        print(df.columns.tolist())
        print("\nFirst 5 rows:")
        print(df.head().to_string())
    except Exception as exc:
        print(f"Error reading Excel file: {exc}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Inspect the columns and preview rows from an Excel trade export.")
    parser.add_argument("input_file", help="Path to the Excel file.")
    args = parser.parse_args()
    inspect_excel(args.input_file)
