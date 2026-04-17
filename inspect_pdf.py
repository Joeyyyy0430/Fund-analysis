import argparse
import os

import pdfplumber


def inspect_pdf(pdf_file):
    if not os.path.exists(pdf_file):
        print(f"File not found: {pdf_file}")
        return

    with pdfplumber.open(pdf_file) as pdf:
        print(f"Total pages: {len(pdf.pages)}")
        for i, page in enumerate(pdf.pages):
            print(f"--- Page {i+1} ---")
            tables = page.extract_tables()
            for j, table in enumerate(tables):
                print(f"Table {j+1}:")
                # Print first 2 rows of each table to see headers and data
                for row in table[:2]:
                    print(row)
            
            # If no tables, try text
            if not tables:
                text = page.extract_text()
                if text:
                    print("No tables found, text snapshot:")
                    print(text[:500])

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Inspect the tables extracted from a fund statement PDF.")
    parser.add_argument("pdf_file", help="Path to the PDF file.")
    args = parser.parse_args()
    inspect_pdf(args.pdf_file)
